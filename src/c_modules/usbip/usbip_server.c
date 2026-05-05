/* Annealage Pod: USB/IP multiplexer server.
 *
 * Concurrency model (R20 per-EP lane architecture):
 *
 *   The earlier shape ran handle_urb_stream as a strict read-one,
 *   submit-one, complete-one, send-one loop. With one URB in flight
 *   per connection, CDC ACM open deadlocks: the kernel cdc-acm
 *   driver submits an interrupt-IN that pends until line state is
 *   set, then a class-control OUT that sets line state. The OUT
 *   queues behind the pending IN and never dispatches.
 *
 *   handle_urb_stream is now a pure read loop. For real-device
 *   URBs it builds an inflight record, links it into the per-
 *   connection seqnum table, and dispatches it to a per-(ep,dir)
 *   lane task. Each lane task dequeues URBs in FIFO order, calls
 *   usbhost_*_transfer (async at the IDF layer so multiple URBs
 *   can be in flight concurrently across lanes), then sends
 *   RET_SUBMIT under a per-connection tx_mutex.
 *
 *   Synthetic devices (CMSIS-DAP) take a fast path: latency is
 *   microseconds, contention is impossible, and the simpler inline
 *   dispatch keeps the regression baseline well-trodden.
 *
 *   CMD_UNLINK is real: the seqnum table is consulted, the cancel
 *   flag is flipped, the lane task observes it via usbhost's halt+
 *   flush+clear path and the URB completes with -ECONNRESET. The
 *   lane task sends RET_SUBMIT first; the read loop sends RET_UNLINK
 *   immediately on receipt to keep the kernel's unlink ledger
 *   balanced.
 */

#include "usbip_server.h"
#include "usbip_proto.h"
#include "../usbhost/usbhost.h"

#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

#ifndef MPY_POD_HOST_TEST_BUILD

#include <inttypes.h>
#include <unistd.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "freertos/idf_additions.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"  /* for esp_timer_get_time() in verbose timing probes */

#include "lwip/sockets.h"
#include "lwip/inet.h"
#include "netinet/tcp.h"
#include "lwip/sys.h"

static const char *TAG = "usbip";

#define USBIP_SERVER_TASK_STACK    8192
#define USBIP_SERVER_TASK_PRIORITY 5
#define USBIP_CLIENT_TASK_STACK    8192
#define USBIP_CLIENT_TASK_PRIORITY 5
#define USBIP_WORKER_TASK_STACK    8192
#define USBIP_WORKER_TASK_PRIORITY 5

/* R22 responder task: priority above IDF worker (9) so completion
 * callbacks preempt the worker immediately. See r22-plan.md caveat 1:
 * priority 11 is above Wi-Fi TX (8) and lwIP (5). If Wi-Fi disconnects
 * or lwip_writev returns EAGAIN under sustained load, drop to 10. */
#define USBIP_RESPONDER_TASK_STACK    8192
#define USBIP_RESPONDER_TASK_PRIORITY 11

/* Pipeline depth per (ep,dir) lane: max URBs in flight concurrently.
 * R27: depth=1 is required when usbhost.c is the TinyUSB backend
 * (gotcha #2 from r24-wip-history.md: tuh_edpt_xfer allows only one
 * transfer in flight per (dev,ep); subsequent submits while busy
 * return false. Pipeline depth at the lane layer must be 1 to avoid
 * a flood of `tuh_edpt_xfer rejected` returns under load). On main
 * with the IDF host backend depth was 16 (chosen for throughput);
 * the R23 deep-dive measurement of avg_depth=13-15 confirmed the
 * lane queue did fill at depth=16. With TinyUSB the floor is 1
 * regardless of throughput goals; multi-URB pipelining must be
 * achieved by other means (multiple EPs, hardware-side scheduling
 * tweaks, or eventually a class-driver-based forwarder). */
#ifndef USBIP_PIPELINE_DEPTH
#define USBIP_PIPELINE_DEPTH 1
#endif

/* R23 step 2: max URBs coalesced into one lwip_writev call.
 * Each URB contributes at most 2 iovec entries (header + payload),
 * so iov[] is dimensioned at 2*USBIP_BATCH_MAX. lwIP IOV_MAX is
 * 0xFFFF on ESP-IDF; 32 entries is well within budget. */
#ifndef USBIP_BATCH_MAX
#define USBIP_BATCH_MAX 16
#endif

/* Task stack memory caps. Default xTaskCreatePinnedToCore allocates
 * stacks from internal SRAM (~232 KiB region on the S3, mostly already
 * consumed at boot by IDF / wifi / lwIP). Per-EP lane tasks (up to 32
 * per connection) at 8 KiB plus the client_task overflow that region.
 * xTaskCreatePinnedToCoreWithCaps allocates stacks from PSRAM
 * (8 MiB free) so two concurrent connections coexist. The TCB itself
 * is still small and stays in internal RAM. */
#define USBIP_TASK_STACK_CAPS  (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)

#define USBIP_TASK_CORE 1

#define USBIP_MAX_TRANSFER_DEFAULT (16 * 1024)

#define USBIP_MAX_CLIENTS 4

/* Per-connection inflight URB cap; cdc-acm queues 16 reads + a few
 * writes + interrupt + control. 32 covers worst case. */
#define USBIP_INFLIGHT_MAX 32

/* Per-EP lane task stack size. One lane per active (ep,dir) pair. */
#define USBIP_LANE_TASK_STACK  8192

typedef struct {
    bool                in_use;
    char                busid[USBIP_BUSID_SIZE];
    int                 fd;
} client_slot_t;

typedef struct {
    TaskHandle_t          task;
    int                   listen_fd;
    bool                  running;
    SemaphoreHandle_t     attach_lock;
    client_slot_t         slots[USBIP_MAX_CLIENTS];
    int32_t               max_transfer;
} usbip_server_state_t;

static usbip_server_state_t s_state = {
    .listen_fd     = -1,
    .max_transfer  = USBIP_MAX_TRANSFER_DEFAULT,
};

static volatile bool s_urb_verbose = false;

void usbip_server_set_verbose(bool enable)
{
    s_urb_verbose = enable;
    ESP_LOGI(TAG, "URB verbose logging %s", enable ? "enabled" : "disabled");
}

bool usbip_server_is_verbose(void)
{
    return s_urb_verbose;
}

/* ---------- attachment table ---------- */

static bool attachment_acquire(int fd, const char busid[USBIP_BUSID_SIZE],
                               size_t *slot_idx)
{
    if (s_state.attach_lock == NULL) {
        return false;
    }
    xSemaphoreTake(s_state.attach_lock, portMAX_DELAY);

    for (size_t i = 0; i < USBIP_MAX_CLIENTS; i++) {
        if (s_state.slots[i].in_use &&
            memcmp(s_state.slots[i].busid, busid, USBIP_BUSID_SIZE) == 0) {
            xSemaphoreGive(s_state.attach_lock);
            return false;
        }
    }
    for (size_t i = 0; i < USBIP_MAX_CLIENTS; i++) {
        if (!s_state.slots[i].in_use) {
            s_state.slots[i].in_use = true;
            s_state.slots[i].fd = fd;
            memcpy(s_state.slots[i].busid, busid, USBIP_BUSID_SIZE);
            *slot_idx = i;
            xSemaphoreGive(s_state.attach_lock);
            return true;
        }
    }
    xSemaphoreGive(s_state.attach_lock);
    return false;
}

static void attachment_release(size_t slot_idx)
{
    if (s_state.attach_lock == NULL || slot_idx >= USBIP_MAX_CLIENTS) {
        return;
    }
    xSemaphoreTake(s_state.attach_lock, portMAX_DELAY);
    s_state.slots[slot_idx].in_use = false;
    s_state.slots[slot_idx].fd = -1;
    memset(s_state.slots[slot_idx].busid, 0, USBIP_BUSID_SIZE);
    xSemaphoreGive(s_state.attach_lock);
}

size_t usbip_server_attached_busids(char (*out)[USBIP_BUSID_SIZE], size_t max)
{
    if (out == NULL || max == 0 || s_state.attach_lock == NULL) {
        return 0;
    }
    size_t copied = 0;
    xSemaphoreTake(s_state.attach_lock, portMAX_DELAY);
    for (size_t i = 0; i < USBIP_MAX_CLIENTS && copied < max; i++) {
        if (s_state.slots[i].in_use) {
            memcpy(out[copied], s_state.slots[i].busid, USBIP_BUSID_SIZE);
            copied++;
        }
    }
    xSemaphoreGive(s_state.attach_lock);
    return copied;
}

/* ---------- byte-stream helpers ---------- */

static bool read_exact(int fd, void *buf, size_t len)
{
    uint8_t *ptr = (uint8_t *)buf;
    size_t remaining = len;
    while (remaining > 0) {
        const ssize_t n = recv(fd, ptr, remaining, 0);
        if (n <= 0) {
            return false;
        }
        ptr += (size_t)n;
        remaining -= (size_t)n;
    }
    return true;
}

static bool write_all(int fd, const void *buf, size_t len)
{
    const uint8_t *ptr = (const uint8_t *)buf;
    size_t remaining = len;
    while (remaining > 0) {
        const ssize_t n = send(fd, ptr, remaining, 0);
        if (n <= 0) {
            return false;
        }
        ptr += (size_t)n;
        remaining -= (size_t)n;
    }
    return true;
}

/* Send two buffers (header + payload) as one atomic stream write.
 * Uses lwip_writev to avoid malloc + memcpy. lwip_writev does a single
 * sendmsg; on TCP_NODELAY sockets the combined header+payload fits
 * in one segment for all USB/IP URB sizes we use (<= 16 KiB + 48 B).
 * Falls back to a drain loop on partial writes (rare on LAN-local TCP). */
