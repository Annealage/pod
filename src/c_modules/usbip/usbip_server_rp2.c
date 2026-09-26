/* Annealage Pod RP2350: USB/IP server, lwIP-RAW transport.
 *
 * A single lwIP-RAW callback state machine (tcp_new/bind/listen/accept +
 * tcp_recv/tcp_sent/tcp_poll/tcp_err) rather than tasks and BSD sockets.
 * Forwards the DUT only (busid 1). The wire codec is usbip_proto.c and the
 * device-record types are in usbip_device.h.
 *
 * Threading model (this is the crux of the port):
 *   - lwIP RAW callbacks (accept/recv/sent/poll/err) run at PendSV level on rp2
 *     (MICROPY_PY_LWIP_ENTER == pendsv_suspend). They preempt the main thread;
 *     they are NOT a separate OS thread. So inside a RAW callback we already
 *     hold the lwIP "lock" relative to the main thread and must NOT re-take it
 *     around tcp_* calls (it is a recursive mutex, so re-taking is harmless,
 *     but unnecessary).
 *   - The TinyUSB host (tuh_task) and the URB completion callback run at THREAD
 *     level (the main MicroPython loop), scheduled via mp_sched. Those contexts
 *     are NOT under the lwIP lock, so the completion handler MUST take
 *     MICROPY_PY_LWIP_ENTER/EXIT before any tcp_* call.
 *   - usbhost_submit_async / usbhost_cancel_ep MUST run in tuh_task (thread)
 *     context. A RAW recv callback therefore cannot call them directly; it
 *     stashes the decoded URB and schedules a per-conn mp_sched node whose
 *     callback (thread level) performs the submit/cancel.
 *
 * Use-after-free guard: a URB completion fires at thread level after the conn
 * may have been torn down by tcp_err/abort at PendSV level. The completion
 * handler validates the conn against a small registry under the lwIP lock
 * before touching the pcb; teardown removes the conn from the registry while it
 * holds the lock-equivalent (PendSV is suspended by the completion handler that
 * got there first, and the teardown path runs at PendSV which the completion
 * handler blocks). The URB also carries the conn's epoch; a stale completion
 * whose epoch no longer matches is dropped.
 */

#include "usbip_server.h"
#include "usbip_proto.h"
#include "usbip_protocol.h"
#include "../usbhost/usbhost.h"

#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "py/runtime.h"
#include "py/mphal.h"

#include "lwip/tcp.h"
#include "lwip/pbuf.h"
#include "lwip/err.h"

/* ---------------------------------------------------------------------------
 * Config
 * ------------------------------------------------------------------------- */

/* At most one DUT export plus a couple of transient DEVLIST/refused-IMPORT
 * connections. Each accepted pcb gets a conn_state; this bounds live conns. */
#ifndef USBIP_MAX_CONNS
#define USBIP_MAX_CONNS 4
#endif

/* Single-import policy: busid 1 = DUT is the only exportable device. */
#ifndef USBIP_MAX_CLIENTS
#define USBIP_MAX_CLIENTS 2
#endif

/* TX backlog is served from a fixed pool. The lwIP RAW callbacks run at PendSV
 * on rp2 and must not call libc malloc: MicroPython hands almost all RAM to its
 * GC heap, leaving the libc/sbrk heap tiny, and pico-sdk PICO_MALLOC_PANIC turns
 * an allocation failure into a fatal panic (fatal here because it is taken on the
 * cyw43/lwIP poll path while holding the pendsv lock, which hard-locks the core).
 * conn_tx splits a send across CAP-sized chunks drained by conn_tx_flush as
 * tcp_sndbuf frees. CAP carries the largest control-plane frame (an EP0
 * GET_DESCRIPTOR cache reply, 1 KiB) in a single chunk; SLOTS covers the few
 * chunks live at once given the ~6 KiB TCP send buffer. */
#ifndef USBIP_TX_CHUNK_CAP
#define USBIP_TX_CHUNK_CAP 1024
#endif
#ifndef USBIP_TX_CHUNK_SLOTS
#define USBIP_TX_CHUNK_SLOTS 12
#endif

/* URB inflight records and their data buffers come from static pools (the URB
 * intake runs at PendSV in the recv callback and must not touch libc malloc).
 * Two separately-sized pools: a larger header pool (a queued URB costs only a
 * header; cdc-acm keeps ~16 bulk-IN read URBs pending, plus interrupt + control
 * + writes) and a smaller data-buffer pool (claimed lazily at submit, ~one per
 * concurrently-submitted endpoint). The RP2350 host is full-speed, so 2 KiB
 * covers control re-enumeration and FS bulk/interrupt; a larger URB is rejected
 * with -EMSGSIZE by the submit validator (s_max_transfer is set to this cap). */
#ifndef USBIP_URB_BUF_CAP
#define USBIP_URB_BUF_CAP 2048
#endif
#ifndef USBIP_URB_SLOTS
#define USBIP_URB_SLOTS 24
#endif
#ifndef USBIP_URB_DATA_SLOTS
#define USBIP_URB_DATA_SLOTS 8
#endif

/* tcp_poll fires every POLL_INTERVAL * 500ms. A conn that has neither received
 * nor acked anything for IDLE_POLLS ticks is treated as a dead half-open peer
 * and aborted, in place of TCP keepalive. 0 disables. */
#define USBIP_POLL_INTERVAL 4      /* ~2s between polls */
#define USBIP_IDLE_POLLS    30     /* ~60s idle before abort */

/* Verbose debug, OFF by default. Mirrors the usbhost backend macro. */
#define USBIP_DBG(...)                                       \
    do {                                                     \
        if (s_urb_verbose) {                                 \
            mp_printf(&mp_plat_print, "usbip: " __VA_ARGS__); \
            mp_printf(&mp_plat_print, "\n");                 \
        }                                                    \
    } while (0)

/* ---------------------------------------------------------------------------
 * rx state machine
 * ------------------------------------------------------------------------- */

typedef enum {
    RX_OP_COMMON = 0,   /* 8 bytes: usbip_op_common_t */
    RX_IMPORT_BUSID,    /* 32 bytes: requested busid */
    RX_HEADER,          /* 48 bytes: usbip_header_t */
    RX_OUT_PAYLOAD,     /* N bytes: CMD_SUBMIT OUT data stage */
    RX_DISCARD,         /* N bytes: drained and discarded */
    RX_DEAD,            /* connection rejected/closed; consume + ignore */
} rx_state_t;

/* EP address -> 5-bit index (number in bits[3:0], direction in bit 4): one slot
 * per (ep, dir), matching the backend's per-EP in-flight tracking. */
static inline uint8_t ep_index(uint8_t ep_addr)
{
    return (uint8_t)((ep_addr & 0x0Fu) | ((ep_addr & 0x80u) >> 3));
}

/* Per-URB record. The backend is depth=1 per (ep,dir); the transport tracks one
 * in-flight URB per EP (conn->inflight[ep_idx]) and queues the rest (pend list,
 * linked via next). The data buffer (in xor out) is from the static URB-data
 * pool: an OUT buffer is claimed when its payload is received; an IN buffer is
 * claimed lazily at submit time, so a queued IN read costs only this header. */
typedef struct inflight_urb {
    struct inflight_urb   *next;      /* pend-FIFO link (NULL when inflight/free) */
    usbip_decoded_header_t hdr;
    char                   busid[USBIP_BUSID_SIZE];
    uint8_t                ep_idx;    /* flow-control lane: 0=EP0/control (EPX), else the EP's own */
    uint8_t                ep_addr;   /* full EP address for backend submit/cancel */
    bool                   is_control;
    uint8_t               *out_buf;   /* OUT data stage (URB-data pool; NULL for IN) */
    size_t                 out_len;
    uint8_t               *in_buf;    /* IN data stage (URB-data pool; lazy) */
    size_t                 in_capacity;
    struct conn_state     *conn;
    bool                   submitted; /* handed to the backend (completion pending) */
    bool                   cancel;    /* UNLINK arrived for this seqnum */
    bool                   unlink_pending;        /* owe a RET_UNLINK after RET_SUBMIT */
    usbip_decoded_header_t unlink_hdr;            /* the UNLINK to reply to */
} inflight_urb_t;

/* Pending TX chunk: bytes copied out of transient/heap source buffers, drained
 * to the pcb as tcp_sndbuf frees up (driven by tcp_sent). Fixed-size, served
 * from a static pool (s_tx_pool); a send larger than USBIP_TX_CHUNK_CAP is split
 * across several chunks by conn_tx. */
