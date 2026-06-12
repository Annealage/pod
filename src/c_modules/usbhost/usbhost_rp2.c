/* Annealage Pod RP2350: TinyUSB raw-URB host backend (lwIP-RAW forwarder).
 *
 * Cooperative single-thread port of usbhost.c (ESP32-S3). TinyUSB host runs on
 * the MicroPython main thread via shared/tinyusb/mp_usbh.c's tuh_task pump
 * (mp_usbh_task -> tuh_task_ext(0,false), scheduled from
 * __wrap_hcd_event_handler + mp_sched_schedule_node). Because there is one
 * cooperative submit/completion context, ALL of the FreeRTOS scaffolding in the
 * esp32 original collapses: no tasks, no per-slot/per-EP mutexes, no watchdog
 * task, no ep_stats/ep0_errors rings, no done_sem sync path, and no
 * natural-vs-cancel atomic CAS (completion and cancel can never run
 * concurrently - both only execute on the main thread, completion when tuh_task
 * runs and cancel when the transport calls in). The host is depth=1 (one
 * transfer in flight per endpoint, the tuh_edpt_xfer / tuh_control_xfer
 * constraint).
 *
 * Enumeration: the rp2 mp_usbh.c does NOT call the weak tuh_mount_hook /
 * tuh_umount_hook (the esp32 backend hooked those; confirmed absent here and
 * unnecessary). Instead descriptors are cached during TinyUSB's own enumeration
 * by the weak tuh_enum_descriptor_device_cb / tuh_enum_descriptor_configuration_cb
 * overrides below, and the slot table is populated by usbhost_start() scanning
 * tuh_mounted() for every device address (rescan_mounted). That scan calls
 * tuh_edpt_open/tuh_speed_get and so runs at THREAD level only; the query
 * accessors (usbhost_get_devices/by_busid), which the transport calls from its
 * lwIP RAW recv callback at PendSV, are PURE slot-table reads with no tuh_*.
 * (Hot-plug-after-start refresh is a follow-up: it needs a thread-level rescan
 * trigger; the wired DUT is present before usbip.start(), covering bring-up.)
 *
 * THREADING NOTE (load-bearing): usbhost_submit_async and usbhost_cancel_ep,
 * and through them submit_xfer / xfer_complete_cb / tuh_*_xfer, MUST be called
 * from the main-thread tuh_task context, NOT directly from an lwIP
 * async_context callback (lwIP RAW recv/sent callbacks fire under the cyw43
 * async_context). Touching TinyUSB host state from the async_context corrupts
 * the internal _usbh_data; the transport (usbip_server_rp2.c) is responsible for
 * marshalling submits to the main thread (mirror mp_usbh_schedule_task /
 * __wrap_hcd_event_handler in mp_usbh.c).
 *
 * Reuse-verbatim helpers ported unchanged from usbhost.c: busid_eq,
 * ep_mutex_index, find_slot_by_busid / find_slot_by_devaddr / find_free_slot,
 * get_endpoint_mps, speed_to_usbip, xfer_result_to_errno, parse_config_desc.
 * Gotchas preserved from the esp32 history: CFG_TUH_API_EDPT_XFER=1 (set in the
 * board cmake; without it the user complete_cb is silently dropped); the
 * tuh_xfer_t setup/buflen UNION hazard (control: set ONLY xfer.setup); MPS
 * no-fallback (an IN with no cached MPS is refused, never silently rounded to
 * 64); the [setup(8)|data] buffer layout with IN-copy clamps (control reads from
 * buf+8, bulk from buf+0; clamp to both payload_len and in_capacity); the DWC2
 * tuh_edpt_close+tuh_edpt_open channel-recovery on abort (never close EP0).
 */

#include "usbhost.h"

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "py/mphal.h"
#include "py/runtime.h"

#ifndef NO_QSTR
#include "tusb.h"
#include "host/usbh.h"
#include "host/hcd.h"
#include "mp_usbh.h"
#endif

/* Compile-time defaults; override at link time with -DUSBHOST_*=N. */
#ifndef USBHOST_MAX_DEVICES
#define USBHOST_MAX_DEVICES 4
#endif
#ifndef USBHOST_MAX_TRANSFER
#define USBHOST_MAX_TRANSFER (64 * 1024)
#endif
#ifndef USBHOST_MAX_ENDPOINTS
#define USBHOST_MAX_ENDPOINTS 16
#endif
#ifndef USBHOST_CONTROL_TIMEOUT_MS
#define USBHOST_CONTROL_TIMEOUT_MS 5000
#endif

/* Inflight records (and their payload buffers) are served from a static pool
 * rather than libc malloc: submit/complete/cancel run at thread level but the
 * buffer is referenced only from C state (current_inflight[]), so the GC heap is
 * not an option (it would collect the buffer), and the tiny libc heap panics on
 * failure (pico-sdk PICO_MALLOC_PANIC). The RP2350 host is full-speed, so a 2 KiB
 * data region per URB covers control re-enumeration and FS bulk/interrupt reads;
 * a transfer that needs more is refused with -EMSGSIZE. BUF_CAP carries the
 * control [setup(8)|data] layout plus an MPS-rounding margin. */
#ifndef USBHOST_URB_SLOTS
#define USBHOST_URB_SLOTS 4
#endif
#ifndef USBHOST_BUF_CAP
#define USBHOST_BUF_CAP (8 + 2048 + 64)
#endif

#ifndef TUSB_CLASS_HUB
#define TUSB_CLASS_HUB 0x09
#endif

#ifndef BOARD_TUH_RHPORT
#define BOARD_TUH_RHPORT 0
#endif

/* Debug print, OFF by default; toggled by usbhost_set_verbose. The esp32
 * backend used ESP_LOGI/W/E with a static TAG; here a single macro keyed off
 * s_verbose covers all three levels. Kept terse so a verbose session does not
 * starve the cooperative loop. */
#define USBHOST_DBG(...)                          \
    do {                                          \
        if (s_verbose) {                          \
            mp_printf(&mp_plat_print, "usbhost: " __VA_ARGS__); \
            mp_printf(&mp_plat_print, "\n");      \
        }                                         \
    } while (0)

static bool s_verbose = false;

/* -------------------------------------------------------------------------
 * Per-device slot and endpoint cache
 * ------------------------------------------------------------------------- */

typedef struct {
    uint8_t  address;
    uint8_t  attributes;       /* bmAttributes; bits[1:0] = transfer type */
    uint16_t max_packet_size;
    uint8_t  interval;
    bool     opened;
} usbhost_ep_t;