static bool writev_all(int fd, const void *hdr, size_t hdr_len,
                       const void *body, size_t body_len)
{
    struct iovec iov[2];
    iov[0].iov_base = (void *)hdr;
    iov[0].iov_len  = hdr_len;
    iov[1].iov_base = (void *)body;
    iov[1].iov_len  = body_len;
    size_t total = hdr_len + body_len;
    ssize_t sent = lwip_writev(fd, iov, 2);
    if (sent < 0) {
        return false;
    }
    if ((size_t)sent == total) {
        return true;
    }
    /* Partial write: drain the remainder through write_all. */
    size_t done = (size_t)sent;
    if (done < hdr_len) {
        if (!write_all(fd, (const uint8_t *)hdr + done, hdr_len - done)) {
            return false;
        }
        done = hdr_len;
    }
    size_t body_done = done - hdr_len;
    if (body_done < body_len) {
        if (!write_all(fd, (const uint8_t *)body + body_done,
                       body_len - body_done)) {
            return false;
        }
    }
    return true;
}

static bool discard_exact(int fd, size_t len)
{
    uint8_t scratch[128];
    size_t remaining = len;
    while (remaining > 0) {
        size_t chunk = remaining < sizeof(scratch) ? remaining : sizeof(scratch);
        if (!read_exact(fd, scratch, chunk)) {
            return false;
        }
        remaining -= chunk;
    }
    return true;
}

/* ---------- protocol-level send wrappers ---------- */

static bool send_op_common(int fd, uint16_t code, uint32_t status)
{
    usbip_op_common_t reply;
    usbip_proto_pack_op_common(&reply, code, status);
    return write_all(fd, &reply, sizeof(reply));
}

static bool send_device_with_interfaces(int fd, const usbip_dev_record_t *device)
{
    usbip_device_desc_t wire;
    usbip_proto_pack_device_desc(device, &wire);
    if (!write_all(fd, &wire, sizeof(wire))) {
        return false;
    }
    for (uint8_t i = 0; i < device->num_interfaces; i++) {
        usbip_interface_desc_t iface;
        if (!usbip_proto_pack_interface_desc(device, i, &iface)) {
            break;
        }
        if (!write_all(fd, &iface, sizeof(iface))) {
            return false;
        }
    }
    return true;
}

/* ---------- DEVLIST / IMPORT ---------- */

static size_t collect_all_devices(usbip_dev_record_t *out, size_t max)
{
    size_t copied = usbhost_get_devices(out, max);
    if (copied < max) {
        copied += usbip_get_virtual_devices(out + copied, max - copied);
    }
    return copied;
}

static bool find_device_by_busid(const char busid[USBIP_BUSID_SIZE],
                                 usbip_dev_record_t *out)
{
    virtual_device_t *vdev = usbip_find_virtual_device(busid);
    if (vdev != NULL) {
        *out = vdev->desc;
        return true;
    }
    return usbhost_get_device_by_busid(busid, out);
}

static bool handle_devlist_request(int fd)
{
    usbip_dev_record_t devices[USBIP_MAX_CLIENTS + USBIP_VIRTUAL_DEVICE_MAX];
    const size_t count = collect_all_devices(devices,
        sizeof(devices) / sizeof(devices[0]));

    /* IDF newlib-nano printf does not implement %zu; cast to unsigned. */
    ESP_LOGI(TAG, "DEVLIST: reporting %u device(s)", (unsigned)count);

    if (!send_op_common(fd, USBIP_OP_REP_DEVLIST, 0)) {
        return false;
    }
    uint32_t count_be = htonl((uint32_t)count);
    if (!write_all(fd, &count_be, sizeof(count_be))) {
        return false;
    }
    for (size_t i = 0; i < count; i++) {
        if (!send_device_with_interfaces(fd, &devices[i])) {
            return false;
        }
    }
    return true;
}

/* ---------- per-connection async URB plumbing ---------- */

typedef struct inflight_urb {
    usbip_decoded_header_t hdr;
    char                   busid[USBIP_BUSID_SIZE];
    uint32_t               expected_devid;
    bool                   is_virtual;
    uint8_t               *out_buf;
    size_t                 out_len;
    uint8_t               *in_buf;
    size_t                 in_capacity;
    /* lane_idx: 5-bit (ep,dir) fold, indexes conn->lanes[]. Computed
     * at intake_submit time (single-threaded read loop). Replaces the
     * old tx_ticket / tx_order_idx submit-ordering fields which are
     * removed in R20 step3 (per-EP lanes serialise submit by
     * construction). */
    uint8_t                lane_idx;
    volatile bool          cancel;
    volatile bool          retired; /* true once RET_SUBMIT has been sent */
    /* Atomic ownership of the kernel-visible giveback. Exactly one of
     * {RET_SUBMIT, RET_UNLINK} must be sent per URB once UNLINK has
     * been received; both confuses vhci_rx ("cannot find a urb of
     * seqnum N") and tears down the connection. The worker claims
     * RET_SUBMIT under inflight_mutex before calling tx_ret_submit;
     * the UNLINK handler claims RET_UNLINK under the same mutex if
     * the worker has not already claimed. Whoever loses the race skips
     * its tx but still advances the tx-order gate. */
    enum {
        TX_OWNER_NONE = 0,
        TX_OWNER_RET_SUBMIT,
        TX_OWNER_RET_UNLINK,
    } tx_owner;
    /* Cancel-then-wait coordination per USB/IP spec ordering. The
     * UNLINK handler sets cancel and waits on cancel_done_sem so the
     * worker's RET_SUBMIT (with status -ECONNRESET) reaches the kernel
     * before our RET_UNLINK; otherwise vhci_rx logs "cannot find a urb
     * of seqnum N" and usb_kill_urb hangs because nothing gives the
     * URB back. cancel_waiters arbitrates who frees the inflight. */
    SemaphoreHandle_t      cancel_done_sem;
    int                    cancel_waiters;
    struct conn_state     *conn;
    struct inflight_urb   *next;
    /* R22: completion result written by lane_completion_cb (from IDF
     * callback context) and read by the responder task. The responder
     * runs at priority 11 > IDF worker 9; it reads these fields only
     * after the queue-send in the callback establishes a happens-before
     * relationship via the queue send/receive pair. */
    int                    comp_status;
    size_t                 comp_in_len;
    /* R22 diagnostic timing (verbose only): microseconds since boot. */
    int64_t                t_submit; /* when lane_task called submit_async */
    int64_t                t_cb;     /* when lane_completion_cb ran */
} inflight_urb_t;

/* Per-EP lane: one task + one queue per active (ep,dir). The lane task
 * calls run_inflight for each URB it dequeues, serialising all URBs on
 * one pipe through a single task (no worker-race). NULL sentinel posted
 * at teardown causes the task to exit.
 *
 * R22: inflight_slots is a counting semaphore initialised to
 * USBIP_PIPELINE_DEPTH. The lane task takes one slot before each
 * async submit. The responder gives it back after each RET_SUBMIT.
 * This bounds the pipeline depth per pipe. */
typedef struct {
    QueueHandle_t  queue;          /* inflight_urb_t* drained by lane task */
    TaskHandle_t   task;           /* NULL if lane not yet spawned */
    bool           alive;          /* false after sentinel processed */
    SemaphoreHandle_t inflight_slots; /* counting sem, max=USBIP_PIPELINE_DEPTH */
} per_ep_lane_t;

/* tx_order_slot_t and the submit-order spin gate are removed in R20
 * step3. Per-EP lanes serialise submit by construction: one lane task
 * per (ep,dir) calls run_inflight (and therefore
 * usb_host_transfer_submit) in FIFO arrival order without a spin gate.
 * The usbhost_submit_order_t cross-module callback dependency also
 * drops as a side effect. */

typedef struct conn_state {
    int                fd;
    bool               shutdown;        /* stop workers cleanly on disconnect */
    SemaphoreHandle_t  tx_mutex;        /* serialise writes to the TCP socket */
    SemaphoreHandle_t  inflight_mutex;  /* protects the inflight list */
    inflight_urb_t    *inflight_head;
    int                inflight_count;
    SemaphoreHandle_t  inflight_drain;  /* given when count drops to 0 */
    /* Per-EP lane dispatch (R20). One lane per active (ep,dir). Indexed
     * by lane_index(ep, dir). */
    per_ep_lane_t      lanes[32];
    volatile int       lanes_alive;    /* count of live lane tasks */
    SemaphoreHandle_t  lanes_done;     /* given when lanes_alive drops to 0 */
    /* R22 responder task. One per connection at priority 11 (above IDF
     * worker at 9). Completions are pushed onto responder_queue from
     * the IDF callback context; the responder drains it, sends
     * RET_SUBMIT under tx_mutex, and gives back the lane's inflight
     * slot. Spawned in handle_import_request for real-host paths.
     * Teardown: post NULL sentinel, wait on responder_alive.
     * Queue sized at USBIP_INFLIGHT_MAX * 2 to cover two pipes. */
    QueueHandle_t      responder_queue;  /* inflight_urb_t* of completed URBs */
    TaskHandle_t       responder_task;
    SemaphoreHandle_t  responder_alive;  /* given when responder exits */
    /* Refcount under inflight_mutex. Initial value 1 (read loop). Each
     * spawned lane task takes one reference. Whoever drops the last
     * reference frees the struct. Heap-allocated so a lane task wedged
     * inside an IDF call that never completes cannot trigger
     * use-after-free of stack memory once handle_import_request returns. */
    int                refcount;
} conn_state_t;

static void conn_state_free(conn_state_t *conn);

/* Drop a reference. If the count reaches zero we own the destruction. */
static void conn_state_release(conn_state_t *conn)
{
    if (conn == NULL) return;
    bool last = false;
    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    if (--conn->refcount == 0) {
        last = true;
    }
    xSemaphoreGive(conn->inflight_mutex);
    if (last) {
        conn_state_free(conn);
    }
}