typedef struct tx_chunk {
    struct tx_chunk *next;
    uint16_t         off;     /* bytes already written to the pcb */
    uint16_t         len;     /* total bytes in data[] (<= USBIP_TX_CHUNK_CAP) */
    uint8_t          data[USBIP_TX_CHUNK_CAP];
} tx_chunk_t;

typedef struct conn_state {
    struct tcp_pcb *pcb;

    rx_state_t      rx_state;
    /* Fixed scratch large enough for the largest fixed-size frame (48-byte
     * header). op_common (8) and busid (32) also land here. */
    uint8_t         rx_fixed[48];
    size_t          rx_have;     /* bytes accumulated in rx_fixed for the
                                    current fixed-size state */
    size_t          rx_need;     /* target size for the current fixed state */
    size_t          rx_discard;  /* bytes left to drain in RX_DISCARD */

    /* Retained inbound data not yet consumed by the state machine. lwIP
     * delivers arbitrary pbuf chains; we pbuf_cat them here and consume from
     * the head with rx_pbuf_off. Bytes are tcp_recved only as they are
     * consumed, so the receive window provides natural backpressure while a
     * URB is in flight (depth=1 flow control). */
    struct pbuf    *rx_pbuf;
    u16_t           rx_pbuf_off; /* consumed offset into rx_pbuf head chain */

    /* IMPORT state. */
    bool            attached;            /* holds an attachment slot */
    char            busid[USBIP_BUSID_SIZE];
    uint32_t        expected_devid;

    /* Pending CMD_SUBMIT whose OUT data stage is still being accumulated. The
     * header is parked here while RX_OUT_PAYLOAD fills pending_out_buf. */
    usbip_decoded_header_t pending_hdr;
    uint8_t        *pending_out_buf;
    size_t          pending_out_len;
    size_t          rx_out_have;   /* bytes accumulated into pending_out_buf */

    /* In-flight URB per flow-control lane (NULL = lane idle); a CMD_SUBMIT for a
     * busy lane queues on pend_head, preserving per-lane FIFO order. Lane 0 is
     * EP0/control (the shared EPX register); every other endpoint (bulk and
     * interrupt) has its own dedicated async hardware endpoint and its own lane,
     * so a URB that pends forever blocks only its own lane. */
    inflight_urb_t *inflight[32];
    inflight_urb_t *pend_head;
    inflight_urb_t *pend_tail;
    /* URBs handed to the backend (submitted, completion pending). The conn shell
     * cannot be reclaimed while >0 (a completion will dereference it). */
    uint16_t        submitted_count;

    /* TX backpressure queue. */
    tx_chunk_t     *tx_head;
    tx_chunk_t     *tx_tail;
    bool            tx_err;       /* a tx alloc/write failed; conn is doomed */

    /* mp_sched node used to marshal usbhost_submit_async / usbhost_cancel_ep
     * to the main (tuh_task) thread out of the PendSV recv callback. */
    mp_sched_node_t sched_node;
    /* True from conn_schedule() until sched_callback() fully drains. While set,
     * a teardown must NOT free the conn: the node is queued in (or running from)
     * the global scheduler and would be dereferenced after free. Treated as an
     * outstanding reference alongside submitted_count (see conn_try_reclaim). */
    bool            sched_pending;

    /* tcp_poll idle accounting (half-open peer detection). */
    uint16_t        idle_polls;

    bool            dead;         /* teardown started; drop everything */
} conn_state_t;

/* ---------------------------------------------------------------------------
 * Module state
 * ------------------------------------------------------------------------- */

static bool            s_running      = false;
static volatile bool   s_urb_verbose  = false;
static struct tcp_pcb *s_listen_pcb   = NULL;
static int32_t         s_max_transfer = USBIP_URB_BUF_CAP;

/* Live connection registry. Also doubles as the use-after-free guard: a URB
 * completion validates conn against this table before dereferencing it. */
static conn_state_t   *s_conns[USBIP_MAX_CONNS];

/* Attachment table: which busids are currently imported (single-import). */
static struct {
    bool            in_use;
    char            busid[USBIP_BUSID_SIZE];
    conn_state_t   *conn;
} s_attach[USBIP_MAX_CLIENTS];

/* ---------------------------------------------------------------------------
 * static allocation pools
 *
 * conn_state and TX chunks are served from fixed pools rather than libc malloc;
 * see the USBIP_TX_CHUNK_* note above for why the lwIP callback path must not
 * allocate. Both pools live in .bss (zero-initialised: all slots start free).
 * ------------------------------------------------------------------------- */

static conn_state_t s_conn_pool[USBIP_MAX_CONNS];
static bool         s_conn_pool_used[USBIP_MAX_CONNS];

static conn_state_t *conn_alloc(void)
{
    for (size_t i = 0; i < USBIP_MAX_CONNS; i++) {
        if (!s_conn_pool_used[i]) {
            s_conn_pool_used[i] = true;
            memset(&s_conn_pool[i], 0, sizeof(s_conn_pool[i]));
            return &s_conn_pool[i];
        }
    }
    return NULL;
}

static void conn_release(conn_state_t *conn)
{
    size_t idx = (size_t)(conn - s_conn_pool);
    if (idx < USBIP_MAX_CONNS && &s_conn_pool[idx] == conn) {
        s_conn_pool_used[idx] = false;
    }
}

static tx_chunk_t s_tx_pool[USBIP_TX_CHUNK_SLOTS];
static bool       s_tx_pool_used[USBIP_TX_CHUNK_SLOTS];

static tx_chunk_t *tx_chunk_alloc(void)
{
    for (size_t i = 0; i < USBIP_TX_CHUNK_SLOTS; i++) {
        if (!s_tx_pool_used[i]) {
            s_tx_pool_used[i] = true;
            return &s_tx_pool[i];
        }
    }
    return NULL;
}

static void tx_chunk_release(tx_chunk_t *c)
{
    size_t idx = (size_t)(c - s_tx_pool);
    if (idx < USBIP_TX_CHUNK_SLOTS && &s_tx_pool[idx] == c) {
        s_tx_pool_used[idx] = false;
    }
}

/* URB inflight records and their data buffers. A URB holds one inflight record
 * and at most one data buffer (OUT staging or IN capacity, never both). */
static inflight_urb_t s_urb_pool[USBIP_URB_SLOTS];
static bool           s_urb_pool_used[USBIP_URB_SLOTS];

static inflight_urb_t *urb_alloc(void)
{
    for (size_t i = 0; i < USBIP_URB_SLOTS; i++) {
        if (!s_urb_pool_used[i]) {
            s_urb_pool_used[i] = true;
            memset(&s_urb_pool[i], 0, sizeof(s_urb_pool[i]));
            return &s_urb_pool[i];
        }
    }
    return NULL;
}

static void urb_release(inflight_urb_t *u)
{
    size_t idx = (size_t)(u - s_urb_pool);
    if (idx < USBIP_URB_SLOTS && &s_urb_pool[idx] == u) {
        s_urb_pool_used[idx] = false;
    }
}

static uint8_t s_urb_data[USBIP_URB_DATA_SLOTS][USBIP_URB_BUF_CAP];
static bool    s_urb_data_used[USBIP_URB_DATA_SLOTS];

static uint8_t *urb_buf_alloc(size_t len)
{
    if (len > USBIP_URB_BUF_CAP) {
        return NULL;
    }
    for (size_t i = 0; i < USBIP_URB_DATA_SLOTS; i++) {
        if (!s_urb_data_used[i]) {
            s_urb_data_used[i] = true;
            return s_urb_data[i];
        }
    }
    return NULL;
}

static void urb_buf_release(uint8_t *p)
{
    if (p == NULL) {
        return;
    }
    for (size_t i = 0; i < USBIP_URB_DATA_SLOTS; i++) {
        if (s_urb_data[i] == p) {
            s_urb_data_used[i] = false;
            return;
        }
    }
}

/* ---------------------------------------------------------------------------
 * portable byte order (the one raw lwip_htonl site: DEVLIST count)
 * ------------------------------------------------------------------------- */

static uint32_t portable_htonl(uint32_t x)
{
    return ((x & 0x000000FFu) << 24) |
           ((x & 0x0000FF00u) << 8) |
           ((x & 0x00FF0000u) >> 8) |
           ((x & 0xFF000000u) >> 24);
}

/* ---------------------------------------------------------------------------
 * connection registry
 * ------------------------------------------------------------------------- */

static bool conn_register(conn_state_t *conn)
{
    for (size_t i = 0; i < USBIP_MAX_CONNS; i++) {
        if (s_conns[i] == NULL) {
            s_conns[i] = conn;
            return true;
        }
    }
    return false;
}