struct usbhost_inflight; /* fwd decl */

typedef struct {
    bool               in_use;
    uint8_t            dev_addr;
    usbip_dev_record_t device;
    uint8_t            num_endpoints;
    usbhost_ep_t       endpoints[USBHOST_MAX_ENDPOINTS];
    /* Currently-submitted async inflight per (ep,dir). Set by submit_xfer
     * before calling tuh_*_xfer; cleared by xfer_complete_cb or by
     * usbhost_cancel_ep. Indexed by ep_mutex_index(). No CAS arbitration:
     * completion and cancel both run on the cooperative main thread and
     * cannot interleave. */
    struct usbhost_inflight *current_inflight[32];
} usbhost_slot_t;

typedef struct {
    bool           started;
    usbhost_slot_t devices[USBHOST_MAX_DEVICES];
} usbhost_state_t;

static usbhost_state_t s_state;

/* Descriptor caches: populated by tuh_enum_descriptor_device_cb and
 * tuh_enum_descriptor_configuration_cb (called during TinyUSB's own
 * enumeration before tuh_mount_cb fires), so enumerate_device can build the
 * usbip record without re-issuing any control transfers. Re-fetching after
 * enumeration fails: devices return STALL on a second GET_DESCRIPTOR once a
 * class driver has claimed the interface (the dabao DUT does this). */
#define USBHOST_CFG_DESC_MAX_LEN 512
typedef struct {
    bool                valid;
    tusb_desc_device_t  device;
    uint16_t            cfg_len;
    uint8_t             cfg[USBHOST_CFG_DESC_MAX_LEN] __attribute__((aligned(4)));
} usbhost_desc_cache_t;

static usbhost_desc_cache_t s_desc_cache[CFG_TUH_DEVICE_MAX]; /* indexed by (dev_addr-1) */

/* -------------------------------------------------------------------------
 * Helpers (reuse-verbatim from usbhost.c, mutex/locked suffix dropped)
 * ------------------------------------------------------------------------- */

static bool busid_eq(const char a[USBIP_BUSID_SIZE], const char b[USBIP_BUSID_SIZE])
{
    return memcmp(a, b, USBIP_BUSID_SIZE) == 0;
}

/* EP address -> 5-bit index (direction in bit 4, number in bits[3:0]). Used
 * to index current_inflight[]. */
static uint8_t ep_mutex_index(uint8_t ep_addr)
{
    return (uint8_t)((ep_addr & 0x0F) | ((ep_addr & 0x80) >> 3));
}

static int find_slot_by_busid(const char busid[USBIP_BUSID_SIZE])
{
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        if (s_state.devices[i].in_use &&
            busid_eq(busid, s_state.devices[i].device.busid)) {
            return i;
        }
    }
    return -1;
}

static int find_slot_by_devaddr(uint8_t dev_addr)
{
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        if (s_state.devices[i].in_use &&
            s_state.devices[i].dev_addr == dev_addr) {
            return i;
        }
    }
    return -1;
}

static int find_free_slot(void)
{
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        if (!s_state.devices[i].in_use) {
            return i;
        }
    }
    return -1;
}

static void clear_slot(int slot)
{
    if (slot < 0 || slot >= USBHOST_MAX_DEVICES) {
        return;
    }
    memset(&s_state.devices[slot], 0, sizeof(s_state.devices[slot]));
}

/* Return wMaxPacketSize for the given EP from the cached endpoint descriptor,
 * or 0 if the EP isn't found / wasn't populated.
 *
 * NO FALLBACK to a hardcoded value: if the lookup fails, callers must surface
 * and stop. Returning 64 here would silently mis-chunk on HS bulk endpoints
 * (MPS=512), defeating the purpose of looking up the descriptor. On rp2 FS
 * (MPS=64) the mis-round still corrupts transfers, so keep the discipline. */
static uint16_t get_endpoint_mps(int slot, uint8_t ep_addr)
{
    if (slot < 0 || slot >= USBHOST_MAX_DEVICES ||
        !s_state.devices[slot].in_use) {
        return 0;
    }
    for (uint8_t i = 0; i < s_state.devices[slot].num_endpoints; i++) {
        if (s_state.devices[slot].endpoints[i].address == ep_addr) {
            return s_state.devices[slot].endpoints[i].max_packet_size;
        }
    }
    return 0;
}

static uint32_t speed_to_usbip(tusb_speed_t speed)
{
    switch (speed) {
    case TUSB_SPEED_LOW:  return 1;
    case TUSB_SPEED_FULL: return 2;
    case TUSB_SPEED_HIGH: return 3;
    default:              return 0;
    }
}

static int xfer_result_to_errno(xfer_result_t result)
{
    switch (result) {
    case XFER_RESULT_SUCCESS: return 0;
    case XFER_RESULT_STALLED: return -EPIPE;
    case XFER_RESULT_TIMEOUT: return -ETIMEDOUT;
    default:                  return -EIO;
    }
}

/* Walk raw config descriptor blob, populate USBIP metadata and EP cache. */
static void parse_config_desc(const uint8_t *raw, size_t total,
                              usbip_dev_record_t *desc,
                              usbhost_ep_t *eps_out, uint8_t *num_eps_out)
{
    uint8_t intf_count = 0;
    uint8_t ep_count   = 0;

    if (!raw || !desc || !eps_out || !num_eps_out) {
        return;
    }
    for (size_t off = 0; off + 2 <= total; ) {
        const uint8_t dlen  = raw[off];
        const uint8_t dtype = raw[off + 1];
        if (dlen < 2 || off + dlen > total) {
            break;
        }
        if (dtype == 0x04 && dlen >= 9) {  /* INTERFACE */
            if (intf_count < USBIP_MAX_INTERFACES) {
                desc->interfaces[intf_count].interface_class    = raw[off + 5];
                desc->interfaces[intf_count].interface_subclass = raw[off + 6];
                desc->interfaces[intf_count].interface_protocol = raw[off + 7];
                intf_count++;
            }
        } else if (dtype == 0x05 && dlen >= 7) {  /* ENDPOINT */
            if (ep_count < USBHOST_MAX_ENDPOINTS) {
                eps_out[ep_count].address         = raw[off + 2];
                eps_out[ep_count].attributes      = raw[off + 3];
                eps_out[ep_count].max_packet_size = (uint16_t)raw[off + 4]
                                                  | ((uint16_t)raw[off + 5] << 8);
                eps_out[ep_count].interval        = raw[off + 6];
                eps_out[ep_count].opened          = false;
                ep_count++;
            }
        }
        off += dlen;
    }
    desc->num_interfaces = intf_count;
    *num_eps_out         = ep_count;
}