/* Fold (ep_num, direction) into a 5-bit index for lanes[]. */
static uint8_t lane_index(uint32_t ep, uint32_t direction)
{
    return (uint8_t)((ep & 0x0F) | ((direction == USBIP_DIR_IN) ? 0x10 : 0));
}

static void inflight_link(conn_state_t *conn, inflight_urb_t *u)
{
    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    u->next = conn->inflight_head;
    conn->inflight_head = u;
    conn->inflight_count++;
    xSemaphoreGive(conn->inflight_mutex);
}

static void inflight_unlink(conn_state_t *conn, inflight_urb_t *u)
{
    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    inflight_urb_t **p = &conn->inflight_head;
    while (*p != NULL) {
        if (*p == u) {
            *p = u->next;
            conn->inflight_count--;
            if (conn->inflight_count == 0 && conn->inflight_drain != NULL) {
                xSemaphoreGive(conn->inflight_drain);
            }
            break;
        }
        p = &(*p)->next;
    }
    xSemaphoreGive(conn->inflight_mutex);
}

/* Free the inflight buffers and the struct. Caller must hold no
 * mutex protecting the URB and must own the right to free (either
 * the worker observed no cancel waiter, or the UNLINK waiter has
 * decremented the last reference). */
static void inflight_free(inflight_urb_t *u)
{
    if (u == NULL) {
        return;
    }
    if (u->cancel_done_sem != NULL) {
        vSemaphoreDelete(u->cancel_done_sem);
    }
    free(u->out_buf);
    free(u->in_buf);
    free(u);
}

/* Find the inflight URB matching seqnum and arrange a synchronous
 * cancel: set cancel=true under the mutex, take a reference (so the
 * worker hands off freeing to us), and return the cancel_done_sem so
 * the caller can wait until the worker has finished sending
 * RET_SUBMIT. Returns NULL if no matching URB is in flight (already
 * retired or never seen). The returned struct stays alive until the
 * caller invokes inflight_release_after_cancel. */
static inflight_urb_t *inflight_begin_cancel(conn_state_t *conn,
                                             uint32_t seqnum)
{
    inflight_urb_t *hit = NULL;
    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    for (inflight_urb_t *u = conn->inflight_head; u != NULL; u = u->next) {
        if (u->hdr.seqnum == seqnum && !u->retired) {
            u->cancel = true;
            u->cancel_waiters++;
            hit = u;
            break;
        }
    }
    xSemaphoreGive(conn->inflight_mutex);
    return hit;
}

/* Drop the cancel reference taken by inflight_begin_cancel. If the
 * worker has already retired the URB and we are the last reference,
 * we own the free. */
static void inflight_release_after_cancel(conn_state_t *conn,
                                          inflight_urb_t *u)
{
    if (u == NULL) {
        return;
    }
    bool free_now = false;
    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    int n = --u->cancel_waiters;
    if (n == 0 && u->retired) {
        free_now = true;
    }
    xSemaphoreGive(conn->inflight_mutex);
    if (free_now) {
        inflight_free(u);
    }
}

/* Send RET_SUBMIT under tx_mutex so workers do not interleave bytes
 * on the wire. Returns false if the socket dropped.
 * site is a short call-site label for instrumentation.
 *
 * Path B b1: when a payload is present, use writev to send header +
 * payload in a single syscall, avoiding the malloc + two memcpy +
 * free that the previous implementation used. writev is provided by
 * lwip/sockets.h on ESP-IDF lwIP. For the no-payload case a single
 * write_all suffices. */
static bool tx_ret_submit(conn_state_t *conn,
                          uint32_t seqnum, uint32_t devid,
                          uint32_t direction, uint32_t ep,
                          int32_t status,
                          const uint8_t *payload, uint32_t payload_len,
                          const char *site)
{
    if (s_urb_verbose) {
        ESP_LOGI(TAG, "TX_RET_SUBMIT seq=%" PRIu32 " ep=%" PRIu32 " dir=%" PRIu32
                      " status=%" PRId32 " len=%" PRIu32 " site=%s",
                 seqnum, ep, direction, status, payload_len, site);
    }
    usbip_header_t reply;
    usbip_proto_pack_ret_submit(&reply, seqnum, devid, direction, ep,
                                status, payload_len);

    xSemaphoreTake(conn->tx_mutex, portMAX_DELAY);
    bool ok;
    if (payload_len > 0 && payload != NULL) {
        ok = writev_all(conn->fd, &reply, sizeof(reply), payload, payload_len);
    } else {
        ok = write_all(conn->fd, &reply, sizeof(reply));
    }
    xSemaphoreGive(conn->tx_mutex);
    return ok;
}

static bool tx_ret_unlink(conn_state_t *conn,
                          uint32_t seqnum, uint32_t devid,
                          uint32_t direction, uint32_t ep,
                          int32_t status,
                          const char *site)
{
    if (s_urb_verbose) {
        ESP_LOGI(TAG, "TX_RET_UNLINK seq=%" PRIu32 " ep=%" PRIu32 " dir=%" PRIu32
                      " status=%" PRId32 " site=%s",
                 seqnum, ep, direction, status, site);
    }
    usbip_header_t reply;
    usbip_proto_pack_ret_unlink(&reply, seqnum, devid, direction, ep, status);
    xSemaphoreTake(conn->tx_mutex, portMAX_DELAY);
    bool ok = write_all(conn->fd, &reply, sizeof(reply));
    xSemaphoreGive(conn->tx_mutex);
    return ok;
}

/* submit_order_t / tx_order_wait / tx_order_advance removed in R20
 * step3. Per-EP lane tasks serialise both submit and RET_SUBMIT order
 * by construction (FIFO queue per (ep,dir)). */

/* Run a single inflight URB end-to-end: backend dispatch + RET_SUBMIT.
 * Caller is either the read loop (synthetic fast path) or a worker
 * (real-host). The function frees the inflight buffers on return. */
static void run_inflight(inflight_urb_t *u)
{
    conn_state_t *conn = u->conn;
    const usbip_decoded_header_t *hdr = &u->hdr;

    /* Experiment 2: per-URB latency breakdown (verbose-gated).
     * t0 = entry, t1 = after IDF transfer, t2 = after tcp send.
     * xTaskGetTickCount() resolution is 1 ms (configTICK_RATE_HZ=1000). */
    TickType_t t0 = 0, t1 = 0;
    if (s_urb_verbose) {
        t0 = xTaskGetTickCount();
        ESP_LOGI(TAG, "usbip_dispatch: busid=%.32s ep=%" PRIu32 " dir=%s len=%" PRIu32
                      " target=%s",
                 u->busid, hdr->ep,
                 (hdr->direction == USBIP_DIR_IN) ? "IN" : "OUT",
                 hdr->transfer_buffer_length,
                 u->is_virtual ? "virtual" : "host");
    }

    int    status = 0;
    size_t in_len = 0;

    if (u->is_virtual) {
        virtual_device_t *vdev = usbip_find_virtual_device(u->busid);
        if (vdev == NULL) {
            status = -ENODEV;
        } else if (hdr->ep == 0) {
            usbip_setup_packet_t setup;
            memcpy(&setup, hdr->setup, sizeof(setup));
            status = vdev->ops->control_transfer(vdev, &setup,
                                                 u->out_buf, u->out_len,
                                                 u->in_buf, u->in_capacity,
                                                 &in_len);
        } else {
            uint8_t ep_addr = (uint8_t)(hdr->ep |
                              (hdr->direction == USBIP_DIR_IN ? 0x80u : 0x00u));
            status = vdev->ops->data_transfer(vdev, ep_addr,
                                              u->out_buf, u->out_len,
                                              u->in_buf, u->in_capacity,
                                              &in_len);
        }
    } else if (hdr->ep == 0) {
        usbip_setup_packet_t setup;
        memcpy(&setup, hdr->setup, sizeof(setup));
        status = usbhost_control_transfer(u->busid, &setup,
                                          u->out_buf, u->out_len,
                                          u->in_buf, u->in_capacity,
                                          &in_len, &u->cancel);
    } else {
        uint8_t ep_addr = (uint8_t)(hdr->ep |
                          (hdr->direction == USBIP_DIR_IN ? 0x80u : 0x00u));
        /* Per-EP lane tasks serialise submit by construction (FIFO queue
         * per (ep,dir)), so plain transfer calls suffice. The _ordered
         * variants and submit_order_t are removed in R20 step3. */
        if (usbhost_is_interrupt_endpoint(u->busid, (uint8_t)hdr->ep,
                                          (uint8_t)hdr->direction)) {
            status = usbhost_interrupt_transfer(u->busid, ep_addr,
                                                u->out_buf, u->out_len,
                                                u->in_buf, u->in_capacity,
                                                &in_len, &u->cancel);
        } else {
            status = usbhost_bulk_transfer(u->busid, ep_addr,
                                           u->out_buf, u->out_len,
                                           u->in_buf, u->in_capacity,
                                           &in_len, &u->cancel);
        }
    }

    if (s_urb_verbose) {
        t1 = xTaskGetTickCount();
    }

    /* If the URB was cancelled (UNLINK arrived, or teardown signalled),
     * the kernel expects RET_SUBMIT with status -ECONNRESET. usbhost
     * may have returned -ECONNRESET, 0 (race: completed just before
     * cancel landed), or another error. Override to -ECONNRESET so
     * vhci_rx marks the URB as cancelled. */
    bool was_cancelled = u->cancel;
    if (was_cancelled) {
        status = -ECONNRESET;
        in_len = 0;
    }

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbip_complete: busid=%.32s ep=%" PRIu32
                      " status=%d actual=%u%s t_idf_ms=%" PRIu32,
                 u->busid, hdr->ep, status, (unsigned)in_len,
                 was_cancelled ? " (cancelled)" : "",
                 (uint32_t)(t1 - t0));
    }

    /* Claim RET_SUBMIT ownership atomically. The UNLINK handler claims
     * the same field under inflight_mutex; whichever loses skips its
     * giveback so vhci_rx does not see a duplicate "cannot find a urb
     * of seqnum N". The done gate is removed in R20 step2: per-EP lanes
     * serialise by construction (one lane task per pipe, FIFO queue). */
    bool send_ret_submit = false;
    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    if (u->tx_owner == TX_OWNER_NONE) {
        u->tx_owner = TX_OWNER_RET_SUBMIT;
        send_ret_submit = true;
    }
    xSemaphoreGive(conn->inflight_mutex);

    bool ok = true;
    if (send_ret_submit) {
        ok = tx_ret_submit(conn,
                           hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                           status,
                           (status == 0 && hdr->direction == USBIP_DIR_IN) ? u->in_buf : NULL,
                           (status == 0 && hdr->direction == USBIP_DIR_IN)
                             ? (uint32_t)in_len : 0,
                           "worker");
    } else if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbip_suppress: busid=%.32s ep=%" PRIu32
                      " seq=%" PRIu32 " (RET_UNLINK already sent)",
                 u->busid, hdr->ep, hdr->seqnum);
    }

    if (s_urb_verbose) {
        TickType_t t2 = xTaskGetTickCount();
        ESP_LOGI(TAG, "usbip_timing: ep=%" PRIu32 " dir=%s len=%" PRIu32
                      " t_idf_ms=%" PRIu32 " t_tcp_ms=%" PRIu32
                      " t_total_ms=%" PRIu32,
                 hdr->ep,
                 (hdr->direction == USBIP_DIR_IN) ? "IN" : "OUT",
                 hdr->transfer_buffer_length,
                 (uint32_t)(t1 - t0),
                 (uint32_t)(t2 - t1),
                 (uint32_t)(t2 - t0));
    }

    /* Retire: remove from inflight list, mark retired, and either
     * free now (no UNLINK waiter) or hand off to the waiter (which
     * is blocked on cancel_done_sem). The cancel_waiters field is
     * read under the mutex to avoid a TOCTOU with inflight_begin_cancel. */
    bool free_now = false;
    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    inflight_urb_t **p = &conn->inflight_head;
    while (*p != NULL) {
        if (*p == u) {
            *p = u->next;
            conn->inflight_count--;
            if (conn->inflight_count == 0 && conn->inflight_drain != NULL) {
                xSemaphoreGive(conn->inflight_drain);
            }
            break;
        }
        p = &(*p)->next;
    }
    u->retired = true;
    if (u->cancel_waiters == 0) {
        free_now = true;
    } else if (u->cancel_done_sem != NULL) {
        xSemaphoreGive(u->cancel_done_sem);
    }
    xSemaphoreGive(conn->inflight_mutex);

    if (free_now) {
        inflight_free(u);
    }

    if (!ok) {
        /* Best effort: nudge accept-loop side. The read loop will
         * notice on its next recv. */
        shutdown(conn->fd, SHUT_RDWR);
    }
}

