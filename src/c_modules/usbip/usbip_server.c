/* Annealage Pod: USB/IP multiplexer server.
 *
 * Concurrency model (R15 refactor):
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
 *   connection seqnum table, and posts it to a worker queue. A
 *   small pool of submit workers per connection pulls records,
 *   calls usbhost_*_transfer (which is now async at the IDF layer
 *   so multiple URBs can be in flight concurrently), then sends
 *   RET_SUBMIT under a per-connection tx_mutex.
 *
 *   Synthetic devices (CMSIS-DAP) take a fast path: latency is
 *   microseconds, contention is impossible, and the simpler inline
 *   dispatch keeps the regression baseline well-trodden.
 *
 *   CMD_UNLINK is real: the seqnum table is consulted, the cancel
 *   flag is flipped, the worker observes it via usbhost's halt+
 *   flush+clear path and the URB completes with -ECONNRESET. The
 *   worker sends RET_SUBMIT first; the read loop sends RET_UNLINK
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
#include "esp_log.h"

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

#define USBIP_TASK_CORE 1

#define USBIP_MAX_TRANSFER_DEFAULT (16 * 1024)

#define USBIP_MAX_CLIENTS 4

/* Per-connection submit-worker pool size. cdc-acm under load keeps up
 * to 16 bulk-IN reads pending plus an interrupt-IN; pool must be at
 * least this large. OUT URBs bypass the pool (run inline from the
 * read loop) so they never block on a long-pending IN. */
#define USBIP_SUBMIT_POOL_SIZE 24

/* Per-connection inflight URB cap; cdc-acm queues 16 reads + a few
 * writes + interrupt + control. 32 covers worst case. */
#define USBIP_INFLIGHT_MAX 32

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
    volatile bool          cancel;
    volatile bool          retired; /* true once RET_SUBMIT has been sent */
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
} inflight_urb_t;

typedef struct conn_state {
    int                fd;
    bool               shutdown;        /* stop workers cleanly on disconnect */
    SemaphoreHandle_t  tx_mutex;        /* serialise writes to the TCP socket */
    SemaphoreHandle_t  inflight_mutex;  /* protects the inflight list */
    QueueHandle_t      submit_queue;    /* inflight_urb_t* drained by workers */
    inflight_urb_t    *inflight_head;
    int                inflight_count;
    SemaphoreHandle_t  inflight_drain;  /* given when count drops to 0 */
    volatile int       workers_alive;   /* count of pool tasks still running */
    SemaphoreHandle_t  workers_done;    /* given when workers_alive drops to 0 */
} conn_state_t;

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
 * on the wire. Returns false if the socket dropped. */
static bool tx_ret_submit(conn_state_t *conn,
                          uint32_t seqnum, uint32_t devid,
                          uint32_t direction, uint32_t ep,
                          int32_t status,
                          const uint8_t *payload, uint32_t payload_len)
{
    usbip_header_t reply;
    usbip_proto_pack_ret_submit(&reply, seqnum, devid, direction, ep,
                                status, payload_len);

    xSemaphoreTake(conn->tx_mutex, portMAX_DELAY);
    bool ok;
    if (payload_len > 0 && payload != NULL) {
        uint8_t *buf = malloc(sizeof(reply) + payload_len);
        if (buf == NULL) {
            xSemaphoreGive(conn->tx_mutex);
            return false;
        }
        memcpy(buf, &reply, sizeof(reply));
        memcpy(buf + sizeof(reply), payload, payload_len);
        ok = write_all(conn->fd, buf, sizeof(reply) + payload_len);
        free(buf);
    } else {
        ok = write_all(conn->fd, &reply, sizeof(reply));
    }
    xSemaphoreGive(conn->tx_mutex);
    return ok;
}

static bool tx_ret_unlink(conn_state_t *conn,
                          uint32_t seqnum, uint32_t devid,
                          uint32_t direction, uint32_t ep,
                          int32_t status)
{
    usbip_header_t reply;
    usbip_proto_pack_ret_unlink(&reply, seqnum, devid, direction, ep, status);
    xSemaphoreTake(conn->tx_mutex, portMAX_DELAY);
    bool ok = write_all(conn->fd, &reply, sizeof(reply));
    xSemaphoreGive(conn->tx_mutex);
    return ok;
}

/* Run a single inflight URB end-to-end: backend dispatch + RET_SUBMIT.
 * Caller is either the read loop (synthetic fast path) or a worker
 * (real-host). The function frees the inflight buffers on return. */