/* -------------------------------------------------------------------------
 * Inflight record
 *
 * Single-owner async record. No done_sem (the sync wrappers pump cooperatively
 * and read the result through a small on-stack sync context, see below), no
 * atomic completed flag (completion and cancel never interleave on the single
 * cooperative thread), no watchdog timing fields.
 * ------------------------------------------------------------------------- */

typedef struct usbhost_inflight {
    /* Async path callback. */
    void             (*user_cb)(void *ctx, int status, size_t in_len);
    void              *user_ctx;
    /* IN data destination (points at caller's buffer). */
    uint8_t           *in_data;
    size_t             in_capacity;
    /* Transfer shape. */
    bool               is_in;
    bool               is_control;
    size_t             payload_len;
    /* In-use flag for the static pool slot. */
    bool               in_use;
    /* Payload buffer (stable for the TinyUSB transfer lifetime), layout
     * [setup(8)|data] for control, [data] for bulk/interrupt. */
    size_t             buf_len;
    uint8_t            buf[USBHOST_BUF_CAP] __attribute__((aligned(4)));
    /* Slot/index back-reference for clearing current_inflight on completion. */
    int                slot_idx;
    uint8_t            ep_idx;
} usbhost_inflight_t;

/* Static inflight pool. The DWC2/rp2040-lineage host DMAs from any RAM (single
 * SRAM), and CFG_TUH_MEM_ALIGN is 4-byte; the embedded buf is 4-byte aligned. */
static usbhost_inflight_t s_inflight_pool[USBHOST_URB_SLOTS];

static usbhost_inflight_t *inflight_alloc(void)
{
    for (size_t i = 0; i < USBHOST_URB_SLOTS; i++) {
        if (!s_inflight_pool[i].in_use) {
            memset(&s_inflight_pool[i], 0, sizeof(s_inflight_pool[i]));
            s_inflight_pool[i].in_use = true;
            return &s_inflight_pool[i];
        }
    }
    return NULL;
}

static void inflight_free(usbhost_inflight_t *inflight)
{
    if (!inflight) {
        return;
    }
    inflight->in_use = false;
}

/* Complete callback; runs in main-thread tuh_task context (mp_usbh_task ->
 * tuh_task_ext). Single completion path: clear current_inflight[ep], copy IN
 * data, fire the user cb. */
static void xfer_complete_cb(tuh_xfer_t *xfer)
{
    usbhost_inflight_t *inflight = (usbhost_inflight_t *)(uintptr_t)xfer->user_data;
    if (!inflight) {
        return;
    }

    /* Clear the slot's current_inflight back-pointer. */
    if (inflight->slot_idx >= 0 && inflight->slot_idx < USBHOST_MAX_DEVICES &&
        inflight->ep_idx < 32) {
        if (s_state.devices[inflight->slot_idx].current_inflight[inflight->ep_idx]
            == inflight) {
            s_state.devices[inflight->slot_idx].current_inflight[inflight->ep_idx] = NULL;
        }
    }

    int status = xfer_result_to_errno(xfer->result);
    size_t in_len = 0;

    if (status == 0 && inflight->is_in && inflight->payload_len > 0) {
        /* TinyUSB reports actual_len as the data-stage byte count for both
         * control and bulk/interrupt transfers (setup is not counted). Source
         * buffer for control is inflight->buf + 8 because we kept the
         * [setup(8) | data] layout; bulk/interrupt read from inflight->buf. */
        in_len = (size_t)xfer->actual_len;
        if (in_len > inflight->payload_len) {
            in_len = inflight->payload_len;
        }
        if (in_len > 0 && inflight->in_data && inflight->in_capacity > 0) {
            const uint8_t *src = inflight->is_control
                ? (inflight->buf + 8) : inflight->buf;
            size_t copy_len = (in_len < inflight->in_capacity)
                ? in_len : inflight->in_capacity;
            memcpy(inflight->in_data, src, copy_len);
            in_len = copy_len;
        }
    }

    USBHOST_DBG("cb ep=0x%02x result=%d alen=%u in_len=%u",
                xfer->ep_addr, (int)xfer->result,
                (unsigned)xfer->actual_len, (unsigned)in_len);

    void (*cb)(void *, int, size_t) = inflight->user_cb;
    void *ctx = inflight->user_ctx;
    inflight_free(inflight);
    if (cb) {
        cb(ctx, status, in_len);
    }
}

/* -------------------------------------------------------------------------
 * Core submit helper (used by the async entry and the cooperative-pump sync
 * wrappers).
 *
 * Allocates inflight + payload buffer, fills tuh_xfer_t, submits.
 * Returns 0 on success, negative errno on immediate failure (no cb fired).
 * On success the user cb fires later from xfer_complete_cb.
 * ------------------------------------------------------------------------- */