/* Block until conn->inflight_count reaches 0 or the deadline elapses.
 * Returns 0 on drained, -1 on timeout. */
static int outstanding_drain_wait(conn_state_t *conn, TickType_t timeout_ticks)
{
    TickType_t deadline = xTaskGetTickCount() + timeout_ticks;
    while (true) {
        xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
        int n = conn->inflight_count;
        xSemaphoreGive(conn->inflight_mutex);
        if (n == 0) return 0;
        if (xTaskGetTickCount() >= deadline) return -1;
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

/* submit_worker_task removed in R20 step4. All real-host URBs are now
 * dispatched via per-EP lane tasks (see lane_task below). */

/* Lane task arg: carries the conn pointer and the lane index so the
 * task knows which queue to drain and which lanes[] slot to clear. */
typedef struct {
    conn_state_t *conn;
    uint8_t       idx;
} lane_task_arg_t;

/* IDF transfer completion callback for async submits. Runs in IDF worker
 * context (priority 9). Must be short: push to responder_queue and return.
 * The responder (priority 11) preempts the worker immediately on queue-send.
 *
 * comp_status and comp_in_len are written here and read by the responder.
 * The queue send/receive pair establishes the happens-before relationship. */
static void lane_completion_cb(void *ctx, int status, size_t in_len)
{
    inflight_urb_t *u = (inflight_urb_t *)ctx;
    u->comp_status = status;
    u->comp_in_len = in_len;
    /* R25 step 5: always capture t_cb for cb2tx_timing aggregation in
     * the responder. Was verbose-gated; now unconditional (one
     * esp_timer_get_time call per URB; negligible cost). */
    u->t_cb = esp_timer_get_time();
    /* Non-blocking send: responder_queue is sized at USBIP_INFLIGHT_MAX*2
     * so it should never be full if the pipeline depth is respected. */
    BaseType_t sent = xQueueSend(u->conn->responder_queue, &u, 0);
    if (sent != pdTRUE) {
        /* Queue full - should not happen with proper depth management.
         * Log and synthesise an error completion. The URB will not be
         * given back; this leaks the inflight slot but avoids a hang. */
        ESP_LOGE("usbip", "lane_completion_cb: responder_queue full, seq=%" PRIu32,
                 u->hdr.seqnum);
    }
}

/* Per-EP lane task. Drains lane->queue, submits each URB asynchronously
 * via usbhost_submit_async. Backpressure via inflight_slots counting sem
 * (max=USBIP_PIPELINE_DEPTH). Exits on NULL sentinel.
 *
 * One task per active (ep,dir) pair. */
static void lane_task(void *arg)
{
    lane_task_arg_t *la   = (lane_task_arg_t *)arg;
    conn_state_t    *conn = la->conn;
    uint8_t          idx  = la->idx;
    free(la);

    QueueHandle_t q = conn->lanes[idx].queue;
    SemaphoreHandle_t slots = conn->lanes[idx].inflight_slots;

    while (true) {
        inflight_urb_t *u = NULL;
        if (xQueueReceive(q, &u, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (u == NULL) {
            /* NULL sentinel: teardown requested. */
            break;
        }

        /* Backpressure: wait for a pipeline slot. */
        xSemaphoreTake(slots, portMAX_DELAY);

        /* If cancelled before we even submitted (UNLINK arrived while
         * URB was queued), synthesise a completion rather than submitting.
         * This avoids a submit+immediate-cancel round-trip. */
        if (u->cancel) {
            u->comp_status = -ECONNRESET;
            u->comp_in_len = 0;
            xQueueSend(conn->responder_queue, &u, portMAX_DELAY);
            continue;
        }

        const usbip_decoded_header_t *hdr = &u->hdr;
        bool is_control = (hdr->ep == 0);
        bool is_in = (hdr->direction == USBIP_DIR_IN);
        uint8_t ep_addr = is_control ? 0
            : (uint8_t)(hdr->ep | (is_in ? 0x80u : 0x00u));

        usbip_setup_packet_t setup;
        if (is_control) {
            memcpy(&setup, hdr->setup, sizeof(setup));
        }

        if (s_urb_verbose) {
            u->t_submit = esp_timer_get_time();
        }
        int err = usbhost_submit_async(
            u->busid,
            ep_addr, is_control,
            is_control ? &setup : NULL,
            u->out_buf, u->out_len,
            u->in_buf,  u->in_capacity,
            lane_completion_cb, u);

        if (err < 0) {
            /* IDF rejected submit: synthesise completion so the responder
             * can send RET_SUBMIT and free the slot. The IDF will NOT
             * call the callback in this case. */
            u->comp_status = err;
            u->comp_in_len = 0;
            if (xQueueSend(conn->responder_queue, &u, portMAX_DELAY) != pdTRUE) {
                /* Responder queue full (should not happen). Return slot. */
                xSemaphoreGive(slots);
            }
        }
    }

    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    conn->lanes[idx].alive = false;
    int remaining = --conn->lanes_alive;
    xSemaphoreGive(conn->inflight_mutex);

    if (remaining == 0 && conn->lanes_done != NULL) {
        xSemaphoreGive(conn->lanes_done);
    }
    /* Drop the lane's reference to conn. */
    conn_state_release(conn);
    vTaskDeleteWithCaps(NULL);
}

/* R23 step 2 responder task. Extends R22 with batched RET_SUBMIT:
 * after the blocking xQueueReceive, drain additional completed URBs
 * non-blocking (up to USBIP_BATCH_MAX - 1 more) and emit one
 * lwip_writev for the entire batch under tx_mutex.
 *
 * Per-URB tx_owner arbitration (RET_SUBMIT vs RET_UNLINK) is still
 * done under inflight_mutex before deciding whether to include the
 * URB in the iovec array.  URBs that lose to UNLINK are skipped in
 * the batch (their RET_UNLINK is sent by the read loop).
 *
 * RET_SUBMIT ordering: the queue is FIFO; arrival order within each
 * (ep,dir) pipe is preserved by construction (IDF callback fires in
 * submit order per pipe; the responder drains in queue order; iovec
 * array is built in drain order).  Cross-pipe order is irrelevant
 * (vhci-hcd keys URBs by seqnum).
 *
 * Stack allocation: USBIP_BATCH_MAX usbip_header_t structs (16*48 =
 * 768 B) + 2*USBIP_BATCH_MAX iovec entries (16*2*8 = 256 B).  Total
 * ~1 KiB per responder wakeup on top of the base task stack.
 * USBIP_RESPONDER_TASK_STACK is 8192 B; this is well within budget. */
static void responder_task(void *arg)
{
    conn_state_t *conn = (conn_state_t *)arg;

    while (true) {
        /* --- Phase 1: batch drain ---------------------------------------- */
        inflight_urb_t *batch[USBIP_BATCH_MAX];
        int batch_n = 0;

        /* Blocking receive for the first URB. */
        inflight_urb_t *u0 = NULL;
        if (xQueueReceive(conn->responder_queue, &u0, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (u0 == NULL) {
            /* NULL sentinel: teardown. */
            break;
        }
        batch[batch_n++] = u0;

        /* Non-blocking drain for up to USBIP_BATCH_MAX-1 more. */
        while (batch_n < USBIP_BATCH_MAX) {
            inflight_urb_t *un = NULL;
            if (xQueueReceive(conn->responder_queue, &un, 0) != pdTRUE) {
                break;
            }
            if (un == NULL) {
                /* NULL sentinel arrived mid-batch: process the current
                 * batch first, then exit on the next outer loop iteration
                 * by re-posting the sentinel (queue has room: at least
                 * one slot freed because we just dequeued it). */
                xQueueSend(conn->responder_queue, &un, portMAX_DELAY);
                break;
            }
            batch[batch_n++] = un;
        }

        /* --- Phase 2: per-URB cancel override + tx_owner claim ----------- */
        /* Per-URB effective status/in_len after cancel override. */
        int    eff_status[USBIP_BATCH_MAX];
        size_t eff_in_len[USBIP_BATCH_MAX];
        bool   send_rs[USBIP_BATCH_MAX];  /* true if this URB sends RET_SUBMIT */

        for (int i = 0; i < batch_n; i++) {
            inflight_urb_t *u = batch[i];
            int    st  = u->comp_status;
            size_t len = u->comp_in_len;
            if (u->cancel) {
                st  = -ECONNRESET;
                len = 0;
            }
            eff_status[i] = st;
            eff_in_len[i] = len;

            xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
            if (u->tx_owner == TX_OWNER_NONE) {
                u->tx_owner = TX_OWNER_RET_SUBMIT;
                send_rs[i] = true;
            } else {
                send_rs[i] = false;
            }
            xSemaphoreGive(conn->inflight_mutex);
        }

        /* --- Phase 3: build iovec + emit one writev ----------------------- */
        /* Stack-allocate headers (one per URB that sends RET_SUBMIT) and
         * iovec array (up to 2 entries per such URB). */
        usbip_header_t hdrs[USBIP_BATCH_MAX];
        struct iovec   iov[USBIP_BATCH_MAX * 2];
        int iov_n = 0;

        for (int i = 0; i < batch_n; i++) {
            if (!send_rs[i]) {
                continue;
            }
            inflight_urb_t *u = batch[i];
            const usbip_decoded_header_t *hdr = &u->hdr;
            int    st  = eff_status[i];
            size_t len = eff_in_len[i];

            bool has_payload = (st == 0 && hdr->direction == USBIP_DIR_IN
                                && len > 0 && u->in_buf != NULL);
            uint32_t wire_len = has_payload ? (uint32_t)len : 0;

            usbip_proto_pack_ret_submit(&hdrs[i], hdr->seqnum, hdr->devid,
                                        hdr->direction, hdr->ep,
                                        st, wire_len);
            iov[iov_n].iov_base = &hdrs[i];
            iov[iov_n].iov_len  = sizeof(usbip_header_t);
            iov_n++;

            if (has_payload) {
                iov[iov_n].iov_base = u->in_buf;
                iov[iov_n].iov_len  = len;
                iov_n++;
            }
        }

        bool ok = true;
        if (iov_n > 0) {
            if (s_urb_verbose) {
                ESP_LOGI(TAG, "R23 batch send: batch_n=%d iov_n=%d", batch_n, iov_n);
            }
            /* R25 step 5: TCP timing instrumentation. Capture t_pre just
             * before the lwip_writev so cb2tx_timing measures the gap
             * between IDF callback (u->t_cb) and the actual send. The
             * tx_mutex take is included in writev cost; for the
             * single-connection bulk-IN read case there is no contender
             * so the take is a fast-path. */
            int64_t t_pre = esp_timer_get_time();
            xSemaphoreTake(conn->tx_mutex, portMAX_DELAY);
            ssize_t sent = lwip_writev(conn->fd, iov, iov_n);
            xSemaphoreGive(conn->tx_mutex);
            int64_t t_post = esp_timer_get_time();

            /* tcp_timing aggregator: per-writev-call latency. */
            {
                static uint32_t s_tcp_count = 0;
                static int64_t  s_tcp_sum_us = 0;
                static int64_t  s_tcp_min_us = INT64_MAX;
                static int64_t  s_tcp_max_us = 0;
                int64_t dt = t_post - t_pre;
                s_tcp_count++;
                s_tcp_sum_us += dt;
                if (dt < s_tcp_min_us) s_tcp_min_us = dt;
                if (dt > s_tcp_max_us) s_tcp_max_us = dt;
                if ((s_tcp_count % 100) == 0) {
                    ESP_LOGI(TAG, "tcp_timing: n=%" PRIu32
                                  " avg_writev=%" PRId32 "us"
                                  " min_writev=%" PRId32 "us"
                                  " max_writev=%" PRId32 "us",
                             s_tcp_count,
                             (int32_t)(s_tcp_sum_us / (int64_t)s_tcp_count),
                             (int32_t)s_tcp_min_us,
                             (int32_t)s_tcp_max_us);
                }
            }

            /* cb2tx_timing aggregator: per-URB cb -> writev-pre gap.
             * Counts every URB in this batch that actually sends a
             * RET_SUBMIT (send_rs[i]==true). Skips URBs with t_cb==0
             * (synthesised completions never reached lane_completion_cb). */
            {
                static uint32_t s_cb2tx_count = 0;
                static int64_t  s_cb2tx_sum_us = 0;
                static int64_t  s_cb2tx_min_us = INT64_MAX;
                static int64_t  s_cb2tx_max_us = 0;
                for (int i = 0; i < batch_n; i++) {
                    if (!send_rs[i]) {
                        continue;
                    }
                    inflight_urb_t *u = batch[i];
                    if (u->t_cb == 0) {
                        continue;
                    }
                    int64_t dt = t_pre - u->t_cb;
                    s_cb2tx_count++;
                    s_cb2tx_sum_us += dt;
                    if (dt < s_cb2tx_min_us) s_cb2tx_min_us = dt;
                    if (dt > s_cb2tx_max_us) s_cb2tx_max_us = dt;
                    if ((s_cb2tx_count % 100) == 0) {
                        ESP_LOGI(TAG, "cb2tx_timing: n=%" PRIu32
                                      " avg_cb2tx=%" PRId32 "us"
                                      " min_cb2tx=%" PRId32 "us"
                                      " max_cb2tx=%" PRId32 "us",
                                 s_cb2tx_count,
                                 (int32_t)(s_cb2tx_sum_us / (int64_t)s_cb2tx_count),
                                 (int32_t)s_cb2tx_min_us,
                                 (int32_t)s_cb2tx_max_us);
                    }
                }
            }

            if (sent < 0) {
                ok = false;
            }
            /* Partial write: the remaining bytes are lost.  A partial
             * writev on a TCP socket means the connection is closing;
             * shutdown() below will trigger teardown. */
        }

        /* --- Phase 4: per-URB slot-release and retire -------------------- */
        for (int i = 0; i < batch_n; i++) {
            inflight_urb_t *u = batch[i];

            /* Give back the lane's pipeline slot. */
            per_ep_lane_t *lane = &conn->lanes[u->lane_idx];
            if (lane->inflight_slots != NULL) {
                xSemaphoreGive(lane->inflight_slots);
            }

            /* Retire: remove from inflight list, signal cancel waiter. */
            bool free_now = false;
            xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
            inflight_urb_t **p = &conn->inflight_head;
            while (*p != NULL) {
                if (*p == u) {
                    *p = u->next;
                    conn->inflight_count--;
                    if (conn->inflight_count == 0 &&
                        conn->inflight_drain != NULL) {
                        xSemaphoreGive(conn->inflight_drain);
                    }
                    break;
                }
                p = &(*p)->next;
            }
            u->retired = true;
            if (u->cancel_waiters == 0) {
                free_now = true;
            } else if (u->cancel_done_sem != NULL) {
                xSemaphoreGive(u->cancel_done_sem);
            }
            xSemaphoreGive(conn->inflight_mutex);

            if (free_now) {
                inflight_free(u);
            }
        }

        if (!ok) {
            shutdown(conn->fd, SHUT_RDWR);
        }
    }

    if (conn->responder_alive != NULL) {
        xSemaphoreGive(conn->responder_alive);
    }
    vTaskDeleteWithCaps(NULL);
}

/* Lazy-spawn a lane for (ep,dir) identified by idx, then enqueue u.
 * Called from intake_submit under no mutex. Returns true on success.
 * On failure the caller must handle the URB error (inflight_free + error
 * response). */
static bool lane_dispatch(conn_state_t *conn, inflight_urb_t *u)
{
    uint8_t idx = u->lane_idx;

    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    per_ep_lane_t *lane = &conn->lanes[idx];

    if (lane->task == NULL) {
        /* First URB on this (ep,dir): create the queue and spawn the task. */
        lane->queue = xQueueCreate(USBIP_INFLIGHT_MAX, sizeof(inflight_urb_t *));
        if (lane->queue == NULL) {
            xSemaphoreGive(conn->inflight_mutex);
            return false;
        }

        /* Counting semaphore bounds concurrent in-flight URBs per pipe.
         * The lane task takes one slot before each async submit; the
         * responder gives it back after each RET_SUBMIT. */
        lane->inflight_slots = xSemaphoreCreateCounting(USBIP_PIPELINE_DEPTH,
                                                         USBIP_PIPELINE_DEPTH);
        if (lane->inflight_slots == NULL) {
            vQueueDelete(lane->queue);
            lane->queue = NULL;
            xSemaphoreGive(conn->inflight_mutex);
            return false;
        }

        lane_task_arg_t *la = malloc(sizeof(*la));
        if (la == NULL) {
            vSemaphoreDelete(lane->inflight_slots);
            lane->inflight_slots = NULL;
            vQueueDelete(lane->queue);
            lane->queue = NULL;
            xSemaphoreGive(conn->inflight_mutex);
            return false;
        }
        la->conn = conn;
        la->idx  = idx;

        conn->refcount++;
        lane->alive = true;
        conn->lanes_alive++;

        char tname[16];
        snprintf(tname, sizeof(tname), "usbip_l_%02x", (unsigned)idx);

        if (xTaskCreatePinnedToCoreWithCaps(lane_task, tname,
                USBIP_LANE_TASK_STACK, la,
                USBIP_WORKER_TASK_PRIORITY, &lane->task,
                USBIP_TASK_CORE,
                USBIP_TASK_STACK_CAPS) != pdPASS) {
            /* Undo the accounting we just did. */
            conn->refcount--;
            lane->alive = false;
            conn->lanes_alive--;
            vSemaphoreDelete(lane->inflight_slots);
            lane->inflight_slots = NULL;
            vQueueDelete(lane->queue);
            lane->queue = NULL;
            free(la);
            xSemaphoreGive(conn->inflight_mutex);
            return false;
        }
    }
    xSemaphoreGive(conn->inflight_mutex);

    if (xQueueSend(lane->queue, &u, 0) != pdTRUE) {
        /* Lane queue full - should not happen with USBIP_INFLIGHT_MAX cap,
         * but handle gracefully. */
        ESP_LOGW(TAG, "lane queue[%u] full, seq=%" PRIu32, (unsigned)idx,
                 u->hdr.seqnum);
        return false;
    }
    return true;
}

/* Read one URB worth of OUT-stage data into a fresh malloc buffer. */
static bool read_out_payload(int fd, size_t len, uint8_t **out_buf)
{
    *out_buf = NULL;
    if (len == 0) {
        return true;
    }
    uint8_t *buf = malloc(len);
    if (buf == NULL) {
        (void)discard_exact(fd, len);
        return false;
    }
    if (!read_exact(fd, buf, len)) {
        free(buf);
        return false;
    }
    *out_buf = buf;
    return true;
}

/* Validate, build, and either run inline (synthetic) or queue for
 * worker dispatch (real). The connection's read-loop calls this. */
static bool intake_submit(conn_state_t *conn,
                          const usbip_decoded_header_t *hdr,
                          const char busid[USBIP_BUSID_SIZE],
                          uint32_t expected_devid,
                          bool is_virtual)
{
    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbip_in: busid=%.32s ep=%" PRIu32 " dir=%s len=%" PRIu32
                      " seq=%" PRIu32,
                 busid, hdr->ep,
                 (hdr->direction == USBIP_DIR_IN) ? "IN" : "OUT",
                 hdr->transfer_buffer_length, hdr->seqnum);
    }

    int v = usbip_proto_validate_submit(hdr, s_state.max_transfer);
    if (v == -EINVAL && (hdr->direction != USBIP_DIR_OUT &&
                          hdr->direction != USBIP_DIR_IN)) {
        ESP_LOGW(TAG, "  -> bad direction %" PRIu32 ", dropping conn", hdr->direction);
        return false;
    }
    if (v == -EMSGSIZE) {
        if (hdr->direction == USBIP_DIR_OUT && hdr->transfer_buffer_length > 0) {
            if (!discard_exact(conn->fd, (size_t)hdr->transfer_buffer_length)) {
                return false;
            }
        }
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -EMSGSIZE, NULL, 0, "intake_emsgsize");
    }
    if (v == -EINVAL) {
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -EINVAL, NULL, 0, "intake_einval");
    }
    if (v == -EOPNOTSUPP) {
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -EOPNOTSUPP, NULL, 0, "intake_eopnotsupp");
    }

    if (hdr->devid != expected_devid) {
        ESP_LOGW(TAG, "  -> ENODEV (devid 0x%08" PRIx32 " != expected 0x%08" PRIx32 ")",
                 hdr->devid, expected_devid);
        if (hdr->direction == USBIP_DIR_OUT && hdr->transfer_buffer_length > 0) {
            if (!discard_exact(conn->fd, (size_t)hdr->transfer_buffer_length)) {
                return false;
            }
        }
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -ENODEV, NULL, 0, "intake_enodev");
    }

    /* Setup direction sanity for control. */
    if (hdr->ep == 0) {
        const bool setup_in = (hdr->setup[0] & USBIP_REQUEST_DIR_IN) != 0;
        if ((hdr->direction == USBIP_DIR_IN) != setup_in) {
            if (hdr->direction == USBIP_DIR_OUT && hdr->transfer_buffer_length > 0) {
                if (!discard_exact(conn->fd, (size_t)hdr->transfer_buffer_length)) {
                    return false;
                }
            }
            return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                                 hdr->ep, -EINVAL, NULL, 0, "intake_setup_dir");
        }
    }

    inflight_urb_t *u = calloc(1, sizeof(*u));
    if (u == NULL) {
        if (hdr->direction == USBIP_DIR_OUT && hdr->transfer_buffer_length > 0) {
            (void)discard_exact(conn->fd, (size_t)hdr->transfer_buffer_length);
        }
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -ENOMEM, NULL, 0, "intake_enomem_calloc");
    }
    u->hdr = *hdr;
    memcpy(u->busid, busid, USBIP_BUSID_SIZE);
    u->expected_devid = expected_devid;
    u->is_virtual = is_virtual;
    u->conn = conn;
    u->cancel_done_sem = xSemaphoreCreateBinary();
    if (u->cancel_done_sem == NULL) {
        free(u);
        if (hdr->direction == USBIP_DIR_OUT && hdr->transfer_buffer_length > 0) {
            (void)discard_exact(conn->fd, (size_t)hdr->transfer_buffer_length);
        }
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -ENOMEM, NULL, 0, "intake_enomem_sem");
    }

    if (hdr->direction == USBIP_DIR_OUT && hdr->transfer_buffer_length > 0) {
        u->out_len = (size_t)hdr->transfer_buffer_length;
        if (!read_out_payload(conn->fd, u->out_len, &u->out_buf)) {
            free(u);
            return false;
        }
    }
    if (hdr->direction == USBIP_DIR_IN && hdr->transfer_buffer_length > 0) {
        u->in_capacity = (size_t)hdr->transfer_buffer_length;
        u->in_buf = malloc(u->in_capacity);
        if (u->in_buf == NULL) {
            free(u->out_buf);
            free(u);
            return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                                 hdr->ep, -ENOMEM, NULL, 0, "intake_enomem_inbuf");
        }
    }

    /* Block waiting for an inflight slot if the kernel is way out
     * over its skis. The read loop staying single-threaded gives us
     * back-pressure for free here. */
    while (true) {
        xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
        bool full = (conn->inflight_count >= USBIP_INFLIGHT_MAX);
        xSemaphoreGive(conn->inflight_mutex);
        if (!full) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }

    /* Compute lane index (5-bit ep/dir fold) for lane_dispatch. */
    u->lane_idx = lane_index(hdr->ep, hdr->direction);

    inflight_link(conn, u);

    if (is_virtual) {
        /* Synthetic devices: microsecond latency, no contention.
         * Inline dispatch keeps the regression baseline simple. */
        run_inflight(u);
        return true;
    }

    /* Real-host URBs (EP0, OUT, and IN all go through per-EP lanes).
     * Each lane serialises its (ep,dir) pipe by construction: one task
     * processes URBs in FIFO order, so submit order matches arrival
     * order without ticket gates (removed in steps 2-3). */
    if (!lane_dispatch(conn, u)) {
        ESP_LOGW(TAG, "lane_dispatch failed, seq=%" PRIu32, hdr->seqnum);
        inflight_unlink(conn, u);
        bool ok2 = tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                                 hdr->ep, -EBUSY, NULL, 0, "intake_ebusy_lane");
        inflight_free(u);
        return ok2;
    }
    return true;
}