static void conn_unregister(conn_state_t *conn)
{
    for (size_t i = 0; i < USBIP_MAX_CONNS; i++) {
        if (s_conns[i] == conn) {
            s_conns[i] = NULL;
            return;
        }
    }
}

/* True if conn is currently in the registry (pointer is valid to dereference).
 * Caller must hold the lwIP lock (or be in a RAW callback at PendSV level). */
static bool conn_in_registry(conn_state_t *conn)
{
    if (conn == NULL) {
        return false;
    }
    for (size_t i = 0; i < USBIP_MAX_CONNS; i++) {
        if (s_conns[i] == conn) {
            return true;
        }
    }
    return false;
}

/* ---------------------------------------------------------------------------
 * attachment table (single-import policy, mutex-free; all access from a RAW
 * callback at PendSV level or the VM thread under the lwIP lock)
 * ------------------------------------------------------------------------- */

static bool attachment_acquire(conn_state_t *conn,
                               const char busid[USBIP_BUSID_SIZE])
{
    for (size_t i = 0; i < USBIP_MAX_CLIENTS; i++) {
        if (s_attach[i].in_use &&
            memcmp(s_attach[i].busid, busid, USBIP_BUSID_SIZE) == 0) {
            return false; /* already attached by someone */
        }
    }
    for (size_t i = 0; i < USBIP_MAX_CLIENTS; i++) {
        if (!s_attach[i].in_use) {
            s_attach[i].in_use = true;
            s_attach[i].conn   = conn;
            memcpy(s_attach[i].busid, busid, USBIP_BUSID_SIZE);
            return true;
        }
    }
    return false;
}

static void attachment_release(conn_state_t *conn)
{
    for (size_t i = 0; i < USBIP_MAX_CLIENTS; i++) {
        if (s_attach[i].in_use && s_attach[i].conn == conn) {
            s_attach[i].in_use = false;
            s_attach[i].conn   = NULL;
            memset(s_attach[i].busid, 0, USBIP_BUSID_SIZE);
        }
    }
}

size_t usbip_server_attached_busids(char (*out)[USBIP_BUSID_SIZE], size_t max)
{
    if (out == NULL || max == 0) {
        return 0;
    }
    size_t copied = 0;
    MICROPY_PY_LWIP_ENTER
    for (size_t i = 0; i < USBIP_MAX_CLIENTS && copied < max; i++) {
        if (s_attach[i].in_use) {
            memcpy(out[copied], s_attach[i].busid, USBIP_BUSID_SIZE);
            copied++;
        }
    }
    MICROPY_PY_LWIP_EXIT
    return copied;
}

/* ---------------------------------------------------------------------------
 * TX path: copy-and-queue, drained by tcp_sent (ERR_MEM backpressure)
 *
 * All sends copy their source bytes into a tx_chunk so the caller's buffer can
 * be freed immediately. conn_tx_flush pushes as much as tcp_sndbuf allows;
 * tcp_sent resumes the drain as the peer acks.
 * ------------------------------------------------------------------------- */

static void conn_tx_flush(conn_state_t *conn)
{
    if (conn->dead || conn->pcb == NULL) {
        return;
    }
    bool wrote = false;
    while (conn->tx_head != NULL) {
        tx_chunk_t *c = conn->tx_head;
        size_t remaining = c->len - c->off;
        u16_t avail = tcp_sndbuf(conn->pcb);
        if (avail == 0) {
            break; /* send buffer full; wait for tcp_sent */
        }
        size_t to_write = remaining < avail ? remaining : avail;
        /* Cap to u16_t per tcp_write contract. */
        if (to_write > 0xFFFFu) {
            to_write = 0xFFFFu;
        }
        err_t e = tcp_write(conn->pcb, c->data + c->off, (u16_t)to_write,
                            TCP_WRITE_FLAG_COPY);
        if (e == ERR_MEM) {
            break; /* no pbuf/segment room; retry from tcp_sent */
        }
        if (e != ERR_OK) {
            conn->tx_err = true;
            return;
        }
        c->off += (uint16_t)to_write;
        wrote = true;
        if (c->off >= c->len) {
            conn->tx_head = c->next;
            if (conn->tx_head == NULL) {
                conn->tx_tail = NULL;
            }
            tx_chunk_release(c);
        }
    }
    if (wrote) {
        tcp_output(conn->pcb);
    }
}

/* Queue len bytes for transmission, splitting across CAP-sized pool chunks.
 * Returns false if the pool is exhausted (conn doomed) or the conn is dead. */
static bool conn_tx(conn_state_t *conn, const void *data, size_t len)
{
    if (len == 0) {
        return true;
    }
    if (conn->dead || conn->tx_err) {
        return false;
    }
    const uint8_t *src = (const uint8_t *)data;
    size_t remaining = len;
    while (remaining > 0) {
        tx_chunk_t *c = tx_chunk_alloc();
        if (c == NULL) {
            conn->tx_err = true;
            return false;
        }
        size_t n = remaining < USBIP_TX_CHUNK_CAP ? remaining : USBIP_TX_CHUNK_CAP;
        c->next = NULL;
        c->off  = 0;
        c->len  = (uint16_t)n;
        memcpy(c->data, src, n);
        if (conn->tx_tail == NULL) {
            conn->tx_head = c;
            conn->tx_tail = c;
        } else {
            conn->tx_tail->next = c;
            conn->tx_tail = c;
        }
        src += n;
        remaining -= n;
    }
    conn_tx_flush(conn);
    return true;
}

static bool send_op_common(conn_state_t *conn, uint16_t code, uint32_t status)
{
    usbip_op_common_t reply;
    usbip_proto_pack_op_common(&reply, code, status);
    return conn_tx(conn, &reply, sizeof(reply));
}

static bool send_device_with_interfaces(conn_state_t *conn,
                                        const usbip_dev_record_t *device)
{
    usbip_usb_device_t wire;
    usbip_proto_pack_device_desc(device, &wire);
    if (!conn_tx(conn, &wire, sizeof(wire))) {
        return false;
    }
    for (uint8_t i = 0; i < device->num_interfaces; i++) {
        usbip_usb_interface_t iface;
        if (!usbip_proto_pack_interface_desc(device, i, &iface)) {
            break;
        }
        if (!conn_tx(conn, &iface, sizeof(iface))) {
            return false;
        }
    }
    return true;
}

/* Pack + queue a RET_SUBMIT (header [+ payload]). */
static bool tx_ret_submit(conn_state_t *conn,
                          uint32_t seqnum, uint32_t devid,
                          uint32_t direction, uint32_t ep,
                          int32_t status,
                          const uint8_t *payload, uint32_t payload_len)
{
    USBIP_DBG("TX_RET_SUBMIT seq=%u ep=%u dir=%u status=%d len=%u",
              (unsigned)seqnum, (unsigned)ep, (unsigned)direction,
              (int)status, (unsigned)payload_len);
    usbip_header_t reply;
    usbip_proto_pack_ret_submit(&reply, seqnum, devid, direction, ep,
                                status, payload_len);
    if (!conn_tx(conn, &reply, sizeof(reply))) {
        return false;
    }
    if (payload_len > 0 && payload != NULL) {
        if (!conn_tx(conn, payload, payload_len)) {
            return false;
        }
    }
    return true;
}

static bool tx_ret_unlink(conn_state_t *conn,
                          uint32_t seqnum, uint32_t devid,
                          uint32_t direction, uint32_t ep,
                          int32_t status)
{
    USBIP_DBG("TX_RET_UNLINK seq=%u ep=%u status=%d",
              (unsigned)seqnum, (unsigned)ep, (int)status);
    usbip_header_t reply;
    usbip_proto_pack_ret_unlink(&reply, seqnum, devid, direction, ep, status);
    return conn_tx(conn, &reply, sizeof(reply));
}

/* ---------------------------------------------------------------------------
 * DEVLIST / device lookup (host devices only; no virtual device on rp2)
 * ------------------------------------------------------------------------- */

static size_t collect_all_devices(usbip_dev_record_t *out, size_t max)
{
    return usbhost_get_devices(out, max);
}

static bool find_device_by_busid(const char busid[USBIP_BUSID_SIZE],
                                 usbip_dev_record_t *out)
{
    return usbhost_get_device_by_busid(busid, out);
}