static int submit_xfer(const char busid[USBIP_BUSID_SIZE],
                       uint8_t ep_addr, bool is_control,
                       const usbip_setup_packet_t *setup,
                       const uint8_t *out_data, size_t out_len,
                       uint8_t *in_data, size_t in_capacity,
                       void (*user_cb)(void *ctx, int status, size_t in_len),
                       void *user_ctx)
{
    const bool is_in = is_control
        ? (setup != NULL && (setup->bmRequestType & 0x80) != 0)
        : (ep_addr & 0x80) != 0;
    const size_t payload_len = is_in ? in_capacity : out_len;

    if (payload_len > USBHOST_MAX_TRANSFER) {
        return -EMSGSIZE;
    }

    /* For control transfers TinyUSB moves exactly setup->wLength bytes through
     * the data-stage buffer (inflight->buf + 8), taken from the host-supplied
     * setup packet, INDEPENDENT of transfer_buffer_length / payload_len. The
     * data region must therefore be sized by wLength, not payload_len, or a host
     * (malformed or hostile) that sends wLength > transfer_buffer_length would
     * make the HCD write past the buffer (heap overflow). Bound it too. */
    uint16_t ctrl_wlen = (is_control && setup) ? setup->wLength : 0;
    if (is_control && (size_t)ctrl_wlen > USBHOST_MAX_TRANSFER) {
        return -EMSGSIZE;
    }

    uint8_t dev_addr = 0;
    size_t xfer_payload = payload_len;

    /* MPS lookup for IN URB length-rounding. The kernel-side cdc-acm passes
     * in_capacity = NR_BUFFERS * MPS (a multiple of MPS), so the rounding is a
     * no-op in the common case; it handles odd in_capacity by extending to the
     * next MPS boundary. MPS comes from the parsed endpoint descriptor with NO
     * hardcoded fallback: MPS==0 (descriptor not cached) fails with -ENODEV
     * rather than silently mis-rounding. */
    bool mps_unavailable = false;
    int slot = find_slot_by_busid(busid);
    if (slot >= 0) {
        dev_addr = s_state.devices[slot].dev_addr;
        if (!is_control && is_in && in_capacity > 0) {
            uint16_t mps = get_endpoint_mps(slot, ep_addr);
            if (mps == 0) {
                mps_unavailable = true;
            } else if ((xfer_payload % mps) != 0) {
                xfer_payload = ((xfer_payload + mps - 1) / mps) * mps;
            }
        }
    }

    if (dev_addr == 0) {
        return -ENODEV;
    }
    if (mps_unavailable) {
        USBHOST_DBG("submit: IN ep=0x%02x has no cached MPS, refusing (slot=%d dev=%u)",
                    ep_addr, slot, dev_addr);
        return -ENODEV;
    }

    USBHOST_DBG("submit dev=%.32s ep=0x%02x dir=%s len=%u",
                busid, ep_addr,
                is_control ? "CTRL" : (is_in ? "IN" : "OUT"),
                (unsigned)payload_len);

    /* Control data region = wLength (what the HCD moves); bulk/interrupt =
     * the MPS-rounded payload. Refuse anything that does not fit a pool slot. */
    size_t buf_len = is_control ? (8u + (size_t)ctrl_wlen) : xfer_payload;
    if (buf_len == 0) {
        buf_len = 1;
    }
    if (buf_len > USBHOST_BUF_CAP) {
        return -EMSGSIZE;
    }

    usbhost_inflight_t *inflight = inflight_alloc();
    if (!inflight) {
        return -ENOMEM;
    }
    inflight->user_cb     = user_cb;
    inflight->user_ctx    = user_ctx;
    inflight->is_in       = is_in;
    inflight->is_control  = is_control;
    inflight->in_data     = in_data;
    inflight->in_capacity = in_capacity;
    inflight->payload_len = payload_len;
    inflight->slot_idx    = slot;
    inflight->ep_idx      = ep_mutex_index(is_control ? 0x00 : ep_addr);
    inflight->buf_len     = buf_len;
    /* inflight_alloc zeroed the whole record including buf, so the unused tail of
     * the data region is already clear (no stale bytes sent on a short OUT). */

    if (is_control && setup) {
        memcpy(inflight->buf, setup, 8);
        if (!is_in && out_len > 0 && out_data) {
            /* Clamp the OUT data copy to the data region (wLength), not the
             * host's transfer_buffer_length. */
            size_t copy = (out_len < (size_t)ctrl_wlen) ? out_len : (size_t)ctrl_wlen;
            memcpy(inflight->buf + 8, out_data, copy);
        }
    } else if (!is_in && out_len > 0 && out_data) {
        size_t copy = (out_len < xfer_payload) ? out_len : xfer_payload;
        memcpy(inflight->buf, out_data, copy);
    }

    tuh_xfer_t xfer;
    memset(&xfer, 0, sizeof(xfer));
    xfer.daddr       = dev_addr;
    xfer.ep_addr     = is_control ? 0 : ep_addr;
    xfer.complete_cb = xfer_complete_cb;
    xfer.user_data   = (uintptr_t)inflight;
    if (is_control) {
        /* tuh_xfer_t.setup and .buflen share a UNION (usbh.h). For control
         * transfers set ONLY setup; the data-stage size is taken from the
         * setup packet's wLength, not buflen. Setting buflen here would
         * overwrite setup with the integer cast as a pointer and fault inside
         * tuh_control_xfer. buffer points at the data stage; our inflight->buf
         * layout is [setup(8)|data], so buffer = inflight->buf+8. This bug
         * bites identically on rp2 DWC2. */
        xfer.setup  = (const tusb_control_request_t *)inflight->buf;
        xfer.buffer = inflight->buf + 8;
    } else {
        xfer.buffer = inflight->buf;
        xfer.buflen = (uint32_t)xfer_payload;
    }

    /* Publish the inflight as the current in-flight on this (slot, ep) BEFORE
     * submitting. usbhost_cancel_ep reads this to find the URB to abort. */
    if (slot >= 0 && inflight->ep_idx < 32) {
        s_state.devices[slot].current_inflight[inflight->ep_idx] = inflight;
    }

    bool ok = is_control ? tuh_control_xfer(&xfer) : tuh_edpt_xfer(&xfer);

    if (!ok) {
        USBHOST_DBG("tuh_%s_xfer rejected ep=0x%02x addr=%u",
                    is_control ? "control" : "edpt", ep_addr, dev_addr);
        /* Submit failed: clear the slot we just published, free, no cb. */
        if (slot >= 0 && inflight->ep_idx < 32 &&
            s_state.devices[slot].current_inflight[inflight->ep_idx] == inflight) {
            s_state.devices[slot].current_inflight[inflight->ep_idx] = NULL;
        }
        inflight_free(inflight);
        return -EIO;
    }
    return 0;
}

/* -------------------------------------------------------------------------
 * Enumeration
 *
 * No tuh_mount_hook on rp2: enumerate_device is driven by usbhost_start()'s
 * seed loop and usbhost_get_devices()'s lazy re-scan. It reads only cached
 * descriptors (populated by the enum cbs), so it does NOT issue synchronous
 * descriptor fetches and is safe to call directly (no tuh_task re-entrance
 * concern). tuh_edpt_open is a host-stack call but enumerate_device runs on
 * the main thread, outside the tuh_task callback dispatch, when called from
 * start/get_devices.
 * ------------------------------------------------------------------------- */