static bool handle_urb_stream(conn_state_t *conn,
                              const char busid[USBIP_BUSID_SIZE],
                              uint32_t expected_devid)
{
    const bool is_virtual = (usbip_find_virtual_device(busid) != NULL);

    while (true) {
        usbip_header_t raw;
        if (!read_exact(conn->fd, &raw, sizeof(raw))) {
            return false;
        }

        usbip_decoded_header_t hdr;
        usbip_proto_unpack_header(&raw, &hdr);

        if (hdr.command == USBIP_CMD_SUBMIT) {
            if (!intake_submit(conn, &hdr, busid, expected_devid, is_virtual)) {
                return false;
            }
        } else if (hdr.command == USBIP_CMD_UNLINK) {
            /* USB/IP cancel ordering: signal the worker, wait for it
             * to send RET_SUBMIT (status -ECONNRESET) which gives the
             * URB back via vhci_rx's priv_rx path, THEN send our
             * RET_UNLINK. Sending RET_UNLINK first while the URB is
             * still on priv_rx leaves usb_kill_urb spinning because
             * the kernel cannot find the seqnum on either list. */
            if (s_urb_verbose) {
                ESP_LOGI(TAG, "RX_UNLINK seq=%" PRIu32 " unlink_seq=%" PRIu32,
                         hdr.seqnum, hdr.unlink_seqnum);
            }
            inflight_urb_t *u = inflight_begin_cancel(conn, hdr.unlink_seqnum);
            bool send_ret_unlink = true;
            const char *site = "unlink_no_match";
            /* RET_UNLINK status semantics. The kernel's vhci_recv_ret_unlink
             * propagates pdu->u.ret_unlink.status into urb->status before
             * giving the URB back. status=0 means "URB completed normally
             * with 0 bytes", which cdc-acm interprets as a successful but
             * empty control transfer; that desyncs its state machine.
             * For an URB we genuinely cancelled mid-flight the correct
             * status is -ECONNRESET. For a URB that was already retired
             * (RET_SUBMIT sent), the kernel will fail the priv_rx pickup
             * and just log "already given back"; status is irrelevant
             * there but -ECONNRESET is still spec-correct. */
            int32_t unlink_status = -ECONNRESET;
            if (u != NULL) {
                /* R22: force the in-flight URB to complete by halting,
                 * flushing, and clearing the endpoint. The IDF then
                 * delivers the URB to lane_completion_cb with
                 * USB_TRANSFER_STATUS_CANCELED; the responder sends
                 * RET_SUBMIT(-ECONNRESET) and gives the cancel_done_sem.
                 * This replaces the old submit_xfer cancel polling loop.
                 * The per-EP submit mutex inside usbhost_cancel_ep
                 * serialises against any concurrent submit on the same EP. */
                if (!u->is_virtual) {
                    uint8_t ep_addr = (u->hdr.ep == 0) ? 0x00
                        : (uint8_t)(u->hdr.ep |
                              (u->hdr.direction == USBIP_DIR_IN ? 0x80u : 0x00u));
                    usbhost_cancel_ep(u->busid, ep_addr);
                }
                /* 250 ms ceiling: usbhost halt+flush+clear drives IDF
                 * completion within ~50 ms in practice. The read loop
                 * handles one UNLINK at a time so a tight cap keeps
                 * the close-storm of ~16 UNLINKs from stalling the
                 * connection for many seconds. */
                BaseType_t took = xSemaphoreTake(u->cancel_done_sem, pdMS_TO_TICKS(250));
                /* Claim RET_UNLINK ownership iff the worker has not
                 * already claimed RET_SUBMIT. If the worker won, the
                 * URB has already been given back via RET_SUBMIT and
                 * we must not send RET_UNLINK; vhci_rx logs "cannot
                 * find a urb of seqnum N" and tears down the
                 * connection if both arrive. */
                xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
                int prior_owner = (int)u->tx_owner;
                if (u->tx_owner == TX_OWNER_NONE) {
                    u->tx_owner = TX_OWNER_RET_UNLINK;
                } else {
                    send_ret_unlink = false;
                }
                xSemaphoreGive(conn->inflight_mutex);
                if (s_urb_verbose) {
                    ESP_LOGI(TAG, "UNLINK_CLAIM seq=%" PRIu32 " unlink_seq=%" PRIu32
                                  " sem=%d prior_owner=%d send=%d",
                             hdr.seqnum, hdr.unlink_seqnum,
                             (int)took, prior_owner, send_ret_unlink ? 1 : 0);
                }
                site = (took == pdTRUE) ? "unlink_after_worker" : "unlink_timeout";
                inflight_release_after_cancel(conn, u);
            } else {
                if (s_urb_verbose) {
                    ESP_LOGI(TAG, "UNLINK_NO_MATCH seq=%" PRIu32 " unlink_seq=%" PRIu32,
                             hdr.seqnum, hdr.unlink_seqnum);
                }
            }
            if (send_ret_unlink) {
                if (!tx_ret_unlink(conn, hdr.seqnum, hdr.devid,
                                   hdr.direction, hdr.ep, unlink_status, site)) {
                    return false;
                }
            }
        } else {
            ESP_LOGW(TAG, "Unsupported USB/IP command 0x%08" PRIx32, hdr.command);
            return false;
        }
    }
}