static void run_inflight(inflight_urb_t *u)
{
    conn_state_t *conn = u->conn;
    const usbip_decoded_header_t *hdr = &u->hdr;

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbip_dispatch: busid=%.32s ep=%" PRIu32 " target=%s",
                 u->busid, hdr->ep, u->is_virtual ? "virtual" : "host");
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
                      " status=%d actual=%u%s",
                 u->busid, hdr->ep, status, (unsigned)in_len,
                 was_cancelled ? " (cancelled)" : "");
    }

    bool ok = tx_ret_submit(conn,
                            hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                            status,
                            (status == 0 && hdr->direction == USBIP_DIR_IN) ? u->in_buf : NULL,
                            (status == 0 && hdr->direction == USBIP_DIR_IN)
                              ? (uint32_t)in_len : 0);

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbip_out: busid=%.32s ep=%" PRIu32 " ok=%d",
                 u->busid, hdr->ep, ok ? 1 : 0);
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

/* Worker: pull inflight URBs from the per-connection queue, run them,
 * exit when shutdown is signalled (queue closed by submit of NULL).
 * The pool only services IN URBs (which may pend long); OUT URBs run
 * inline in intake_submit so they never block behind a pending IN. */
static void submit_worker_task(void *arg)
{
    conn_state_t *conn = (conn_state_t *)arg;
    while (true) {
        inflight_urb_t *u = NULL;
        if (xQueueReceive(conn->submit_queue, &u, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (u == NULL) {
            /* Sentinel: drain time. */
            break;
        }
        run_inflight(u);
    }
    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    int remaining = --conn->workers_alive;
    xSemaphoreGive(conn->inflight_mutex);
    if (remaining == 0 && conn->workers_done != NULL) {
        xSemaphoreGive(conn->workers_done);
    }
    vTaskDelete(NULL);
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
                             hdr->ep, -EMSGSIZE, NULL, 0);
    }
    if (v == -EINVAL) {
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -EINVAL, NULL, 0);
    }
    if (v == -EOPNOTSUPP) {
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -EOPNOTSUPP, NULL, 0);
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
                             hdr->ep, -ENODEV, NULL, 0);
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
                                 hdr->ep, -EINVAL, NULL, 0);
        }
    }

    inflight_urb_t *u = calloc(1, sizeof(*u));
    if (u == NULL) {
        if (hdr->direction == USBIP_DIR_OUT && hdr->transfer_buffer_length > 0) {
            (void)discard_exact(conn->fd, (size_t)hdr->transfer_buffer_length);
        }
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -ENOMEM, NULL, 0);
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
                             hdr->ep, -ENOMEM, NULL, 0);
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
                                 hdr->ep, -ENOMEM, NULL, 0);
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

    inflight_link(conn, u);

    if (is_virtual) {
        /* Synthetic devices: microsecond latency, no contention.
         * Inline dispatch keeps the regression baseline simple. */
        run_inflight(u);
        return true;
    }

    /* EP0 control transfers complete in milliseconds and never pend.
     * Routing them through the worker pool puts them behind pending
     * bulk-IN reads, which is the deadlock cdc-acm hit before this
     * fix: SET_CONTROL_LINE_STATE waited behind 16 pending bulk reads
     * that would only complete once the device saw line state set.
     *
     * Bulk/interrupt OUT URBs also run inline. The kernel emits a
     * write only when it has data ready and expects ACK quickly; the
     * IDF returns within milliseconds. Inlining prevents head-of-line
     * blocking when all worker pool slots are occupied by pending IN
     * URBs (cdc-acm keeps 16 read URBs queued). */
    if (hdr->ep == 0 || hdr->direction == USBIP_DIR_OUT) {
        run_inflight(u);
        return true;
    }

    if (xQueueSend(conn->submit_queue, &u, 0) != pdTRUE) {
        ESP_LOGW(TAG, "submit queue saturated, seq=%" PRIu32, hdr->seqnum);
        inflight_unlink(conn, u);
        inflight_free(u);
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -EBUSY, NULL, 0);
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
            inflight_urb_t *u = inflight_begin_cancel(conn, hdr.unlink_seqnum);
            if (u != NULL) {
                /* 250 ms ceiling: usbhost halt+flush+clear drives IDF
                 * completion within ~50 ms in practice. The read loop
                 * handles one UNLINK at a time so a tight cap keeps
                 * the close-storm of ~16 UNLINKs from stalling the
                 * connection for many seconds. If a cancel still
                 * hasn't landed we send RET_UNLINK anyway and let the
                 * worker's eventual RET_SUBMIT unblock the kernel
                 * giveback. */
                (void)xSemaphoreTake(u->cancel_done_sem, pdMS_TO_TICKS(250));
                inflight_release_after_cancel(conn, u);
            }
            if (!tx_ret_unlink(conn, hdr.seqnum, hdr.devid,
                               hdr.direction, hdr.ep, 0)) {
                return false;
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

    /* Stand up the per-connection async URB plumbing. */
    conn_state_t conn = {
        .fd              = fd,
        .shutdown        = false,
        .tx_mutex        = xSemaphoreCreateMutex(),
        .inflight_mutex  = xSemaphoreCreateMutex(),
        .submit_queue    = xQueueCreate(USBIP_INFLIGHT_MAX, sizeof(inflight_urb_t *)),
        .inflight_head   = NULL,
        .inflight_count  = 0,
        .inflight_drain  = xSemaphoreCreateBinary(),
        .workers_alive   = 0,
        .workers_done    = xSemaphoreCreateBinary(),
    };

    bool plumbing_ok = (conn.tx_mutex != NULL && conn.inflight_mutex != NULL &&
                        conn.submit_queue != NULL && conn.inflight_drain != NULL &&
                        conn.workers_done != NULL);

    /* Spawn submit workers for the real-host path. Synthetic devices
     * never need them; running inline avoids the queue hop. */
    int workers_started = 0;
    if (plumbing_ok && vdev == NULL) {
        for (int i = 0; i < USBIP_SUBMIT_POOL_SIZE; i++) {
            xSemaphoreTake(conn.inflight_mutex, portMAX_DELAY);
            conn.workers_alive++;
            xSemaphoreGive(conn.inflight_mutex);
            if (xTaskCreatePinnedToCore(submit_worker_task, "usbip_w",
                                        USBIP_WORKER_TASK_STACK, &conn,
                                        USBIP_WORKER_TASK_PRIORITY, NULL,
                                        USBIP_TASK_CORE) == pdPASS) {
                workers_started++;
            } else {
                xSemaphoreTake(conn.inflight_mutex, portMAX_DELAY);
                conn.workers_alive--;
                xSemaphoreGive(conn.inflight_mutex);
            }
        }
    }

    bool ok = false;
    if (plumbing_ok) {
        ok = handle_urb_stream(&conn, busid, usbip_proto_make_devid(&device));
    }

    /* Tear down: signal cancellation for any in-flight URBs, then
     * drain the queue with NULL sentinels so workers exit. */
    xSemaphoreTake(conn.inflight_mutex, portMAX_DELAY);
    for (inflight_urb_t *u = conn.inflight_head; u != NULL; u = u->next) {
        u->cancel = true;
    }
    int outstanding = conn.inflight_count;
    xSemaphoreGive(conn.inflight_mutex);

    if (outstanding > 0) {
        TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(3000);
        while (true) {
            xSemaphoreTake(conn.inflight_mutex, portMAX_DELAY);
            int n = conn.inflight_count;
            xSemaphoreGive(conn.inflight_mutex);
            if (n == 0) break;
            if (xTaskGetTickCount() >= deadline) break;
            vTaskDelay(pdMS_TO_TICKS(20));
        }
    }

    for (int i = 0; i < workers_started; i++) {
        inflight_urb_t *sentinel = NULL;
        xQueueSend(conn.submit_queue, &sentinel, portMAX_DELAY);
    }
    if (workers_started > 0) {
        xSemaphoreTake(conn.workers_done, pdMS_TO_TICKS(2000));
    }

    /* Free any straggler inflight records. */
    xSemaphoreTake(conn.inflight_mutex, portMAX_DELAY);
    inflight_urb_t *u = conn.inflight_head;
    while (u != NULL) {
        inflight_urb_t *next = u->next;
        inflight_free(u);
        u = next;
    }
    conn.inflight_head = NULL;
    conn.inflight_count = 0;
    xSemaphoreGive(conn.inflight_mutex);

    if (conn.tx_mutex)       vSemaphoreDelete(conn.tx_mutex);
    if (conn.inflight_mutex) vSemaphoreDelete(conn.inflight_mutex);
    if (conn.submit_queue)   vQueueDelete(conn.submit_queue);
    if (conn.inflight_drain) vSemaphoreDelete(conn.inflight_drain);
    if (conn.workers_done)   vSemaphoreDelete(conn.workers_done);

    if (vdev && vdev->ops->on_detach) {
        vdev->ops->on_detach(vdev);
    }
    return ok;
}

/* ---------- accept loop ---------- */

static void handle_client(int fd, size_t *slot_idx, bool *slot_held)
{
    usbip_op_common_t op_raw;
    if (!read_exact(fd, &op_raw, sizeof(op_raw))) {
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
    vTaskDelete(NULL);
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

        int nodelay = 1;
        setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

        client_task_arg_t *cargs = malloc(sizeof(*cargs));
        if (cargs == NULL) {
            close(client_fd);
            continue;
        }
        cargs->fd = client_fd;
        if (xTaskCreatePinnedToCore(client_task, "usbip_client",
                                    USBIP_CLIENT_TASK_STACK, cargs,
                                    USBIP_CLIENT_TASK_PRIORITY, NULL,
                                    USBIP_TASK_CORE) != pdPASS) {
            free(cargs);
            close(client_fd);
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