static bool handle_devlist_request(conn_state_t *conn)
{
    usbip_dev_record_t devices[USBIP_MAX_CLIENTS];
    const size_t count = collect_all_devices(devices,
        sizeof(devices) / sizeof(devices[0]));

    USBIP_DBG("DEVLIST: reporting %u device(s)", (unsigned)count);

    if (!send_op_common(conn, USBIP_OP_REP_DEVLIST, 0)) {
        return false;
    }
    uint32_t count_be = portable_htonl((uint32_t)count);
    if (!conn_tx(conn, &count_be, sizeof(count_be))) {
        return false;
    }
    for (size_t i = 0; i < count; i++) {
        if (!send_device_with_interfaces(conn, &devices[i])) {
            return false;
        }
    }
    return true;
}

/* ---------------------------------------------------------------------------
 * URB lifecycle
 * ------------------------------------------------------------------------- */

static void inflight_free(inflight_urb_t *u)
{
    if (u == NULL) {
        return;
    }
    urb_buf_release(u->out_buf);
    urb_buf_release(u->in_buf);
    urb_release(u);
}

/* ---------------------------------------------------------------------------
 * marshalling seam: schedule the main-thread submit/cancel work
 *
 * Called from the RAW recv callback (PendSV). mp_sched_schedule_node runs the
 * callback at thread level where usbhost_submit_async/cancel are legal. One node
 * per conn suffices for any number of EPs: sched_callback drains all of the
 * conn's actionable in-flight URBs (submit the pending ones, cancel the marked
 * ones) in a single re-scanning pass.
 * ------------------------------------------------------------------------- */

static void sched_callback(mp_sched_node_t *node);
static void conn_free(conn_state_t *conn);
static bool conn_teardown(conn_state_t *conn, bool abort_pcb);
static bool rx_pump(conn_state_t *conn);
static void urb_complete_cb(void *ctx, int status, size_t in_len);

static void conn_schedule(conn_state_t *conn)
{
    /* node->callback is reset to NULL by the scheduler before it runs the
     * callback, so re-arming for a later submit/cancel works. If a node is
     * already pending, the flags are already set and will be picked up.
     * sched_pending pins the conn against free() until sched_callback drains. */
    conn->sched_pending = true;
    mp_sched_schedule_node(&conn->sched_node, sched_callback);
}

/* Free a torn-down conn iff no async work can still reference it: no pending
 * sched_node invocation and no backend-owned (submitted) URB whose completion is
 * still to come. Must hold the lwIP lock. After this returns true the conn
 * pointer is dangling. (Teardown frees the unsubmitted URBs and cancels the
 * submitted ones, so reaching submitted_count==0 means every URB is gone.) */
static bool conn_try_reclaim(conn_state_t *conn)
{
    if (conn == NULL || !conn->dead) {
        return false;
    }
    if (conn->sched_pending) {
        return false; /* sched_callback still owes a run; it will reclaim */
    }
    if (conn->submitted_count > 0) {
        return false; /* backend owns URB(s); their completions will reclaim */
    }
    conn_unregister(conn);
    conn_free(conn);
    return true;
}

/* Build the RET_SUBMIT reply for a completed URB and queue it. Runs at thread
 * level under the lwIP lock (taken by the completion callback). */
static void emit_completion(conn_state_t *conn, inflight_urb_t *u,
                            int status, size_t in_len)
{
    const usbip_decoded_header_t *hdr = &u->hdr;
    bool has_payload = (status == 0 && hdr->direction == USBIP_DIR_IN &&
                        in_len > 0 && u->in_buf != NULL);
    (void)tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                        status,
                        has_payload ? u->in_buf : NULL,
                        has_payload ? (uint32_t)in_len : 0);
}

/* Send the RET_UNLINK that was deferred until this URB's RET_SUBMIT was emitted
 * (ordering: RET_SUBMIT(-ECONNRESET) precedes RET_UNLINK). */
static void emit_deferred_unlink(conn_state_t *conn, const inflight_urb_t *u)
{
    if (!u->unlink_pending) {
        return;
    }
    const usbip_decoded_header_t *uh = &u->unlink_hdr;
    (void)tx_ret_unlink(conn, uh->seqnum, uh->devid, uh->direction, uh->ep,
                        -ECONNRESET);
}

/* pend FIFO: URBs accepted from the stream whose EP was busy. They are not in
 * inflight[] and (for IN) hold no data buffer yet. */
static void pend_enqueue(conn_state_t *conn, inflight_urb_t *u)
{
    u->next = NULL;
    if (conn->pend_tail == NULL) {
        conn->pend_head = u;
        conn->pend_tail = u;
    } else {
        conn->pend_tail->next = u;
        conn->pend_tail = u;
    }
}

/* Remove + return the oldest pending URB for ep_idx (per-EP FIFO), or NULL. */
static inflight_urb_t *pend_take_ep(conn_state_t *conn, uint8_t ep_idx)
{
    inflight_urb_t *prev = NULL, *u = conn->pend_head;
    while (u != NULL) {
        if (u->ep_idx == ep_idx) {
            if (prev != NULL) {
                prev->next = u->next;
            } else {
                conn->pend_head = u->next;
            }
            if (conn->pend_tail == u) {
                conn->pend_tail = prev;
            }
            u->next = NULL;
            return u;
        }
        prev = u;
        u = u->next;
    }
    return NULL;
}

/* Remove + return the pending URB carrying usbip seqnum (for UNLINK), or NULL. */
static inflight_urb_t *pend_take_seqnum(conn_state_t *conn, uint32_t seqnum)
{
    inflight_urb_t *prev = NULL, *u = conn->pend_head;
    while (u != NULL) {
        if (u->hdr.seqnum == seqnum) {
            if (prev != NULL) {
                prev->next = u->next;
            } else {
                conn->pend_head = u->next;
            }
            if (conn->pend_tail == u) {
                conn->pend_tail = prev;
            }
            u->next = NULL;
            return u;
        }
        prev = u;
        u = u->next;
    }
    return NULL;
}

/* Retire a finished URB (completed / cancelled / failed-to-submit): emit its
 * RET_SUBMIT (+ any deferred RET_UNLINK), clear its EP slot, free it, and promote
 * the next pending URB for that EP. Does NOT touch submitted_count (the caller
 * adjusts it). Runs under the lwIP lock. A dead conn only frees (no wire output,
 * no promotion). */
static void urb_retire(conn_state_t *conn, inflight_urb_t *u,
                       int status, size_t in_len)
{
    uint8_t ep = u->ep_idx;
    if (u->cancel) {
        status = -ECONNRESET;
        in_len = 0;
    }
    if (!conn->dead) {
        emit_completion(conn, u, status, in_len);
        emit_deferred_unlink(conn, u);
    }
    if (conn->inflight[ep] == u) {
        conn->inflight[ep] = NULL;
    }
    inflight_free(u);

    if (!conn->dead) {
        inflight_urb_t *next = pend_take_ep(conn, ep);
        if (next != NULL) {
            conn->inflight[ep] = next;
            next->submitted = false;
            conn_schedule(conn);   /* submit it at thread level */
        }
    }
}

/* Submit one not-yet-submitted URB (inflight[u->ep_idx] == u) to the backend.
 * Thread level, lwIP lock held. Allocates the IN buffer lazily here. Honours a
 * cancel that raced in before submission, and retires the URB on any failure. */
static void urb_submit_one(conn_state_t *conn, inflight_urb_t *u)
{
    if (u->cancel) {
        urb_retire(conn, u, -ECONNRESET, 0);   /* UNLINK beat the submit */
        return;
    }
    if (u->in_buf == NULL && u->in_capacity > 0 &&
        u->hdr.direction == USBIP_DIR_IN) {
        u->in_buf = urb_buf_alloc(u->in_capacity);
        if (u->in_buf == NULL) {
            urb_retire(conn, u, -ENOMEM, 0);
            return;
        }
    }
    usbip_setup_packet_t setup;
    if (u->is_control) {
        memcpy(&setup, u->hdr.setup, sizeof(setup));
    }
    int err = usbhost_submit_async(u->busid, u->ep_addr, u->is_control,
                                   u->is_control ? &setup : NULL,
                                   u->out_buf, u->out_len,
                                   u->in_buf, u->in_capacity,
                                   urb_complete_cb, u);
    if (err < 0) {
        urb_retire(conn, u, err, 0);   /* backend rejected; no completion fires */
        return;
    }
    u->submitted = true;
    conn->submitted_count++;
}

/* URB completion callback. Fires at THREAD level (tuh_task / mp_sched, or
 * synchronously inside usbhost_cancel_ep), NOT under the lwIP lock. ctx is the
 * inflight_urb_t. The conn may have been torn down meanwhile; validate under the
 * lock before touching anything. */