static bool handle_import_request(int fd, size_t *slot_idx, bool *slot_held)
{
    char busid[USBIP_BUSID_SIZE];
    if (!read_exact(fd, busid, sizeof(busid))) {
        return false;
    }
    ESP_LOGI(TAG, "IMPORT: requested busid='%.32s'", busid);

    usbip_dev_record_t device;
    bool found = find_device_by_busid(busid, &device);

    if (!found) {
        (void)send_op_common(fd, USBIP_OP_REP_IMPORT, 1);
        return true;
    }

    size_t my_slot = 0;
    if (!attachment_acquire(fd, busid, &my_slot)) {
        ESP_LOGW(TAG, "IMPORT: busid '%.32s' already attached, refusing", busid);
        (void)send_op_common(fd, USBIP_OP_REP_IMPORT, 1);
        return true;
    }
    *slot_idx = my_slot;
    *slot_held = true;

    if (!send_op_common(fd, USBIP_OP_REP_IMPORT, 0)) {
        return false;
    }

    usbip_device_desc_t wire;
    usbip_proto_pack_device_desc(&device, &wire);
    if (!write_all(fd, &wire, sizeof(wire))) {
        return false;
    }

    virtual_device_t *vdev = usbip_find_virtual_device(busid);
    if (vdev && vdev->ops->on_attach) {
        if (vdev->ops->on_attach(vdev) != 0) {
            return false;
        }
    }

    /* Stand up the per-connection async URB plumbing on the heap. The
     * struct outlives this function because a wedged lane task (one
     * whose IDF submit never completes) would otherwise dereference freed
     * stack memory after handle_import_request returns. Refcount: 1 for
     * the read loop plus 1 per spawned lane task. */
    conn_state_t *conn = calloc(1, sizeof(*conn));
    if (conn == NULL) {
        if (vdev && vdev->ops->on_detach) {
            vdev->ops->on_detach(vdev);
        }
        return false;
    }
    conn->fd              = fd;
    conn->shutdown        = false;
    conn->tx_mutex        = xSemaphoreCreateMutex();
    conn->inflight_mutex  = xSemaphoreCreateMutex();
    conn->inflight_drain  = xSemaphoreCreateBinary();
    conn->lanes_done      = xSemaphoreCreateBinary();
    /* R22: responder queue sized at USBIP_INFLIGHT_MAX * 2 to cover two
     * EPs' completions across both directions in worst case. */
    conn->responder_queue = xQueueCreate(USBIP_INFLIGHT_MAX * 2,
                                         sizeof(inflight_urb_t *));
    conn->responder_alive = xSemaphoreCreateBinary();
    conn->refcount        = 1;

    bool plumbing_ok = (conn->tx_mutex != NULL && conn->inflight_mutex != NULL &&
                        conn->inflight_drain != NULL && conn->lanes_done != NULL &&
                        conn->responder_queue != NULL && conn->responder_alive != NULL);

    /* Spawn the responder task for real-host connections. The responder
     * blocks on responder_queue waiting for completions. In step 2 the
     * queue is never populated (step 3 wires it up); the task just idles
     * and exits on the NULL sentinel posted in teardown. */
    if (plumbing_ok) {
        const virtual_device_t *vdev_check = usbip_find_virtual_device(busid);
        if (vdev_check == NULL) {
            /* Real-host path: spawn responder. */
            BaseType_t rrc = xTaskCreatePinnedToCoreWithCaps(
                responder_task, "usbip_resp",
                USBIP_RESPONDER_TASK_STACK, conn,
                USBIP_RESPONDER_TASK_PRIORITY,
                &conn->responder_task,
                USBIP_TASK_CORE, USBIP_TASK_STACK_CAPS);
            if (rrc != pdPASS) {
                ESP_LOGW(TAG, "spawn responder_task failed");
                plumbing_ok = false;
            }
        }
    }

    bool ok = false;
    if (plumbing_ok) {
        ok = handle_urb_stream(conn, busid, usbip_proto_make_devid(&device));
    }

    /* Tear down: signal cancellation for any in-flight URBs so each
     * lane task's submit_xfer returns quickly via halt+flush+clear. */
    conn->shutdown = true;
    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    for (inflight_urb_t *u = conn->inflight_head; u != NULL; u = u->next) {
        u->cancel = true;
    }
    xSemaphoreGive(conn->inflight_mutex);

    /* Release the attachment slot now: the kernel-side connection has
     * dropped, the device is no longer "attached" from any client's
     * point of view. Holding the slot through the cancel-storm drain
     * (up to 11 s) blocks a fresh IMPORT for the same busid with
     * "already attached, refusing". The conn_state is heap-allocated
     * and refcounted, so lane tasks still draining safely keep their
     * own references; a parallel new IMPORT spawns its own conn_state. */
    if (slot_held != NULL && *slot_held && slot_idx != NULL) {
        attachment_release(*slot_idx);
        *slot_held = false;
    }

    if (outstanding_drain_wait(conn, pdMS_TO_TICKS(3000)) != 0) {
        ESP_LOGW(TAG, "teardown: inflight drain timed out, count=%d",
                 conn->inflight_count);
    }

    /* Drain per-EP lane tasks. Post a NULL sentinel to each alive lane
     * and wait for all lane tasks to exit. Lanes that were never spawned
     * have task==NULL and alive==false; skip them. If any lane task is
     * wedged inside an IDF call the 8 s timeout fires and we drop our
     * reference; the wedged task still holds one and frees conn
     * when it eventually exits. */
    int lanes_to_drain = 0;
    for (int i = 0; i < 32; i++) {
        xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
        bool do_sentinel = conn->lanes[i].alive && conn->lanes[i].queue != NULL;
        xSemaphoreGive(conn->inflight_mutex);
        if (do_sentinel) {
            inflight_urb_t *sentinel = NULL;
            xQueueSend(conn->lanes[i].queue, &sentinel, portMAX_DELAY);
            lanes_to_drain++;
        }
    }
    if (lanes_to_drain > 0) {
        if (xSemaphoreTake(conn->lanes_done, pdMS_TO_TICKS(8000)) != pdTRUE) {
            xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
            int alive = conn->lanes_alive;
            xSemaphoreGive(conn->inflight_mutex);
            ESP_LOGW(TAG, "teardown: lanes_alive=%d after 8s wait", alive);
        }
    }

    /* Drain the responder task. Post NULL sentinel; wait up to 2 s.
     * All lane tasks have exited above, so no new completions can arrive.
     * Any completions already queued will be processed before the sentinel. */
    if (conn->responder_task != NULL && conn->responder_queue != NULL) {
        inflight_urb_t *resp_sentinel = NULL;
        xQueueSend(conn->responder_queue, &resp_sentinel, portMAX_DELAY);
        if (conn->responder_alive != NULL) {
            if (xSemaphoreTake(conn->responder_alive, pdMS_TO_TICKS(2000)) != pdTRUE) {
                ESP_LOGW(TAG, "teardown: responder still alive after 2s wait");
            }
        }
    }

    if (vdev && vdev->ops->on_detach) {
        vdev->ops->on_detach(vdev);
    }

    /* Drop our reference. If all workers have already exited, this is
     * the last reference and conn_state_free frees everything. */
    conn_state_release(conn);
    return ok;
}