static void enumerate_device(uint8_t dev_addr)
{
    if (dev_addr < 1 || dev_addr > CFG_TUH_DEVICE_MAX) {
        USBHOST_DBG("enumerate: addr=%u out of range", dev_addr);
        return;
    }
    usbhost_desc_cache_t *dcache = &s_desc_cache[dev_addr - 1];
    if (!dcache->valid) {
        USBHOST_DBG("enumerate: no cached descriptors for addr=%u", dev_addr);
        return;
    }
    tusb_desc_device_t dev_desc = dcache->device;

    if (dev_desc.bDeviceClass == TUSB_CLASS_HUB) {
        USBHOST_DBG("hub at addr=%u, skipping", dev_addr);
        return;
    }

    uint8_t cfg_buf[USBHOST_CFG_DESC_MAX_LEN] __attribute__((aligned(4)));
    memset(cfg_buf, 0, sizeof(cfg_buf));
    bool has_cfg = (dcache->cfg_len > 0);
    if (has_cfg) {
        memcpy(cfg_buf, dcache->cfg, dcache->cfg_len);
    }

    usbip_dev_record_t desc;
    memset(&desc, 0, sizeof(desc));
    desc.present             = true;
    desc.busnum              = 1;
    desc.devnum              = dev_addr;
    desc.speed               = speed_to_usbip(tuh_speed_get(dev_addr));
    desc.id_vendor           = dev_desc.idVendor;
    desc.id_product          = dev_desc.idProduct;
    desc.bcd_device          = dev_desc.bcdDevice;
    desc.device_class        = dev_desc.bDeviceClass;
    desc.device_subclass     = dev_desc.bDeviceSubClass;
    desc.device_protocol     = dev_desc.bDeviceProtocol;
    desc.num_configurations  = dev_desc.bNumConfigurations;

    usbhost_ep_t eps[USBHOST_MAX_ENDPOINTS];
    memset(eps, 0, sizeof(eps));
    uint8_t num_eps = 0;

    if (has_cfg) {
        uint16_t total_len = (uint16_t)cfg_buf[2] | ((uint16_t)cfg_buf[3] << 8);
        if (total_len > sizeof(cfg_buf)) {
            total_len = sizeof(cfg_buf);
        }
        if (total_len >= 6) {
            desc.configuration_value = cfg_buf[5];
        }
        parse_config_desc(cfg_buf, total_len, &desc, eps, &num_eps);
    }

    for (uint8_t i = 0; i < desc.num_interfaces; i++) {
        if (desc.interfaces[i].interface_class == TUSB_CLASS_HUB) {
            USBHOST_DBG("hub interface at addr=%u, skipping", dev_addr);
            return;
        }
    }

    snprintf(desc.busid, sizeof(desc.busid), "1-%u", dev_addr);
    snprintf(desc.path,  sizeof(desc.path),  "/annealage-pod/1-%u", dev_addr);

    int existing = find_slot_by_devaddr(dev_addr);
    if (existing >= 0) {
        /* Already enumerated into a slot; nothing to do (lazy re-scan re-enters
         * here for the same address). */
        return;
    }
    int slot = find_free_slot();
    if (slot < 0) {
        USBHOST_DBG("no free device slot (max=%d)", USBHOST_MAX_DEVICES);
        return;
    }
    s_state.devices[slot].in_use        = true;
    s_state.devices[slot].dev_addr      = dev_addr;
    s_state.devices[slot].device        = desc;
    s_state.devices[slot].num_endpoints = num_eps;
    memcpy(s_state.devices[slot].endpoints, eps, sizeof(eps[0]) * num_eps);

    /* Open non-zero endpoints. */
    for (uint8_t i = 0; i < num_eps; i++) {
        tusb_desc_endpoint_t ep_desc;
        memset(&ep_desc, 0, sizeof(ep_desc));
        ep_desc.bLength             = 7;
        ep_desc.bDescriptorType     = 0x05;
        ep_desc.bEndpointAddress    = eps[i].address;
        ep_desc.bmAttributes.xfer   = eps[i].attributes & 0x03;
        ep_desc.wMaxPacketSize      = eps[i].max_packet_size;
        ep_desc.bInterval           = eps[i].interval;

        bool ok = tuh_edpt_open(dev_addr, &ep_desc);
        s_state.devices[slot].endpoints[i].opened = ok;
        if (!ok) {
            USBHOST_DBG("tuh_edpt_open ep=0x%02x failed addr=%u",
                        eps[i].address, dev_addr);
        }
    }

    USBHOST_DBG("exported busid=%s vid=%04x pid=%04x num_intf=%u num_ep=%u",
                desc.busid, desc.id_vendor, desc.id_product,
                desc.num_interfaces, num_eps);
}

/* Re-scan: TinyUSB has no mount hook wired on rp2, so walk tuh_mounted() and
 * enumerate any newly-present address, and drop any slot whose device is no
 * longer mounted. tuh_edpt_open / tuh_speed_get inside make this a host-stack
 * mutator, so it MUST run at thread level only (called from usbhost_start();
 * NOT from the PendSV query accessors). */
static void rescan_mounted(void)
{
    /* Add newly-mounted devices. */
    for (uint8_t dev_addr = 1; dev_addr <= CFG_TUH_DEVICE_MAX; dev_addr++) {
        if (tuh_mounted(dev_addr) && find_slot_by_devaddr(dev_addr) < 0) {
            enumerate_device(dev_addr);
        }
    }
    /* Drop slots whose device disappeared (unmount). The enum cache for that
     * address is left stale; it is overwritten on the next mount of the same
     * address by the enum cbs. */
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        if (s_state.devices[i].in_use &&
            !tuh_mounted(s_state.devices[i].dev_addr)) {
            uint8_t da = s_state.devices[i].dev_addr;
            if (da >= 1 && da <= CFG_TUH_DEVICE_MAX) {
                s_desc_cache[da - 1].valid = false;
            }
            clear_slot(i);
        }
    }
}

/* -------------------------------------------------------------------------
 * Weak enum-descriptor overrides (reuse-verbatim from usbhost.c)
 * ------------------------------------------------------------------------- */

/* Override the weak tuh_enum_descriptor_device_cb from usbh.c. Called during
 * TinyUSB's enumeration with the device descriptor. */
void tuh_enum_descriptor_device_cb(uint8_t daddr, const tusb_desc_device_t *desc_device)
{
    if (daddr >= 1 && daddr <= CFG_TUH_DEVICE_MAX && desc_device) {
        s_desc_cache[daddr - 1].device = *desc_device;
        USBHOST_DBG("dev_cache addr=%u vid=%04x pid=%04x",
                    daddr, desc_device->idVendor, desc_device->idProduct);
    }
}