static void urb_complete_cb(void *ctx, int status, size_t in_len)
{
    inflight_urb_t *u = (inflight_urb_t *)ctx;
    conn_state_t   *conn = u->conn;

    MICROPY_PY_LWIP_ENTER

    /* Only safe to dereference if u is still the active URB on its EP of a
     * registered conn; a doubly-late / stale completion fails this and is
     * dropped (u may have been freed with the conn). */
    if (!conn_in_registry(conn) || conn->inflight[u->ep_idx] != u) {
        MICROPY_PY_LWIP_EXIT
        return;
    }

    if (conn->submitted_count > 0) {
        conn->submitted_count--;
    }
    urb_retire(conn, u, status, in_len);

    if (conn->dead) {
        (void)conn_try_reclaim(conn);
    }

    MICROPY_PY_LWIP_EXIT
}

/* Thread-level worker: drain the conn's actionable in-flight URBs - submit the
 * not-yet-submitted ones and cancel the ones an UNLINK (or teardown) marked. The
 * loop re-scans after each action because a cancel completes synchronously
 * (urb_complete_cb retires that URB and may promote the next pending one, which
 * the next pass then submits - after usbhost_cancel_ep's EP reset has finished).
 *
 * sched_pending stays TRUE for the whole body (set by conn_schedule) so a
 * teardown racing at PendSV defers the conn free; the tail clears it and reclaims
 * a torn-down shell once no backend-owned URB remains. Runs entirely under the
 * lwIP lock (recursive), so the synchronous cancel completion re-enters safely. */
static void sched_callback(mp_sched_node_t *node)
{
    conn_state_t *conn = (conn_state_t *)((uint8_t *)node -
        offsetof(conn_state_t, sched_node));

    MICROPY_PY_LWIP_ENTER
    for (;;) {
        inflight_urb_t *act = NULL;
        bool do_cancel = false;
        for (uint8_t ep = 0; ep < 32; ep++) {
            inflight_urb_t *u = conn->inflight[ep];
            if (u == NULL) {
                continue;
            }
            if (conn->dead) {
                /* A dead conn keeps only its backend-owned URBs (teardown freed
                 * the unsubmitted ones); cancel them so their completions land
                 * and the shell can be reclaimed. */
                if (u->submitted) {
                    act = u; do_cancel = true; break;
                }
            } else if (!u->submitted) {
                act = u; do_cancel = false; break;   /* needs submit */
            } else if (u->cancel) {
                act = u; do_cancel = true; break;     /* UNLINK on a live URB */
            }
        }
        if (act == NULL) {
            break;
        }
        if (do_cancel) {
            /* On this TinyUSB pin the abort fires the completion synchronously
             * through urb_complete_cb, which retires the URB. */
            usbhost_cancel_ep(act->busid, act->ep_addr);
        } else {
            urb_submit_one(conn, act);
        }
    }

    conn->sched_pending = false;
    if (conn->dead) {
        (void)conn_try_reclaim(conn);
    }
    MICROPY_PY_LWIP_EXIT
}

/* ---------------------------------------------------------------------------
 * intake: validate a CMD_SUBMIT, apply policy, build the URB, route it per-EP
 *
 * Runs in the recv callback (PendSV). The OUT data stage, if any, has already
 * been accumulated into out_buf by the rx state machine before this is called.
 * Returns false to drop the connection.
 * ------------------------------------------------------------------------- */

/* Pure validation/policy for a CMD_SUBMIT header. If a policy reply is owed
 * (EMSGSIZE/EINVAL/EOPNOTSUPP/ENODEV/EPIPE or an EP0 cache-served descriptor),
 * it queues the RET_SUBMIT and sets *handled. Returns true to proceed (caller
 * dispatches the URB to the backend if *handled is false); false only on a
 * fatal (drop-connection) condition. No OUT data has been read yet at this
 * point; the caller drains it (RX_DISCARD) when *handled is true. */
static bool intake_policy(conn_state_t *conn,
                          const usbip_decoded_header_t *hdr,
                          bool *handled)
{
    *handled = false;

    int v = usbip_proto_validate_submit(hdr, s_max_transfer);
    if (v == -EINVAL && (hdr->direction != USBIP_DIR_OUT &&
                         hdr->direction != USBIP_DIR_IN)) {
        USBIP_DBG("bad direction %u, dropping conn", (unsigned)hdr->direction);
        return false; /* fatal */
    }
    if (v == -EMSGSIZE) {
        *handled = true;
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -EMSGSIZE, NULL, 0);
    }
    if (v == -EINVAL) {
        *handled = true;
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -EINVAL, NULL, 0);
    }
    if (v == -EOPNOTSUPP) {
        *handled = true;
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -EOPNOTSUPP, NULL, 0);
    }

    if (hdr->devid != conn->expected_devid) {
        USBIP_DBG("ENODEV devid=0x%08x expected=0x%08x",
                  (unsigned)hdr->devid, (unsigned)conn->expected_devid);
        *handled = true;
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -ENODEV, NULL, 0);
    }

    if (hdr->ep == 0) {
        const bool setup_in = (hdr->setup[0] & USBIP_REQUEST_DIR_IN) != 0;
        if ((hdr->direction == USBIP_DIR_IN) != setup_in) {
            *handled = true;
            return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                                 hdr->ep, -EINVAL, NULL, 0);
        }
        const uint8_t bmRequestType = hdr->setup[0];
        const uint8_t bRequest      = hdr->setup[1];
        const bool is_standard = ((bmRequestType & 0x60u) == 0x00u);
        /* Refuse SET_ADDRESS proxied over the wire (would desync the bus
         * address TinyUSB assigned to the DUT). */
        if (is_standard && bRequest == 0x05u /* SET_ADDRESS */) {
            USBIP_DBG("refusing SET_ADDRESS (seq=%u)", (unsigned)hdr->seqnum);
            *handled = true;
            return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                                 hdr->ep, -EPIPE, NULL, 0);
        }
        /* EP0 GET_DESCRIPTOR cache intercept: serve cached device/config
         * descriptors so STALL-on-refetch DUTs enumerate over USB/IP. */
        if (is_standard && hdr->direction == USBIP_DIR_IN &&
            bRequest == 0x06u /* GET_DESCRIPTOR */) {
            const uint8_t desc_type = hdr->setup[3]; /* wValue high byte */
            uint8_t cache_buf[1024];
            size_t  cache_len = 0;
            bool    served = false;
            if (desc_type == 0x01 /* DEVICE */) {
                served = usbhost_get_cached_device_desc(
                    conn->busid, cache_buf, sizeof(cache_buf), &cache_len);
            } else if (desc_type == 0x02 /* CONFIGURATION */) {
                served = usbhost_get_cached_config_desc(
                    conn->busid, cache_buf, sizeof(cache_buf), &cache_len);
            }
            if (served) {
                size_t cap = (hdr->transfer_buffer_length > 0)
                    ? (size_t)hdr->transfer_buffer_length : 0u;
                if (cap < cache_len) {
                    cache_len = cap;
                }
                USBIP_DBG("EP0 cache-served type=0x%02x len=%u (seq=%u)",
                          desc_type, (unsigned)cache_len, (unsigned)hdr->seqnum);
                *handled = true;
                return tx_ret_submit(conn, hdr->seqnum, hdr->devid,
                                     hdr->direction, hdr->ep, 0,
                                     cache_buf, (uint32_t)cache_len);
            }
        }
    }

    return true; /* proceed to dispatch */
}

/* Build an inflight_urb from the decoded header and any accumulated OUT payload.
 * If the URB's EP is free it becomes inflight[ep] and a submit is scheduled;
 * otherwise it queues (per-EP FIFO). Takes ownership of *out_buf. The IN buffer
 * is allocated lazily at submit (urb_submit_one), so a queued IN read costs only
 * the header. Returns false to drop the connection. */