/* Free conn_state_t and all attached resources. Called only when the
 * refcount hits zero, so no other task is using any of these. */
static void conn_state_free(conn_state_t *conn)
{
    /* Free any straggler inflight records. With refcount-driven destroy
     * we can only reach here once every lane task has dropped its ref,
     * so the inflight list contains URBs not yet dequeued at teardown. */
    inflight_urb_t *u = conn->inflight_head;
    while (u != NULL) {
        inflight_urb_t *next = u->next;
        inflight_free(u);
        u = next;
    }
    /* Free any lane queues and inflight_slots that were created (tasks already exited). */
    for (int i = 0; i < 32; i++) {
        if (conn->lanes[i].queue != NULL) {
            vQueueDelete(conn->lanes[i].queue);
            conn->lanes[i].queue = NULL;
        }
        if (conn->lanes[i].inflight_slots != NULL) {
            vSemaphoreDelete(conn->lanes[i].inflight_slots);
            conn->lanes[i].inflight_slots = NULL;
        }
    }
    if (conn->tx_mutex)        vSemaphoreDelete(conn->tx_mutex);
    if (conn->inflight_mutex)  vSemaphoreDelete(conn->inflight_mutex);
    if (conn->inflight_drain)  vSemaphoreDelete(conn->inflight_drain);
    if (conn->lanes_done)      vSemaphoreDelete(conn->lanes_done);
    if (conn->responder_queue) vQueueDelete(conn->responder_queue);
    if (conn->responder_alive) vSemaphoreDelete(conn->responder_alive);
    free(conn);
}