/* Override the weak tuh_enum_descriptor_configuration_cb from usbh.c. Cache the
 * config descriptor and return TRUE so TinyUSB completes enumeration by sending
 * SET_CONFIGURATION and firing tuh_mount_cb (which makes tuh_mounted() true so
 * usbhost_start()'s seed picks the device up).
 *
 * NOTE: the esp32 backend returned false here intending to keep the device in
 * Address state. That does NOT translate to this TinyUSB pin: in usbh.c's
 * ENUM_SET_CONFIG, returning false means "reject this configuration, try the
 * next index", and for a single-config device that trips
 * TU_ASSERT(config_idx < bNumConfigurations) and ABORTS enumeration - the device
 * never mounts. So we return true. Class drivers are off (CFG_TUH_CDC/MSC/HID=0),
 * so SET_CONFIGURATION claims no interface and every endpoint stays reachable for
 * raw URB forwarding; the USB/IP host issues its own SET_CONFIGURATION as part of
 * its enumeration, forwarded transparently (a re-set of the same config). */
bool tuh_enum_descriptor_configuration_cb(uint8_t daddr, uint8_t cfg_index,
                                          const tusb_desc_configuration_t *desc_config)
{
    (void)cfg_index;
    if (daddr >= 1 && daddr <= CFG_TUH_DEVICE_MAX && desc_config) {
        usbhost_desc_cache_t *cache = &s_desc_cache[daddr - 1];
        uint16_t total_len = tu_le16toh(desc_config->wTotalLength);
        if (total_len > USBHOST_CFG_DESC_MAX_LEN) {
            total_len = USBHOST_CFG_DESC_MAX_LEN;
        }
        memcpy(cache->cfg, desc_config, total_len);
        cache->cfg_len = total_len;
        cache->valid   = true;
        USBHOST_DBG("cfg_cache addr=%u len=%u (SET_CONFIGURATION proceeds)", daddr, total_len);
    }
    return true;
}

/* -------------------------------------------------------------------------
 * Public API: usbhost_start
 * ------------------------------------------------------------------------- */

int usbhost_start(void)
{
    if (s_state.started) {
        return 0;
    }

    memset(&s_state, 0, sizeof(s_state));

    /* Only call mp_usbh_init_tuh() if TinyUSB has not been initialised yet.
     * machine.USBHost().active(True) may have already done it; calling it twice
     * reinitialises the USB PHY and drops any already-connected device. */
    if (!tusb_inited()) {
        mp_usbh_init_tuh();
    }

    s_state.started = true;

    /* Seed the slot table with any device TinyUSB already mounted before
     * usbhost_start(). There is no mount hook on rp2, so this seed plus the
     * lazy re-scan in usbhost_get_devices() are the only enumeration triggers. */
    rescan_mounted();

    USBHOST_DBG("started (rhport=%d)", BOARD_TUH_RHPORT);
    return 0;
}

/* -------------------------------------------------------------------------
 * Public API: device queries
 * ------------------------------------------------------------------------- */

size_t usbhost_get_devices(usbip_dev_record_t *out, size_t max)
{
    if (!out || max == 0) {
        return 0;
    }
    /* PURE READ of the slot table: NO rescan / tuh_* here. The USB/IP transport
     * calls this from its lwIP RAW recv callback (PendSV), which preempts the
     * main thread; calling tuh_edpt_open / tuh_speed_get from PendSV would race
     * tuh_task and corrupt _usbh_data. The slot table is populated at thread
     * level by usbhost_start()'s seed-from-tuh_mounted(). (Hot-plug-after-start
     * refresh needs a thread-level rescan trigger - a follow-up; the wired DUT
     * is present before usbip.start(), so the seed covers the bring-up case.) */
    size_t copied = 0;
    for (int i = 0; i < USBHOST_MAX_DEVICES && copied < max; i++) {
        if (s_state.devices[i].in_use) {
            out[copied++] = s_state.devices[i].device;
        }
    }
    return copied;
}

bool usbhost_get_device_by_busid(const char busid[USBIP_BUSID_SIZE],
                                 usbip_dev_record_t *out)
{
    if (!busid || !out) {
        return false;
    }
    /* PURE READ (PendSV-safe); see usbhost_get_devices. No rescan here. */
    int slot = find_slot_by_busid(busid);
    if (slot >= 0) {
        *out = s_state.devices[slot].device;
        return true;
    }
    return false;
}

bool usbhost_get_cached_device_desc(const char busid[USBIP_BUSID_SIZE],
                                    uint8_t *out, size_t cap, size_t *out_len)
{
    if (!busid || !out || !out_len) {
        return false;
    }
    *out_len = 0;
    int slot = find_slot_by_busid(busid);
    if (slot >= 0) {
        uint8_t dev_addr = s_state.devices[slot].dev_addr;
        if (dev_addr >= 1 && dev_addr <= CFG_TUH_DEVICE_MAX) {
            const usbhost_desc_cache_t *dc = &s_desc_cache[dev_addr - 1];
            if (dc->valid) {
                /* sizeof(tusb_desc_device_t) is 18 per USB-2.0 spec. */
                const size_t desc_size = sizeof(tusb_desc_device_t);
                size_t n = (cap < desc_size) ? cap : desc_size;
                memcpy(out, &dc->device, n);
                *out_len = n;
                return true;
            }
        }
    }
    return false;
}

bool usbhost_get_cached_config_desc(const char busid[USBIP_BUSID_SIZE],
                                    uint8_t *out, size_t cap, size_t *out_len)
{
    if (!busid || !out || !out_len) {
        return false;
    }
    *out_len = 0;
    int slot = find_slot_by_busid(busid);
    if (slot >= 0) {
        uint8_t dev_addr = s_state.devices[slot].dev_addr;
        if (dev_addr >= 1 && dev_addr <= CFG_TUH_DEVICE_MAX) {
            const usbhost_desc_cache_t *dc = &s_desc_cache[dev_addr - 1];
            if (dc->valid && dc->cfg_len > 0) {
                size_t n = (cap < dc->cfg_len) ? cap : dc->cfg_len;
                memcpy(out, dc->cfg, n);
                *out_len = n;
                return true;
            }
        }
    }
    return false;
}

bool usbhost_is_interrupt_endpoint(const char busid[USBIP_BUSID_SIZE],
                                   uint8_t ep_num, uint8_t direction)
{
    if (!busid) {
        return false;
    }
    const uint8_t ep_addr = (uint8_t)((ep_num & 0x7F) | (direction ? 0x80 : 0x00));
    int slot = find_slot_by_busid(busid);
    if (slot >= 0) {
        for (uint8_t i = 0; i < s_state.devices[slot].num_endpoints; i++) {
            if (s_state.devices[slot].endpoints[i].address == ep_addr) {
                return (s_state.devices[slot].endpoints[i].attributes & 0x03) == 0x03;
            }
        }
    }
    return false;
}