static bool intake_dispatch(conn_state_t *conn,
                            const usbip_decoded_header_t *hdr,
                            uint8_t *out_buf, size_t out_len)
{
    inflight_urb_t *u = urb_alloc();
    if (u == NULL) {
        urb_buf_release(out_buf);
        return tx_ret_submit(conn, hdr->seqnum, hdr->devid, hdr->direction,
                             hdr->ep, -ENOMEM, NULL, 0);
    }
    u->hdr        = *hdr;
    memcpy(u->busid, conn->busid, USBIP_BUSID_SIZE);
    u->conn       = conn;
    u->out_buf    = out_buf;
    u->out_len    = out_len;
    u->is_control = (hdr->ep == 0);
    u->ep_addr    = u->is_control ? 0
        : (uint8_t)(hdr->ep | (hdr->direction == USBIP_DIR_IN ? 0x80u : 0x00u));
    /* Flow-control lane (depth=1 per lane). On the RP2040/RP2350 host only EP0
     * (control) uses the shared EPX register; every other endpoint - bulk AND
     * interrupt - gets its own dedicated async hardware endpoint and polls
     * concurrently (hcd_rp2040.c _hw_endpoint_allocate). So each non-control EP
     * gets its own lane: a bulk-IN read that pends (device NAKing until the DUT
     * has data) blocks only its own lane, never control or bulk-OUT. */
    u->ep_idx = u->is_control ? 0 : ep_index(u->ep_addr);
    if (hdr->direction == USBIP_DIR_IN && hdr->transfer_buffer_length > 0) {
        u->in_capacity = (size_t)hdr->transfer_buffer_length;
    }

    if (conn->inflight[u->ep_idx] == NULL) {
        conn->inflight[u->ep_idx] = u;   /* EP idle: this is its active URB */
        conn_schedule(conn);             /* submit it at thread level */
    } else {
        pend_enqueue(conn, u);           /* EP busy: wait behind the in-flight URB */
    }
    return true;
}

/* ---------------------------------------------------------------------------
 * rx state machine helpers
 * ------------------------------------------------------------------------- */

static void rx_enter_fixed(conn_state_t *conn, rx_state_t state, size_t need)
{
    conn->rx_state = state;
    conn->rx_have  = 0;
    conn->rx_need  = need;
}

static void rx_enter_discard(conn_state_t *conn, size_t n)
{
    conn->rx_state   = RX_DISCARD;
    conn->rx_discard = n;
}

/* Handle a fully-received op_common (8 bytes). */
static bool on_op_common(conn_state_t *conn)
{
    uint16_t version, code;
    uint32_t status;
    usbip_proto_unpack_op_common((const usbip_op_common_t *)conn->rx_fixed,
                                 &version, &code, &status);
    if (version != USBIP_VERSION) {
        USBIP_DBG("unsupported version 0x%04x", version);
        return false;
    }
    if (code == USBIP_OP_REQ_DEVLIST) {
        if (!handle_devlist_request(conn)) {
            return false;
        }
        /* DEVLIST is a one-shot; the kernel closes the conn after reading. */
        conn->rx_state = RX_DEAD;
        return true;
    }
    if (code == USBIP_OP_REQ_IMPORT) {
        rx_enter_fixed(conn, RX_IMPORT_BUSID, USBIP_BUSID_SIZE);
        return true;
    }
    USBIP_DBG("unsupported op 0x%04x", code);
    return false;
}

/* Handle a fully-received IMPORT busid (32 bytes). */
static bool on_import_busid(conn_state_t *conn)
{
    char busid[USBIP_BUSID_SIZE];
    memcpy(busid, conn->rx_fixed, USBIP_BUSID_SIZE);

    usbip_dev_record_t device;
    bool found = find_device_by_busid(busid, &device);
    if (!found) {
        (void)send_op_common(conn, USBIP_OP_REP_IMPORT, 1);
        conn->rx_state = RX_DEAD;
        return true;
    }
    if (!attachment_acquire(conn, busid)) {
        USBIP_DBG("busid '%.32s' already attached, refusing", busid);
        (void)send_op_common(conn, USBIP_OP_REP_IMPORT, 1);
        conn->rx_state = RX_DEAD;
        return true;
    }
    conn->attached = true;
    memcpy(conn->busid, busid, USBIP_BUSID_SIZE);
    conn->expected_devid = usbip_proto_make_devid(&device);

    if (!send_op_common(conn, USBIP_OP_REP_IMPORT, 0)) {
        return false;
    }
    usbip_usb_device_t wire;
    usbip_proto_pack_device_desc(&device, &wire);
    if (!conn_tx(conn, &wire, sizeof(wire))) {
        return false;
    }

    /* Now stream URBs. */
    rx_enter_fixed(conn, RX_HEADER, sizeof(usbip_header_t));
    return true;
}

/* Handle a fully-received URB header (48 bytes). For CMD_SUBMIT with an OUT
 * data stage, transition to RX_OUT_PAYLOAD; otherwise dispatch immediately.
 * Returns false to drop the connection. */
static bool on_header(conn_state_t *conn)
{
    usbip_decoded_header_t hdr;
    usbip_proto_unpack_header((const usbip_header_t *)conn->rx_fixed, &hdr);

    if (hdr.command == USBIP_CMD_SUBMIT) {
        USBIP_DBG("CMD_SUBMIT seq=%u ep=%u dir=%s len=%u",
                  (unsigned)hdr.seqnum, (unsigned)hdr.ep,
                  hdr.direction == USBIP_DIR_IN ? "IN" : "OUT",
                  (unsigned)hdr.transfer_buffer_length);

        bool is_out = (hdr.direction == USBIP_DIR_OUT);
        bool has_out_payload = is_out && hdr.transfer_buffer_length > 0;

        /* Run validation/policy. EMSGSIZE etc. reply now; if there is an OUT
         * payload still on the wire, it must be drained (RX_DISCARD). */
        bool handled = false;
        uint8_t *out_buf = NULL;
        if (!intake_policy(conn, &hdr, &handled)) {
            return false; /* fatal */
        }
        if (handled) {
            if (has_out_payload) {
                rx_enter_discard(conn, (size_t)hdr.transfer_buffer_length);
            } else {
                rx_enter_fixed(conn, RX_HEADER, sizeof(usbip_header_t));
            }
            return true;
        }

        /* Proceeding to dispatch. Bound the OUT buffer by max_transfer (the
         * EMSGSIZE check above already enforced it). Allocate it AFTER the
         * EMSGSIZE check, here. */
        if (has_out_payload) {
            size_t n = (size_t)hdr.transfer_buffer_length;
            out_buf = urb_buf_alloc(n);
            if (out_buf == NULL) {
                /* Drain the payload and reply ENOMEM. */
                if (!tx_ret_submit(conn, hdr.seqnum, hdr.devid, hdr.direction,
                                   hdr.ep, -ENOMEM, NULL, 0)) {
                    return false;
                }
                rx_enter_discard(conn, n);
                return true;
            }
            /* Park the header + buffer; accumulate the payload into
             * pending_out_buf across RX_OUT_PAYLOAD (the consume loop copies
             * into pending_out_buf, not rx_fixed, for this state). */
            conn->pending_hdr = hdr;
            conn->pending_out_buf = out_buf;
            conn->pending_out_len = n;
            conn->rx_out_have = 0;
            conn->rx_state = RX_OUT_PAYLOAD;
            return true;
        }

        /* IN or zero-length OUT: dispatch immediately. intake_dispatch submits
         * on a free EP or queues behind a busy one; either way the stream keeps
         * flowing (per-EP flow control). */
        if (!intake_dispatch(conn, &hdr, NULL, 0)) {
            return false;
        }
        rx_enter_fixed(conn, RX_HEADER, sizeof(usbip_header_t));
        return true;
    }

    if (hdr.command == USBIP_CMD_UNLINK) {
        USBIP_DBG("CMD_UNLINK seq=%u unlink_seq=%u",
                  (unsigned)hdr.seqnum, (unsigned)hdr.unlink_seqnum);
        /* Find the target URB by its (submit) seqnum: first across the in-flight
         * EPs, then the pending queue. */
        inflight_urb_t *target = NULL;
        for (uint8_t ep = 0; ep < 32 && target == NULL; ep++) {
            inflight_urb_t *u = conn->inflight[ep];
            if (u != NULL && u->hdr.seqnum == hdr.unlink_seqnum) {
                target = u;
            }
        }
        if (target != NULL) {
            if (!target->cancel) {
                /* Defer the RET_UNLINK until the URB's RET_SUBMIT(-ECONNRESET).
                 * A submitted URB is aborted by sched_callback; one still waiting
                 * to be submitted is short-circuited in urb_submit_one (which a
                 * submit was already scheduled for at intake). */
                target->cancel = true;
                target->unlink_pending = true;
                target->unlink_hdr = hdr;
                if (target->submitted) {
                    conn_schedule(conn);
                }
            }
        } else {
            inflight_urb_t *q = pend_take_seqnum(conn, hdr.unlink_seqnum);
            if (q != NULL) {
                /* Queued, never submitted: emit RET_SUBMIT(-ECONNRESET) then
                 * RET_UNLINK, and drop it (no backend involvement). */
                (void)tx_ret_submit(conn, q->hdr.seqnum, q->hdr.devid,
                                    q->hdr.direction, q->hdr.ep, -ECONNRESET,
                                    NULL, 0);
                (void)tx_ret_unlink(conn, hdr.seqnum, hdr.devid, hdr.direction,
                                    hdr.ep, -ECONNRESET);
                inflight_free(q);
            } else {
                /* Unknown seqnum: already completed (RET_SUBMIT sent) or never
                 * seen. Reply RET_UNLINK immediately. */
                if (!tx_ret_unlink(conn, hdr.seqnum, hdr.devid, hdr.direction,
                                   hdr.ep, -ECONNRESET)) {
                    return false;
                }
            }
        }
        rx_enter_fixed(conn, RX_HEADER, sizeof(usbip_header_t));
        return true;
    }

    USBIP_DBG("unsupported command 0x%08x", (unsigned)hdr.command);
    return false;
}