/* ---------- accept loop ---------- */

static void handle_client(int fd, size_t *slot_idx, bool *slot_held)
{
    ESP_LOGI(TAG, "handle_client: fd=%d", fd);
    usbip_op_common_t op_raw;
    if (!read_exact(fd, &op_raw, sizeof(op_raw))) {
        ESP_LOGW(TAG, "handle_client: fd=%d op_common read failed", fd);
        return;
    }
    uint16_t version, code;
    uint32_t status;
    usbip_proto_unpack_op_common(&op_raw, &version, &code, &status);

    if (version != USBIP_VERSION) {
        ESP_LOGW(TAG, "Unsupported USB/IP version 0x%04x", version);
        return;
    }
    if (code == USBIP_OP_REQ_DEVLIST) {
        (void)handle_devlist_request(fd);
        return;
    }
    if (code == USBIP_OP_REQ_IMPORT) {
        (void)handle_import_request(fd, slot_idx, slot_held);
        return;
    }
    ESP_LOGW(TAG, "Unsupported op 0x%04x", code);
}

typedef struct {
    int fd;
} client_task_arg_t;

static void client_task(void *arg)
{
    client_task_arg_t *ctx = (client_task_arg_t *)arg;
    int fd = ctx->fd;
    free(ctx);
    ESP_LOGI(TAG, "client_task: fd=%d", fd);

    size_t slot_idx = 0;
    bool   slot_held = false;
    handle_client(fd, &slot_idx, &slot_held);
    if (slot_held) {
        attachment_release(slot_idx);
    }
    /* Force a FIN on close so the kernel sees the disconnect even if
     * any cached state is hanging on a half-closed socket. */
    shutdown(fd, SHUT_RDWR);
    close(fd);
    ESP_LOGI(TAG, "Client disconnected");
    vTaskDeleteWithCaps(NULL);
}

static void usbip_server_task(void *arg)
{
    uint16_t port = (uint16_t)(uintptr_t)arg;

    int listen_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (listen_fd < 0) {
        ESP_LOGE(TAG, "socket() failed: errno=%d", errno);
        s_state.running = false;
        vTaskDelete(NULL);
        return;
    }
    int opt = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGE(TAG, "bind() failed on port %u: errno=%d", port, errno);
        close(listen_fd);
        s_state.running = false;
        vTaskDelete(NULL);
        return;
    }
    if (listen(listen_fd, 4) < 0) {
        ESP_LOGE(TAG, "listen() failed: errno=%d", errno);
        close(listen_fd);
        s_state.running = false;
        vTaskDelete(NULL);
        return;
    }

    s_state.listen_fd = listen_fd;
    s_state.running = true;
    ESP_LOGI(TAG, "USB/IP server listening on TCP %u", port);

    while (s_state.running) {
        struct sockaddr_in client_addr;
        socklen_t client_len = sizeof(client_addr);
        int client_fd = accept(listen_fd, (struct sockaddr *)&client_addr, &client_len);
        if (client_fd < 0) {
            if (!s_state.running) {
                break;
            }
            ESP_LOGW(TAG, "accept() failed: errno=%d", errno);
            continue;
        }
        ESP_LOGI(TAG, "accept: fd=%d from=%s:%u", client_fd,
                 inet_ntoa(client_addr.sin_addr),
                 (unsigned)ntohs(client_addr.sin_port));

        int nodelay = 1;
        setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

        client_task_arg_t *cargs = malloc(sizeof(*cargs));
        if (cargs == NULL) {
            close(client_fd);
            continue;
        }
        cargs->fd = client_fd;
        BaseType_t xrc = xTaskCreatePinnedToCoreWithCaps(client_task, "usbip_client",
                                    USBIP_CLIENT_TASK_STACK, cargs,
                                    USBIP_CLIENT_TASK_PRIORITY, NULL,
                                    USBIP_TASK_CORE,
                                    USBIP_TASK_STACK_CAPS);
        if (xrc != pdPASS) {
            ESP_LOGW(TAG, "xTaskCreatePinnedToCore(client_task) failed rc=%d fd=%d",
                     (int)xrc, client_fd);
            free(cargs);
            close(client_fd);
        } else {
            ESP_LOGI(TAG, "spawned client_task for fd=%d", client_fd);
        }
    }

    close(listen_fd);
    s_state.listen_fd = -1;
    s_state.running = false;
    s_state.task = NULL;
    ESP_LOGI(TAG, "USB/IP server stopped");
    vTaskDelete(NULL);
}

/* ---------- public API ---------- */

int usbip_server_start(uint16_t port)
{
    if (s_state.running) {
        return 0;
    }
    if (s_state.attach_lock == NULL) {
        s_state.attach_lock = xSemaphoreCreateMutex();
        if (s_state.attach_lock == NULL) {
            return -ENOMEM;
        }
        for (size_t i = 0; i < USBIP_MAX_CLIENTS; i++) {
            s_state.slots[i].in_use = false;
            s_state.slots[i].fd = -1;
            memset(s_state.slots[i].busid, 0, USBIP_BUSID_SIZE);
        }
    }

    (void)usbhost_start();

    if (port == 0) {
        port = USBIP_TCP_PORT;
    }

    BaseType_t rc = xTaskCreatePinnedToCore(
        usbip_server_task, "usbip_server",
        USBIP_SERVER_TASK_STACK,
        (void *)(uintptr_t)port,
        USBIP_SERVER_TASK_PRIORITY,
        &s_state.task,
        USBIP_TASK_CORE);
    if (rc != pdPASS) {
        return -ENOMEM;
    }
    return 0;
}

int usbip_server_stop(void)
{
    if (!s_state.running) {
        return 0;
    }
    s_state.running = false;
    if (s_state.listen_fd >= 0) {
        shutdown(s_state.listen_fd, SHUT_RDWR);
        close(s_state.listen_fd);
        s_state.listen_fd = -1;
    }
    return 0;
}

bool usbip_server_is_running(void)
{
    return s_state.running;
}

int usbip_server_register_virtual_device(virtual_device_t *dev)
{
    return usbip_register_virtual_device(dev);
}

int32_t usbip_server_max_transfer(void)
{
    return s_state.max_transfer;
}

#else /* MPY_POD_HOST_TEST_BUILD */

#include <stdio.h>

int usbip_server_start(uint16_t port)
{
    (void)port;
    return 0;
}

int usbip_server_stop(void)
{
    return 0;
}

bool usbip_server_is_running(void)
{
    return false;
}

int usbip_server_register_virtual_device(virtual_device_t *dev)
{
    return usbip_register_virtual_device(dev);
}

int32_t usbip_server_max_transfer(void)
{
    return 16 * 1024;
}

size_t usbip_server_attached_busids(char (*out)[USBIP_BUSID_SIZE], size_t max)
{
    (void)out;
    (void)max;
    return 0;
}

void usbip_server_set_verbose(bool enable) { (void)enable; }
bool usbip_server_is_verbose(void) { return false; }

#endif /* MPY_POD_HOST_TEST_BUILD */