/* -------------------------------------------------------------------------
 * Public API: usbhost_submit_async (PRIMARY entry)
 *
 * The transport drives raw URBs through this entry and receives completion via
 * the callback. MUST be called from the main-thread tuh_task context (see file
 * header). Returns 0 if the submit was accepted (cb fires later), negative
 * errno on immediate failure (cb will NOT fire).
 * ------------------------------------------------------------------------- */

int usbhost_submit_async(const char busid[USBIP_BUSID_SIZE],
                         uint8_t ep_addr, bool is_control,
                         const usbip_setup_packet_t *setup,
                         const uint8_t *out_data, size_t out_len,
                         uint8_t *in_data, size_t in_capacity,
                         void (*cb)(void *ctx, int status, size_t in_len),
                         void *ctx)
{
    if (!busid || !cb) {
        return -EINVAL;
    }
    return submit_xfer(busid, ep_addr, is_control, setup,
                       out_data, out_len, in_data, in_capacity, cb, ctx);
}

/* -------------------------------------------------------------------------
 * Public API: usbhost_cancel_ep
 *
 * Abort the in-flight URB on an EP. MUST be called from the main-thread
 * tuh_task context. Simplified from the esp32 original: no CAS arbitration
 * (completion and cancel never interleave), no usbh_edpt_busy probe, no synth
 * counters, no done_sem branch. Reduces to: abort, clear current_inflight,
 * deliver -ECONNRESET to the user cb, then close+open the EP (DWC2 channel
 * recovery, never EP0).
 * ------------------------------------------------------------------------- */

void usbhost_cancel_ep(const char busid[USBIP_BUSID_SIZE], uint8_t ep_addr)
{
    if (!busid) {
        return;
    }
    int     slot     = find_slot_by_busid(busid);
    if (slot < 0) {
        return;
    }
    uint8_t dev_addr = s_state.devices[slot].dev_addr;
    uint8_t ep_idx   = ep_mutex_index(ep_addr);
    if (dev_addr == 0) {
        return;
    }

    /* Only act if there is actually an URB in flight on this EP. */
    usbhost_inflight_t *inflight = NULL;
    if (ep_idx < 32) {
        inflight = s_state.devices[slot].current_inflight[ep_idx];
    }
    if (inflight == NULL) {
        USBHOST_DBG("cancel_ep dev=%.32s ep=0x%02x no_current_inflight",
                    busid, ep_addr);
        return;
    }

    USBHOST_DBG("cancel_ep dev=%.32s ep=0x%02x", busid, ep_addr);
    bool abort_ok = tuh_edpt_abort_xfer(dev_addr, ep_addr);
    if (!abort_ok) {
        USBHOST_DBG("tuh_edpt_abort_xfer ep=0x%02x returned false", ep_addr);
    }

    /* On the bundled TinyUSB pin tuh_edpt_abort_xfer does NOT fire the
     * completion callback (usbh.c just dequeues the transfer), so the synthesise
     * block below is the PRIMARY completion path on cancel, not a fallback. The
     * re-read of current_inflight is defensive: if a future TinyUSB bump starts
     * firing the abort callback, xfer_complete_cb would have cleared the slot on
     * this same cooperative thread before we get here, so trust the slot, not the
     * cached pointer, to avoid a double-complete / double-free. */
    inflight = (ep_idx < 32) ? s_state.devices[slot].current_inflight[ep_idx] : NULL;
    if (inflight != NULL) {
        /* Abort did not deliver a natural completion: synthesise it. Clear the
         * slot, deliver -ECONNRESET through the user cb, free the record. */
        if (ep_idx < 32) {
            s_state.devices[slot].current_inflight[ep_idx] = NULL;
        }
        void (*pending_cb)(void *, int, size_t) = inflight->user_cb;
        void *pending_ctx = inflight->user_ctx;
        inflight_free(inflight);
        if (pending_cb) {
            pending_cb(pending_ctx, -ECONNRESET, 0);
        }
    }

    /* DWC2 channel reset: after tuh_edpt_abort_xfer the channel can stay
     * half-allocated and subsequent tuh_edpt_xfer calls return false. Close+open
     * forces a clean teardown + re-allocation. EP0 control is excluded; it is
     * opened implicitly at SetAddress and tuh_edpt_close on EP0 is undefined. */
    if (ep_addr != 0) {
        usbhost_ep_t ep_cache;
        memset(&ep_cache, 0, sizeof(ep_cache));
        bool found = false;
        for (uint8_t i = 0; i < s_state.devices[slot].num_endpoints; i++) {
            if (s_state.devices[slot].endpoints[i].address == ep_addr) {
                ep_cache = s_state.devices[slot].endpoints[i];
                found = true;
                break;
            }
        }
        if (found) {
            bool close_ok = tuh_edpt_close(dev_addr, ep_addr);
            tusb_desc_endpoint_t ep_desc;
            memset(&ep_desc, 0, sizeof(ep_desc));
            ep_desc.bLength             = 7;
            ep_desc.bDescriptorType     = 0x05;
            ep_desc.bEndpointAddress    = ep_cache.address;
            ep_desc.bmAttributes.xfer   = ep_cache.attributes & 0x03;
            ep_desc.wMaxPacketSize      = ep_cache.max_packet_size;
            ep_desc.bInterval           = ep_cache.interval;
            bool open_ok = tuh_edpt_open(dev_addr, &ep_desc);
            USBHOST_DBG("ep_reset ep=0x%02x close=%d open=%d",
                        ep_addr, (int)close_ok, (int)open_ok);
        }
    }
}

/* -------------------------------------------------------------------------
 * Synchronous wrappers (secondary; the transport uses the async entry)
 *
 * Cooperative pump: the single thread cannot block on a semaphore (there is no
 * other thread to run tuh_task and complete the URB - that would deadlock).
 * Instead submit through submit_xfer with a tiny on-stack completion context
 * and pump mp_usbh_task / mp_event_handle_nowait until the cb fires, honouring
 * cancel and the control timeout. Modeled on mp_usbh_msc_wait_complete in
 * mp_usbh.c.
 * ------------------------------------------------------------------------- */

typedef struct {
    volatile bool done;
    int           status;
    size_t        in_len;
} sync_ctx_t;

static void sync_complete_cb(void *ctx, int status, size_t in_len)
{
    sync_ctx_t *sc = (sync_ctx_t *)ctx;
    sc->status = status;
    sc->in_len = in_len;
    sc->done   = true;
}