/* ---------------------------------------------------------------------------
 * rx pump: drive the rx state machine from the retained pbuf chain
 *
 * Consumes as much of conn->rx_pbuf (from rx_pbuf_off) as the state machine can
 * accept, tcp_recved'ing exactly what is consumed and freeing fully-drained
 * pbufs. Consumes the whole available chain (per-EP flow control means no
 * conn-wide withhold); it stops only when the chain is exhausted or a fixed-size
 * frame is incomplete. Returns false on a fatal (drop-connection) condition.
 * ------------------------------------------------------------------------- */

/* Copy up to `take` bytes from the retained chain at the current offset into
 * dst (or discard if dst==NULL), advancing rx_pbuf_off and freeing drained
 * pbufs. Returns bytes actually copied (may be < take if the chain is short).
 * Caller must have already bounded `take` by available bytes. */
static size_t rx_take(conn_state_t *conn, uint8_t *dst, size_t take)
{
    size_t done = 0;
    while (done < take && conn->rx_pbuf != NULL) {
        struct pbuf *head = conn->rx_pbuf;
        u16_t head_avail = head->len - conn->rx_pbuf_off;
        size_t n = take - done;
        if (n > head_avail) {
            n = head_avail;
        }
        if (dst != NULL) {
            memcpy(dst + done, (const uint8_t *)head->payload + conn->rx_pbuf_off, n);
        }
        done += n;
        conn->rx_pbuf_off += (u16_t)n;
        /* Reopen the receive window for the bytes just consumed. */
        if (conn->pcb != NULL && n > 0) {
            tcp_recved(conn->pcb, (u16_t)n);
        }
        if (conn->rx_pbuf_off >= head->len) {
            /* Drained this pbuf; advance to the next in the chain. */
            struct pbuf *next = head->next;
            if (next != NULL) {
                pbuf_ref(next);
            }
            pbuf_free(head);          /* frees only this segment's ref */
            conn->rx_pbuf = next;
            conn->rx_pbuf_off = 0;
        }
    }
    return done;
}

static size_t rx_avail(const conn_state_t *conn)
{
    return (conn->rx_pbuf != NULL)
        ? (size_t)(conn->rx_pbuf->tot_len - conn->rx_pbuf_off) : 0;
}

static bool rx_pump(conn_state_t *conn)
{
    while (rx_avail(conn) > 0) {
        if (conn->tx_err) {
            return false;
        }
        /* No conn-wide stop here: every CMD_SUBMIT is consumed and either
         * submitted (its EP was free) or queued (per-EP FIFO), so an EP that
         * stalls blocks only itself. Backpressure is the URB-pool cap (intake
         * replies -ENOMEM when exhausted). RX_OUT_PAYLOAD/RX_DISCARD finish an
         * accepted/rejected URB; RX_DEAD drains. */
        if (conn->rx_state == RX_DEAD) {
            (void)rx_take(conn, NULL, rx_avail(conn)); /* drain + tcp_recved */
            break;
        }

        if (conn->rx_state == RX_DISCARD) {
            size_t take = rx_avail(conn);
            if (take > conn->rx_discard) {
                take = conn->rx_discard;
            }
            conn->rx_discard -= rx_take(conn, NULL, take);
            if (conn->rx_discard == 0) {
                rx_enter_fixed(conn, RX_HEADER, sizeof(usbip_header_t));
            }
            continue;
        }

        if (conn->rx_state == RX_OUT_PAYLOAD) {
            size_t want = conn->pending_out_len - conn->rx_out_have;
            size_t avail = rx_avail(conn);
            size_t take = avail < want ? avail : want;
            conn->rx_out_have += rx_take(conn,
                conn->pending_out_buf + conn->rx_out_have, take);
            if (conn->rx_out_have >= conn->pending_out_len) {
                uint8_t *ob = conn->pending_out_buf;
                size_t   ol = conn->pending_out_len;
                usbip_decoded_header_t hdr = conn->pending_hdr;
                conn->pending_out_buf = NULL;
                conn->pending_out_len = 0;
                conn->rx_out_have = 0;
                if (!intake_dispatch(conn, &hdr, ob, ol)) {
                    return false;
                }
                rx_enter_fixed(conn, RX_HEADER, sizeof(usbip_header_t));
            }
            continue;
        }

        /* Fixed-size states: RX_OP_COMMON / RX_IMPORT_BUSID / RX_HEADER. */
        size_t want = conn->rx_need - conn->rx_have;
        size_t avail = rx_avail(conn);
        size_t take = avail < want ? avail : want;
        conn->rx_have += rx_take(conn, conn->rx_fixed + conn->rx_have, take);
        if (conn->rx_have < conn->rx_need) {
            break; /* need more bytes; chain exhausted */
        }

        bool ok;
        switch (conn->rx_state) {
            case RX_OP_COMMON:    ok = on_op_common(conn); break;
            case RX_IMPORT_BUSID: ok = on_import_busid(conn); break;
            case RX_HEADER:       ok = on_header(conn); break;
            default:              ok = false; break;
        }
        if (!ok) {
            return false;
        }
    }
    return true;
}

/* ---------------------------------------------------------------------------
 * connection teardown
 * ------------------------------------------------------------------------- */

static void conn_free(conn_state_t *conn)
{
    /* Release any retained inbound pbuf chain. */
    if (conn->rx_pbuf != NULL) {
        pbuf_free(conn->rx_pbuf);
        conn->rx_pbuf = NULL;
    }
    /* Release any TX backlog. */
    tx_chunk_t *c = conn->tx_head;
    while (c != NULL) {
        tx_chunk_t *n = c->next;
        tx_chunk_release(c);
        c = n;
    }
    /* All URBs are released by teardown (unsubmitted) or their completions
     * (submitted) before reclaim; free any stragglers defensively so a pool slot
     * can never leak. */
    for (uint8_t ep = 0; ep < 32; ep++) {
        if (conn->inflight[ep] != NULL) {
            inflight_free(conn->inflight[ep]);
            conn->inflight[ep] = NULL;
        }
    }
    inflight_urb_t *p = conn->pend_head;
    while (p != NULL) {
        inflight_urb_t *n = p->next;
        inflight_free(p);
        p = n;
    }
    conn->pend_head = NULL;
    conn->pend_tail = NULL;
    urb_buf_release(conn->pending_out_buf);
    conn_release(conn);
}

/* Begin teardown: detach the pcb, release the attachment slot, and free the
 * conn. MUST run with the lwIP lock held (or in a RAW callback at PendSV
 * level). After this returns, no further pcb callback dereferences conn.
 *
 * Backend-owned (submitted) URBs complete at thread level and would dereference
 * conn, so the shell stays REGISTERED (dead) until their completions land; the
 * unsubmitted URBs are freed here and the submitted ones aborted, so even a
 * never-completing URB (an idle interrupt-IN) cannot pin the conn forever.
 *
 * Returns true iff it called tcp_abort() on the pcb. A caller that is itself an
 * lwIP RAW callback for this pcb MUST propagate that as ERR_ABRT (lwIP keeps
 * using the pcb after the callback unless told it was aborted). */