static int sync_xfer(const char busid[USBIP_BUSID_SIZE],
                     uint8_t ep_addr, bool is_control,
                     const usbip_setup_packet_t *setup,
                     const uint8_t *out_data, size_t out_len,
                     uint8_t *in_data, size_t in_capacity, size_t *in_len,
                     volatile bool *cancel)
{
    if (!busid || !in_len) {
        return -EINVAL;
    }
    *in_len = 0;

    /* Resolve dev_addr up front so cancel/timeout can abort the right EP. */
    int slot = find_slot_by_busid(busid);
    if (slot < 0) {
        return -ENODEV;
    }
    uint8_t dev_addr = s_state.devices[slot].dev_addr;
    if (dev_addr == 0) {
        return -ENODEV;
    }

    sync_ctx_t sc = { .done = false, .status = -EIO, .in_len = 0 };
    int rc = submit_xfer(busid, ep_addr, is_control, setup,
                         out_data, out_len, in_data, in_capacity,
                         sync_complete_cb, &sc);
    if (rc != 0) {
        return rc;
    }

    const uint8_t abort_ep = is_control ? 0x00 : ep_addr;
    uint32_t start_ms = mp_hal_ticks_ms();
    bool cancelled = false;

    while (!sc.done) {
        /* Drive the host stack and the scheduler so the URB can complete and
         * xfer_complete_cb (which sets sc.done) can run on this same thread. */
        mp_usbh_task();
        mp_event_handle_nowait();

        if (!cancelled && cancel && *cancel) {
            cancelled = true;
            tuh_edpt_abort_xfer(dev_addr, abort_ep);
        }
        if (is_control &&
            (mp_hal_ticks_ms() - start_ms) >= USBHOST_CONTROL_TIMEOUT_MS) {
            tuh_edpt_abort_xfer(dev_addr, 0);
            /* Pump a little longer to let the abort completion land. */
            uint32_t t2 = mp_hal_ticks_ms();
            while (!sc.done && (mp_hal_ticks_ms() - t2) < 50) {
                mp_usbh_task();
                mp_event_handle_nowait();
            }
            break;
        }
        if (!sc.done) {
            mp_hal_delay_ms(1);
        }
    }

    *in_len = sc.in_len;
    USBHOST_DBG("sync_complete dev=%.32s ep=0x%02x status=%d actual=%u",
                busid, ep_addr, sc.status, (unsigned)*in_len);
    return sc.status;
}

int usbhost_control_transfer(const char busid[USBIP_BUSID_SIZE],
                             const usbip_setup_packet_t *setup,
                             const uint8_t *out_data, size_t out_len,
                             uint8_t *in_data, size_t in_capacity, size_t *in_len,
                             volatile bool *cancel)
{
    if (!setup) {
        return -EINVAL;
    }
    if (in_len) {
        *in_len = 0;
    }
    return sync_xfer(busid, 0, true, setup,
                     out_data, out_len, in_data, in_capacity, in_len, cancel);
}

int usbhost_bulk_transfer(const char busid[USBIP_BUSID_SIZE],
                          uint8_t ep_addr,
                          const uint8_t *out_data, size_t out_len,
                          uint8_t *in_data, size_t in_capacity, size_t *in_len,
                          volatile bool *cancel)
{
    if (ep_addr == 0) {
        return -EINVAL;
    }
    return sync_xfer(busid, ep_addr, false, NULL,
                     out_data, out_len, in_data, in_capacity, in_len, cancel);
}

int usbhost_interrupt_transfer(const char busid[USBIP_BUSID_SIZE],
                               uint8_t ep_addr,
                               const uint8_t *out_data, size_t out_len,
                               uint8_t *in_data, size_t in_capacity, size_t *in_len,
                               volatile bool *cancel)
{
    if (ep_addr == 0) {
        return -EINVAL;
    }
    return sync_xfer(busid, ep_addr, false, NULL,
                     out_data, out_len, in_data, in_capacity, in_len, cancel);
}

/* -------------------------------------------------------------------------
 * Verbose toggle
 * ------------------------------------------------------------------------- */

void usbhost_set_verbose(bool enable)
{
    s_verbose = enable;
    USBHOST_DBG("URB verbose logging %s", enable ? "enabled" : "disabled");
}

bool usbhost_is_verbose(void)
{
    return s_verbose;
}

/* -------------------------------------------------------------------------
 * Identity-change recovery: usbhost_flush / usbhost_bus_reset
 *
 * Kept from the esp32 backend (the DWC2 PRT_CONN_DET edge-trigger-miss / stuck
 * D+ pull-up case is a DWC2 property shared with rp2350). vTaskDelay ->
 * mp_hal_delay_ms; no mutex (single cooperative thread); tuh_deinit is live on
 * rp2 (mp_usbh_deinit only skips it on ESP32).
 * ------------------------------------------------------------------------- */

int usbhost_flush(bool force_bus_reset)
{
    if (!s_state.started) {
        return -ENODEV;
    }

    /* Invalidate our descriptor cache up front. */
    for (size_t i = 0; i < CFG_TUH_DEVICE_MAX; i++) {
        s_desc_cache[i].valid = false;
    }

    if (!force_bus_reset) {
        /* Cache-only path: clear our slot table directly. Does NOT touch
         * TinyUSB's internal device list. */
        for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
            clear_slot(i);
        }
        return 0;
    }

    /* Hard host restart. tuh_deinit tears down every device on the rhport
     * through the normal disconnect path and resets the DWC2 controller; the
     * re-init re-arms hot-plug detection. */
    USBHOST_DBG("flush: full host stack restart (tuh_deinit + tuh_init)");
    tuh_deinit(BOARD_TUH_RHPORT);

    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        clear_slot(i);
    }

    /* Let the DWC2 PHY settle before re-init. */
    mp_hal_delay_ms(50);

    if (!tusb_inited()) {
        mp_usbh_init_tuh();
    }

    return 0;
}

int usbhost_bus_reset(void)
{
    if (!s_state.started) {
        return -ENODEV;
    }
    if (!tuh_rhport_is_active(BOARD_TUH_RHPORT)) {
        return -ENODEV;
    }
    USBHOST_DBG("bus_reset: driving SE0 10 ms on rhport %d", BOARD_TUH_RHPORT);
    tuh_rhport_reset_bus(BOARD_TUH_RHPORT, true);
    mp_hal_delay_ms(10);
    tuh_rhport_reset_bus(BOARD_TUH_RHPORT, false);
    return 0;
}