static bool conn_teardown(conn_state_t *conn, bool abort_pcb)
{
    if (conn == NULL || conn->dead) {
        return false;
    }
    conn->dead = true;

    if (conn->attached) {
        attachment_release(conn);
        conn->attached = false;
    }

    bool aborted = false;
    struct tcp_pcb *pcb = conn->pcb;
    if (pcb != NULL) {
        tcp_arg(pcb, NULL);
        tcp_recv(pcb, NULL);
        tcp_sent(pcb, NULL);
        tcp_poll(pcb, NULL, 0);
        tcp_err(pcb, NULL);
        conn->pcb = NULL;
        if (abort_pcb) {
            tcp_abort(pcb);
            aborted = true;
        } else {
            if (tcp_close(pcb) != ERR_OK) {
                tcp_abort(pcb);
                aborted = true;
            }
        }
    }

    /* Drop the pending queue and the unsubmitted in-flight URBs (not at the
     * backend, will never complete). Leave the submitted ones in inflight[] and
     * schedule sched_callback to abort them; their completions decrement
     * submitted_count and conn_try_reclaim frees the shell once it reaches 0. */
    inflight_urb_t *p = conn->pend_head;
    while (p != NULL) {
        inflight_urb_t *n = p->next;
        inflight_free(p);
        p = n;
    }
    conn->pend_head = NULL;
    conn->pend_tail = NULL;
    bool any_submitted = false;
    for (uint8_t ep = 0; ep < 32; ep++) {
        inflight_urb_t *u = conn->inflight[ep];
        if (u == NULL) {
            continue;
        }
        if (u->submitted) {
            any_submitted = true;
        } else {
            conn->inflight[ep] = NULL;
            inflight_free(u);
        }
    }
    if (any_submitted) {
        conn_schedule(conn);
    }

    (void)conn_try_reclaim(conn);
    return aborted;
}

/* ---------------------------------------------------------------------------
 * lwIP RAW callbacks (all fire at PendSV level)
 * ------------------------------------------------------------------------- */

static err_t cb_recv(void *arg, struct tcp_pcb *pcb, struct pbuf *p, err_t err)
{
    conn_state_t *conn = (conn_state_t *)arg;
    if (conn == NULL) {
        if (p != NULL) {
            pbuf_free(p);
        }
        return ERR_OK;
    }
    if (err != ERR_OK || p == NULL) {
        /* Peer closed or error: tear down. */
        if (p != NULL) {
            pbuf_free(p);
        }
        return conn_teardown(conn, false) ? ERR_ABRT : ERR_OK;
    }

    /* Inbound data is liveness: reset the idle/half-open counter so a healthy
     * but quiescent import (an attached DUT with no current URB traffic) is not
     * torn down by cb_poll after USBIP_IDLE_POLLS. */
    conn->idle_polls = 0;

    /* Append to the retained chain (rx_pump consumes from it and tcp_recved's
     * exactly what it uses, so the window stays closed for un-consumed data). */
    if (conn->rx_pbuf == NULL) {
        conn->rx_pbuf = p;
        conn->rx_pbuf_off = 0;
    } else {
        pbuf_cat(conn->rx_pbuf, p);
    }

    if (!rx_pump(conn)) {
        return conn_teardown(conn, true) ? ERR_ABRT : ERR_OK;
    }
    return ERR_OK;
}

static err_t cb_sent(void *arg, struct tcp_pcb *pcb, u16_t len)
{
    (void)pcb;
    (void)len;
    conn_state_t *conn = (conn_state_t *)arg;
    if (conn == NULL || conn->dead) {
        return ERR_OK;
    }
    /* Bytes acked: drain more of the TX backlog. */
    conn_tx_flush(conn);
    if (conn->tx_err) {
        return conn_teardown(conn, true) ? ERR_ABRT : ERR_OK;
    }
    return ERR_OK;
}

static err_t cb_poll(void *arg, struct tcp_pcb *pcb)
{
    (void)pcb;
    conn_state_t *conn = (conn_state_t *)arg;
    if (conn == NULL || conn->dead) {
        return ERR_OK;
    }
    /* Retry a stalled TX backlog (segments may have freed up). */
    if (conn->tx_head != NULL) {
        conn_tx_flush(conn);
    }
    /* Idle half-open detection: count polls with no activity. Only reaps a
     * connection that has NOT completed IMPORT (a stalled DEVLIST/handshake
     * peer). An established import legitimately sits quiet - a forwarded device
     * with no current URB traffic (an idle REPL waiting for input) must never be
     * torn down by this timer. Reset the counter on any TX progress or in-flight
     * work too. */
    if (conn->attached || conn->tx_head != NULL) {
        conn->idle_polls = 0;
    } else if (++conn->idle_polls >= USBIP_IDLE_POLLS) {
        USBIP_DBG("idle timeout, aborting conn");
        return conn_teardown(conn, true) ? ERR_ABRT : ERR_OK;
    }
    return ERR_OK;
}

static void cb_err(void *arg, err_t err)
{
    (void)err;
    conn_state_t *conn = (conn_state_t *)arg;
    if (conn == NULL) {
        return;
    }
    /* The pcb is already freed by lwIP on err; do not touch it. */
    conn->pcb = NULL;
    conn_teardown(conn, false);
}

static err_t cb_accept(void *arg, struct tcp_pcb *newpcb, err_t err)
{
    (void)arg;
    if (err != ERR_OK || newpcb == NULL) {
        return ERR_OK;
    }

    conn_state_t *conn = conn_alloc();
    if (conn == NULL) {
        tcp_abort(newpcb);
        return ERR_ABRT;
    }
    if (!conn_register(conn)) {
        conn_release(conn);
        tcp_abort(newpcb);
        return ERR_ABRT;
    }
    conn->pcb = newpcb;
    rx_enter_fixed(conn, RX_OP_COMMON, sizeof(usbip_op_common_t));

    tcp_arg(newpcb, conn);
    tcp_recv(newpcb, cb_recv);
    tcp_sent(newpcb, cb_sent);
    tcp_err(newpcb, cb_err);
    tcp_poll(newpcb, cb_poll, USBIP_POLL_INTERVAL);
    tcp_nagle_disable(newpcb);

    USBIP_DBG("accepted conn %p", (void *)conn);
    return ERR_OK;
}

/* ---------------------------------------------------------------------------
 * public API
 * ------------------------------------------------------------------------- */

int usbip_server_start(uint16_t port)
{
    if (s_running) {
        /* Already serving: re-seed the USB host slot table. usbhost_start() is an
         * idempotent thread-level rescan (rescan_mounted) that drops vanished /
         * re-enumerated slots and re-adds them fresh under the current enum gen,
         * without disturbing a healthy live attachment. A DUT that re-enumerated
         * since the server came up (reset, replug, or a machine.USBHost re-init)
         * otherwise leaves a stale slot whose live transfers fail -ENODEV while the
         * descriptor cache still answers IMPORT + GET_DESCRIPTOR(DEVICE/CONFIG), so
         * an attach half-enumerates (kernel "string descriptor 0 read error: -19")
         * with no stop/start. Re-seeding here makes attach_dut / ensure self-heal. */
        (void)usbhost_start();
        return 0;
    }
    if (port == 0) {
        port = USBIP_TCP_PORT;
    }

    /* Ensure the USB host backend is up before we can serve DEVLIST/IMPORT. */
    (void)usbhost_start();

    int rc = 0;
    MICROPY_PY_LWIP_ENTER

    struct tcp_pcb *pcb = tcp_new();
    if (pcb == NULL) {
        rc = -ENOMEM;
        goto out;
    }
    if (tcp_bind(pcb, IP_ANY_TYPE, port) != ERR_OK) {
        tcp_abort(pcb);
        rc = -EADDRINUSE;
        goto out;
    }
    struct tcp_pcb *lpcb = tcp_listen_with_backlog(pcb, USBIP_MAX_CONNS);
    if (lpcb == NULL) {
        tcp_abort(pcb);
        rc = -ENOMEM;
        goto out;
    }
    s_listen_pcb = lpcb;
    tcp_accept(lpcb, cb_accept);
    s_running = true;

out:
    MICROPY_PY_LWIP_EXIT
    return rc;
}

int usbip_server_stop(void)
{
    if (!s_running) {
        return 0;
    }
    MICROPY_PY_LWIP_ENTER

    if (s_listen_pcb != NULL) {
        tcp_accept(s_listen_pcb, NULL);
        tcp_close(s_listen_pcb);
        s_listen_pcb = NULL;
    }
    /* Abort all active connections. conn_teardown unregisters as it goes; copy
     * pointers first to avoid mutating the table mid-iteration. */
    for (size_t i = 0; i < USBIP_MAX_CONNS; i++) {
        conn_state_t *conn = s_conns[i];
        if (conn != NULL) {
            conn_teardown(conn, true);
        }
    }
    s_running = false;

    MICROPY_PY_LWIP_EXIT
    return 0;
}

bool usbip_server_is_running(void)
{
    return s_running;
}

int32_t usbip_server_max_transfer(void)
{
    return s_max_transfer;
}

void usbip_server_set_verbose(bool enable)
{
    s_urb_verbose = enable;
}

bool usbip_server_is_verbose(void)
{
    return s_urb_verbose;
}
