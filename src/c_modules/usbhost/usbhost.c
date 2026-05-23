/* Annealage Pod: USB host backend (TinyUSB).
 *
 * Implements the API in usbhost.h against TinyUSB's host primitives
 * (tuh_edpt_xfer / tuh_control_xfer / tuh_edpt_open / tuh_descriptor_*).
 * Replaces the IDF usb_host_* backend that was on main through R25.
 *
 * Lineage: this file was first landed on the parked `r24-wip` branch
 * (tip 79ee842, 11 commits) which proved single-call mpremote 30/30
 * but not multi-step fs cp. R27 (this branch r27-tinyusb-migration)
 * brings the r24-wip TinyUSB backend onto current main alongside the
 * R25/R26 work. The fs-cp deadlock work and Phase 2 throughput
 * measurement happen on top of this branch. See
 * test/integration/phase3/r27-tinyusb-resume-plan.md for the wider
 * plan and r24-wip-history.md for the seven non-obvious gotchas.
 *
 * Seven gotchas baked in (per r24-wip-history.md):
 *   1. CFG_TUH_API_EDPT_XFER=1: required for user complete_cb to fire.
 *      Set in src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.cmake.
 *   2. tuh_edpt_xfer allows only one transfer in flight per (dev,ep).
 *      Pipeline depth at the lane layer is 1 (USBIP_PIPELINE_DEPTH=1).
 *   3. tuh_xfer_t.setup and .buflen are in a UNION. submit_xfer() sets
 *      ONLY setup for control transfers; setting both corrupts setup
 *      into a pointer-cast-from-int that faults.
 *   4. tuh_edpt_abort_xfer does NOT reliably fire complete_cb for bulk
 *      EPs. usbhost_cancel_ep synthesises completion under an atomic
 *      CAS on inflight->completed.
 *   5. tuh_edpt_abort_xfer leaves the DWC2 channel half-allocated.
 *      Recovery is tuh_edpt_close + tuh_edpt_open (in usbhost_cancel_ep).
 *   6. close+open does not reset device-side data toggle. usbhost_cancel_ep
 *      issues CLEAR_FEATURE(ENDPOINT_HALT) via tuh_control_xfer to sync.
 *   7. Holding any user mutex around tuh_edpt_abort_xfer deadlocks on
 *      _usbh_mutex. The ep_submit_mutex is held across close+open
 *      but NOT across abort.
 *
 * Init:
 *   usbhost_start() calls mp_usbh_init_tuh() to boot TinyUSB host
 *   (incl. DWC2 HCD on ESP32-S3), then spawns a pump task that calls
 *   tuh_task_ext(0, false) in a loop on APP_CPU. Replaces the two-task
 *   IDF model (daemon + worker).
 *
 * Hot-plug:
 *   tuh_mount_cb / tuh_umount_cb are hard-defined (non-weak) in mp_usbh.c
 *   for the machine.USBHost Python API. We hook in via weak tuh_mount_hook
 *   / tuh_umount_hook which mp_usbh.c calls at the end of each callback.
 *   Our strong-linkage definitions here override the weak no-ops in mp_usbh.c.
 *
 * Async submit:
 *   usbhost_submit_async() allocates an inflight record + payload buffer,
 *   fills tuh_xfer_t, and calls tuh_edpt_xfer / tuh_control_xfer.
 *   xfer_complete_cb (fires in pump task context) copies IN data and invokes
 *   the caller's callback. Single-owner; no ref-count.
 *
 * Synchronous wrappers:
 *   usbhost_{control,bulk,interrupt}_transfer() use sync_xfer() which
 *   allocates inflight with a done_sem; xfer_complete_cb signals it.
 *
 * Cancel:
 *   usbhost_cancel_ep() calls tuh_edpt_abort_xfer(). Per gotcha #4
 *   TinyUSB does not synthesise a complete_cb for bulk on abort, so
 *   we synthesise via atomic CAS on inflight->completed; per gotcha
 *   #5 we close+open the EP to clear the DWC2 channel; per gotcha #6
 *   we issue CLEAR_FEATURE(ENDPOINT_HALT) to sync the device toggle.
 *
 * Pump task priority:
 *   USBHOST_PUMP_TASK_PRIORITY=20 (above responder=11, lwIP=18, below
 *   Wi-Fi=23). The R25 step 3 finding that worker=20 does not move
 *   avg_round on IDF was conclusive only for the IDF host stack;
 *   priority 20 is still the right tier for a USB host pump task
 *   relative to network / IO peers.
 *
 * IDF usb_host_* symbols may still be linked via the managed
 * component but are NOT called at runtime. Only TinyUSB (via
 * mp_usbh_init_tuh / tuh_task_ext) drives the DWC2 controller.
 */


#include "usbhost.h"

/* Workaround: FreeRTOS.h defines traceISR_EXIT_TO_SCHEDULER() as a no-op
 * only inside the #ifdef ESP_PLATFORM block (line 1488).  The micropython.elf
 * secondary build target does not pass -DESP_PLATFORM, so the macro is
 * undefined when osal_freertos.h's static inline osal_semaphore_post()
 * expands portYIELD_FROM_ISR() -> portYIELD_FROM_ISR_NO_ARG() ->
 * traceISR_EXIT_TO_SCHEDULER().  Pre-defining it here (before any FreeRTOS
 * include) ensures the no-op is in place for both build targets.
 * See slaveio.c lines 48-59 for the same pattern. */
#ifndef traceISR_EXIT_TO_SCHEDULER
#define traceISR_EXIT_TO_SCHEDULER()
#endif

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "host/usbh.h"
#include "mp_usbh.h"

/* The managed component espressif__tinyusb declares tuh_descriptor_get_*_sync
 * as non-inline external functions and wins the include path race ahead of
 * lib/tinyusb/src where they are TU_ATTR_ALWAYS_INLINE static inlines.
 * The managed component's libespressif__tinyusb.a is device-only and does not
 * provide host-side _sync implementations, so the linker fails when
 * micropython.elf picks up the managed component header.
 *
 * Work around by providing local wrappers that call the underlying async
 * tuh_descriptor_get_*() functions with NULL callback (which blocks until
 * complete).  These are available in libmain.a via mp_usbh.c compilation. */
static xfer_result_t local_get_device_desc(uint8_t dev_addr, tusb_desc_device_t *buf) {
    xfer_result_t result = XFER_RESULT_INVALID;
    if (!tuh_descriptor_get_device(dev_addr, buf, sizeof(*buf), NULL,
            (uintptr_t)&result)) {
        return XFER_RESULT_TIMEOUT;
    }
    return result;
}

static xfer_result_t local_get_config_desc(uint8_t dev_addr, void *buf, uint16_t len) {
    xfer_result_t result = XFER_RESULT_INVALID;
    /* Use the synchronous form via tuh_descriptor_get directly so we can
     * see actual_len. tuh_descriptor_get_configuration is a thin wrapper
     * around tuh_descriptor_get; the wrapper hides actual_len from us. */
    tusb_control_request_t request = {
        .bmRequestType_bit = {
            .recipient = TUSB_REQ_RCPT_DEVICE,
            .type      = TUSB_REQ_TYPE_STANDARD,
            .direction = TUSB_DIR_IN
        },
        .bRequest = TUSB_REQ_GET_DESCRIPTOR,
        .wValue   = (TUSB_DESC_CONFIGURATION << 8) | 0,
        .wIndex   = 0,
        .wLength  = len
    };
    tuh_xfer_t xfer = {
        .daddr       = dev_addr,
        .ep_addr     = 0,
        .setup       = &request,
        .buffer      = buf,
        .complete_cb = NULL,
        .user_data   = (uintptr_t)&result,
    };
    if (!tuh_control_xfer(&xfer)) {
        return XFER_RESULT_TIMEOUT;
    }
    ESP_LOGI("usbhost", "local_get_config_desc: requested=%u actual_len=%u result=%d",
             (unsigned)len, (unsigned)xfer.actual_len, (int)result);
    return result;
}

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
#ifndef USBHOST_TASK_CORE
#define USBHOST_TASK_CORE 1
#endif
#ifndef USBHOST_PUMP_TASK_PRIORITY
#define USBHOST_PUMP_TASK_PRIORITY 20
#endif
#ifndef USBHOST_PUMP_TASK_STACK
#define USBHOST_PUMP_TASK_STACK 8192
#endif
#ifndef USBHOST_PUMP_IDLE_TICKS
#define USBHOST_PUMP_IDLE_TICKS 1
#endif

/* R27 URB watchdog: bound the time between submit and completion so a
 * stuck URB cannot leave the kernel-side cdc-acm in usb_poison_urb
 * D-state forever. If TinyUSB's natural callback fires within
 * USBHOST_WATCHDOG_TIMEOUT_US, the watchdog is silent. If not, the
 * watchdog walks current_inflight[] every USBHOST_WATCHDOG_TICK_MS
 * and runs the gotcha #5/#6 recovery (close+open+CLEAR_FEATURE) plus
 * a synthesised -ETIMEDOUT completion under the same atomic CAS that
 * gates usbhost_cancel_ep. This is permanent production code, not
 * gated by R27_DEADLOCK_TRACE; the cost on the happy path is one
 * task wake every 100 ms walking USBHOST_MAX_DEVICES * 32 = 128
 * pointer slots. */
#ifndef USBHOST_WATCHDOG_TIMEOUT_US
#define USBHOST_WATCHDOG_TIMEOUT_US 2000000   /* 2 seconds */
#endif
#ifndef USBHOST_WATCHDOG_TICK_MS
#define USBHOST_WATCHDOG_TICK_MS 100
#endif
#ifndef USBHOST_WATCHDOG_TASK_PRIORITY
#define USBHOST_WATCHDOG_TASK_PRIORITY 5
#endif
#ifndef USBHOST_WATCHDOG_TASK_STACK
#define USBHOST_WATCHDOG_TASK_STACK 4096
#endif

#ifndef TUSB_CLASS_HUB
#define TUSB_CLASS_HUB 0x09
#endif

/* R27 DEADLOCK_TRACE: per-URB submit / complete log lines for the
 * fs-cp deadlock investigation. NOT enabled by default; the dispatch
 * sequence here is the watchdog (always on, prevents D-state) plus
 * the trace (debug-only, enabled via -DR27_DEADLOCK_TRACE in a
 * per-build override).
 *
 * To enable for a debug build add to the top of mpconfigboard.cmake
 * (above the MICROPY_DEF_BOARD lines):
 *
 *     list(APPEND MICROPY_DEF_BOARD R27_DEADLOCK_TRACE=1)
 *
 * or pass -DR27_DEADLOCK_TRACE=1 via the cmake invocation.
 *
 * When enabled the trace prints one ESP_LOGI line per submit and one
 * per natural / synthesised / watchdog completion, all keyed by a
 * monotonic per-host trace_seq so the chain can be reassembled in
 * the captured UART log. */

static const char *TAG = "usbhost";

/* Forward declarations for the task entry points (definitions later
 * in this file). usbhost_start uses xTaskCreatePinnedToCore on these. */
static void usbhost_pump_task(void *arg);
static void usbhost_watchdog_task(void *arg);

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
    /* Per-EP submit mutexes; index = ep_mutex_index(). Lazy-created. */
    SemaphoreHandle_t  ep_submit_mutex[32];
    /* Currently-submitted async inflight per (ep,dir). Set by submit_xfer
     * before calling tuh_*_xfer; cleared by xfer_complete_cb or by
     * usbhost_cancel_ep. The race between TinyUSB's natural completion
     * and our cancel-synthesised completion is arbitrated via the
     * inflight->completed atomic. Indexed by ep_mutex_index(). */
    struct usbhost_inflight *current_inflight[32];
} usbhost_slot_t;

typedef struct {
    bool               started;
    SemaphoreHandle_t  state_mutex;
    TaskHandle_t       pump_hdl;
    TaskHandle_t       watchdog_hdl;        /* R27: URB-completion watchdog */
    usbhost_slot_t     devices[USBHOST_MAX_DEVICES];
} usbhost_state_t;

static usbhost_state_t s_state;
static volatile bool   s_urb_verbose = false;

void usbhost_set_verbose(bool enable)
{
    s_urb_verbose = enable;
    ESP_LOGI(TAG, "URB verbose logging %s", enable ? "enabled" : "disabled");
}

bool usbhost_is_verbose(void)
{
    return s_urb_verbose;
}

/* -------------------------------------------------------------------------
 * Helpers
 * ------------------------------------------------------------------------- */

static bool busid_eq(const char a[USBIP_BUSID_SIZE], const char b[USBIP_BUSID_SIZE])
{
    return memcmp(a, b, USBIP_BUSID_SIZE) == 0;
}

/* EP address -> 5-bit mutex index (direction in bit 4, number in bits[3:0]). */
static uint8_t ep_mutex_index(uint8_t ep_addr)
{
    return (uint8_t)((ep_addr & 0x0F) | ((ep_addr & 0x80) >> 3));
}

/* Caller holds state_mutex. */
static SemaphoreHandle_t get_ep_submit_mutex_locked(int slot, uint8_t ep_addr)
{
    if (slot < 0 || slot >= USBHOST_MAX_DEVICES || !s_state.devices[slot].in_use) {
        return NULL;
    }
    uint8_t idx = ep_mutex_index(ep_addr);
    SemaphoreHandle_t m = s_state.devices[slot].ep_submit_mutex[idx];
    if (m == NULL) {
        m = xSemaphoreCreateMutex();
        s_state.devices[slot].ep_submit_mutex[idx] = m;
    }
    return m;
}

static int find_slot_by_busid_locked(const char busid[USBIP_BUSID_SIZE])
{
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        if (s_state.devices[i].in_use &&
            busid_eq(busid, s_state.devices[i].device.busid)) {
            return i;
        }
    }
    return -1;
}

static int find_slot_by_devaddr_locked(uint8_t dev_addr)
{
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        if (s_state.devices[i].in_use &&
            s_state.devices[i].dev_addr == dev_addr) {
            return i;
        }
    }
    return -1;
}

static int find_free_slot_locked(void)
{
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        if (!s_state.devices[i].in_use) {
            return i;
        }
    }
    return -1;
}

static void clear_slot_locked(int slot)
{
    if (slot < 0 || slot >= USBHOST_MAX_DEVICES) {
        return;
    }
    for (size_t i = 0; i < sizeof(s_state.devices[slot].ep_submit_mutex) /
                          sizeof(s_state.devices[slot].ep_submit_mutex[0]); i++) {
        if (s_state.devices[slot].ep_submit_mutex[i] != NULL) {
            vSemaphoreDelete(s_state.devices[slot].ep_submit_mutex[i]);
            s_state.devices[slot].ep_submit_mutex[i] = NULL;
        }
    }
    memset(&s_state.devices[slot], 0, sizeof(s_state.devices[slot]));
}

/* Return wMaxPacketSize for the given EP from the cached endpoint
 * descriptor, or 0 if the EP isn't found / wasn't populated.
 *
 * The USB endpoint descriptor's wMaxPacketSize field is parsed at
 * enumeration time in parse_config_desc (bytes 4-5, little-endian)
 * and is the authoritative MPS for both FS (typically 64) and HS
 * (typically 512) bulk endpoints. For bulk EPs the upper bits
 * [12:11] of wMaxPacketSize are reserved (0) per USB 2.0 §9.6.6,
 * so reading the full uint16 is correct without masking.
 *
 * NO FALLBACK to a hardcoded value: if the lookup fails, callers
 * must surface and stop. Returning 64 here would silently
 * mis-chunk on HS bulk endpoints (where MPS=512), defeating the
 * purpose of looking up the descriptor in the first place. */
static uint16_t get_endpoint_mps_locked(int slot, uint8_t ep_addr)
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

/* Returns the TUSB_XFER_* code (CTRL=0 ISO=1 BULK=2 INTR=3) for the
 * given EP, or 0xff if not found. Caller holds state_mutex. The R27
 * watchdog uses this to skip URBs on EPs that legitimately wait
 * indefinitely (interrupt-IN, isochronous). */
static uint8_t get_endpoint_xfer_type_locked(int slot, uint8_t ep_addr)
{
    if (slot < 0 || slot >= USBHOST_MAX_DEVICES ||
        !s_state.devices[slot].in_use) {
        return 0xff;
    }
    for (uint8_t i = 0; i < s_state.devices[slot].num_endpoints; i++) {
        if (s_state.devices[slot].endpoints[i].address == ep_addr) {
            return s_state.devices[slot].endpoints[i].attributes & 0x03;
        }
    }
    return 0xff;
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
#ifdef R27_DEADLOCK_TRACE
    ESP_LOGI(TAG, "parse_config: total=%u intf=%u eps=%u",
             (unsigned)total, (unsigned)intf_count, (unsigned)ep_count);
    char hex[80];
    size_t dump = total < 24 ? total : 24;
    for (size_t i = 0; i < dump; i++) {
        snprintf(hex + 3 * i, sizeof(hex) - 3 * i, "%02x ", raw[i]);
    }
    ESP_LOGI(TAG, "  first %u bytes: %s", (unsigned)dump, hex);
    for (uint8_t i = 0; i < ep_count; i++) {
        ESP_LOGI(TAG, "  ep[%u] addr=0x%02x attr=0x%02x mps=%u interval=%u",
                 i, eps_out[i].address, eps_out[i].attributes,
                 (unsigned)eps_out[i].max_packet_size, eps_out[i].interval);
    }
#endif
}

/* -------------------------------------------------------------------------
 * Inflight record
 * ------------------------------------------------------------------------- */

typedef struct usbhost_inflight {
    /* Sync path: binary semaphore + results written by xfer_complete_cb. */
    SemaphoreHandle_t  done_sem;
    int                sync_status;
    size_t             sync_in_len;
    /* Async path callback. */
    void             (*user_cb)(void *ctx, int status, size_t in_len);
    void              *user_ctx;
    /* IN data destination (both paths: points at caller's buffer). */
    uint8_t           *in_data;
    size_t             in_capacity;
    /* Transfer shape. */
    bool               is_in;
    bool               is_control;
    size_t             payload_len;
    /* Heap payload buffer (stable for TinyUSB lifetime). */
    uint8_t           *buf;
    size_t             buf_len;
    /* Atomic completion claim. 0 = pending, 1 = completed (either by
     * TinyUSB callback or by cancel synthesis). __atomic_compare_exchange
     * resolves the race; whoever wins runs the user_cb / done_sem give.
     * The other path no-ops (does not free either; the winner frees). */
    volatile uint32_t  completed;
    /* Slot/index for the slot's current_inflight[] back-reference. Set
     * at submit time; used to clear current_inflight on completion. */
    int                slot_idx;
    uint8_t            ep_idx;
    /* R27 watchdog state. t_submit_us is the monotonic timestamp of
     * tuh_*_xfer return; the watchdog compares this against
     * esp_timer_get_time() each tick. dev_addr and ep_addr_full are
     * kept here so the watchdog can run the same recovery as
     * usbhost_cancel_ep (close+open+CLEAR_FEATURE) without rewalking
     * the slot table. watchdog_armed prevents re-firing across ticks
     * while the synchronous recovery (tuh_control_xfer for
     * CLEAR_FEATURE) is in progress. */
    int64_t            t_submit_us;
    uint8_t            dev_addr;
    uint8_t            ep_addr_full;       /* full ep_addr including dir bit */
    uint8_t            ep_xfer_type;       /* TUSB_XFER_* (CTRL=0, ISO=1,
                                            * BULK=2, INTR=3) - cached at
                                            * submit so the watchdog can
                                            * skip endpoints that
                                            * legitimately wait
                                            * indefinitely (interrupt-IN). */
    volatile uint32_t  watchdog_armed;     /* CAS 0->1 to claim recovery */
#ifdef R27_DEADLOCK_TRACE
    /* Local monotonic trace counter. NOT the usbip wire seqnum; this is
     * a per-host local id for correlating sub/cb log lines under a
     * deadlock-debug session. */
    uint32_t           trace_seq;
#endif
} usbhost_inflight_t;

/* Allocate a DMA-capable URB buffer in internal SRAM.
 *
 * Required for CFG_TUH_DWC2_DMA_ENABLE=1 on ESP32-S2/S3 (and P4 in
 * the non-PSRAM-DMA configuration). The DWC2 internal DMA engine
 * accesses internal SRAM directly via the AHB bus and cannot reach
 * PSRAM (on S2/S3) without cache flushes that we do not perform.
 *
 * MALLOC_CAP_DMA: ensure the allocation can be DMA'd to/from.
 * MALLOC_CAP_INTERNAL: stay in internal SRAM, not PSRAM. Default
 *   malloc() can return PSRAM for allocations >= 8 KB on this board
 *   (CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL=8192) which would silently
 *   break DMA transfers; force INTERNAL to be safe.
 *
 * Alignment falls out of heap_caps_malloc; for DWC2 internal DMA on
 * ESP32-S3 4-byte alignment is sufficient (no L1 cache line concern).
 *
 * Returns NULL on OOM; caller must handle.
 */
static void *usbhost_buf_alloc(size_t len)
{
    return heap_caps_malloc(len, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
}

static void inflight_free(usbhost_inflight_t *inflight)
{
    if (!inflight) {
        return;
    }
    if (inflight->done_sem) {
        vSemaphoreDelete(inflight->done_sem);
    }
    /* heap_caps_malloc-ed buffers can be freed with free(); IDF maps
     * free() through the multi-heap allocator that also handles
     * heap_caps_malloc. */
    free(inflight->buf);
    free(inflight);
}

/* Complete callback; runs in pump task context (priority USBHOST_PUMP_TASK_PRIORITY). */
static void xfer_complete_cb(tuh_xfer_t *xfer)
{
    usbhost_inflight_t *inflight = (usbhost_inflight_t *)(uintptr_t)xfer->user_data;
    if (!inflight) {
        return;
    }

#ifdef R27_DEADLOCK_TRACE
    ESP_LOGI(TAG, "cb:  seq=%" PRIu32 " ep=0x%02x result=%d alen=%u ifl=%p",
             inflight->trace_seq, xfer->ep_addr,
             (int)xfer->result, (unsigned)xfer->actual_len, inflight);
    /* R27 DMA short-IN corruption triage: hex-dump short bulk-IN
     * URB buffers right at the cb. We see a paste-mode-entry
     * response where Pico sends b"R\x01" (2 bytes) but our reader
     * gets b"\x80\x00\x01" (3 bytes). The dump shows what landed in
     * inflight->buf vs what the wire sent, plus adjacent bytes to
     * test the heap-reuse / channel-state / round-up hypotheses
     * documented in r27-dma-fix-findings.md. Only fires for
     * bulk-IN with actual_len <= 16 to keep the noise bounded. */
    if ((xfer->ep_addr & 0x80) && (xfer->ep_addr & 0x0f) != 0 &&
        xfer->actual_len > 0 && xfer->actual_len <= 64 &&
        inflight->buf && inflight->buf_len > 0) {
        size_t valid = xfer->actual_len;
        if (valid > inflight->buf_len) {
            valid = inflight->buf_len;
        }
        char vhex[64 * 3 + 1];
        for (size_t i = 0; i < valid && i < 64; i++) {
            snprintf(vhex + 3 * i, sizeof(vhex) - 3 * i, "%02x ",
                     inflight->buf[i]);
        }
        ESP_LOGI(TAG, "  short_in seq=%" PRIu32 " buf=%p buf_len=%u "
                      "valid(%u): %s",
                 inflight->trace_seq, inflight->buf,
                 (unsigned)inflight->buf_len, (unsigned)valid, vhex);
    }
#endif

    /* R24 diagnostic: track natural-completion win rate vs cancel-synth.
     * Always-on counters (cheap), summary log every 50 wins. */
    static uint32_t s_natural_wins = 0;
    static uint32_t s_natural_lost = 0;

    /* Atomic claim: the cancel path may also try to fire completion if
     * TinyUSB's tuh_edpt_abort_xfer disabled the channel without firing
     * a natural completion. Whoever does CAS 0->1 first runs the user
     * completion; the other returns without firing or freeing. */
    uint32_t expected = 0;
    if (!__atomic_compare_exchange_n(&inflight->completed, &expected, 1,
                                     false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        s_natural_lost++;
        if (s_urb_verbose) {
            ESP_LOGI(TAG, "natural_lost: ep=0x%02x ifl=%p (cancel_won)",
                     xfer->ep_addr, inflight);
        }
        return; /* cancel path won */
    }
    s_natural_wins++;
    if (s_urb_verbose) {
        ESP_LOGI(TAG, "natural_won: ep=0x%02x ifl=%p result=%d alen=%u",
                 xfer->ep_addr, inflight, (int)xfer->result,
                 (unsigned)xfer->actual_len);
    }
    if ((s_natural_wins + s_natural_lost) % 5 == 0) {
        ESP_LOGI(TAG, "completion_summary: natural_wins=%" PRIu32
                      " natural_lost=%" PRIu32,
                 s_natural_wins, s_natural_lost);
    }

    /* Clear slot's current_inflight back-pointer. */
    if (inflight->slot_idx >= 0 && inflight->slot_idx < USBHOST_MAX_DEVICES &&
        inflight->ep_idx < 32) {
        xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
        if (s_state.devices[inflight->slot_idx].current_inflight[inflight->ep_idx]
            == inflight) {
            s_state.devices[inflight->slot_idx].current_inflight[inflight->ep_idx] = NULL;
        }
        xSemaphoreGive(s_state.state_mutex);
    }

    int status = xfer_result_to_errno(xfer->result);
    size_t in_len = 0;

    if (status == 0 && inflight->is_in && inflight->payload_len > 0) {
        /* TinyUSB reports actual_len as the data-stage byte count for
         * both control and bulk/interrupt transfers (setup is not
         * counted). Source buffer for control is inflight->buf + 8
         * because we kept the [setup(8) | data] layout. */
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

    if (inflight->done_sem) {
        /* Sync path: store results, signal caller.  Caller frees inflight. */
        inflight->sync_status = status;
        inflight->sync_in_len = in_len;
        xSemaphoreGive(inflight->done_sem);
        return;
    }

    /* Async path: free then callback. */
    void (*cb)(void *, int, size_t) = inflight->user_cb;
    void *ctx = inflight->user_ctx;
    inflight_free(inflight);
    if (cb) {
        cb(ctx, status, in_len);
    }
}

/* -------------------------------------------------------------------------
 * Core submit helper (used by both sync and async paths)
 *
 * Allocates inflight + payload buffer, fills tuh_xfer_t, submits.
 * For async: done_sem = NULL, user_cb / user_ctx set.
 * For sync:  done_sem = pre-allocated semaphore, user_cb = NULL.
 *
 * Returns 0 on success, negative errno on immediate failure.
 * On failure, done_sem ownership is NOT consumed (caller must delete it).
 * ------------------------------------------------------------------------- */

static int submit_xfer(const char busid[USBIP_BUSID_SIZE],
                       uint8_t ep_addr, bool is_control,
                       const usbip_setup_packet_t *setup,
                       const uint8_t *out_data, size_t out_len,
                       uint8_t *in_data, size_t in_capacity,
                       void (*user_cb)(void *ctx, int status, size_t in_len),
                       void *user_ctx,
                       SemaphoreHandle_t done_sem)
{
    const bool is_in = is_control
        ? (setup != NULL && (setup->bmRequestType & 0x80) != 0)
        : (ep_addr & 0x80) != 0;
    const size_t payload_len = is_in ? in_capacity : out_len;

    if (payload_len > USBHOST_MAX_TRANSFER) {
        return -EMSGSIZE;
    }

    uint8_t dev_addr = 0;
    size_t xfer_payload = payload_len;
    SemaphoreHandle_t ep_mutex = NULL;
    const uint8_t submit_ep = is_control ? 0x00 : ep_addr;

    /* MPS lookup for IN URB length-rounding. The kernel-side cdc-acm
     * passes in_capacity = NR_BUFFERS * MPS (typically 16 * 64 = 1024
     * for FS, or 16 * 512 = 8192 for HS) which is already a multiple
     * of MPS, so the rounding is a no-op in the common case. The
     * rounding handles odd in_capacity values (e.g. control xfers
     * with non-MPS-aligned wLength) by extending to the next MPS
     * boundary.
     *
     * MPS comes from the parsed endpoint descriptor; no hardcoded
     * fallback. If MPS is 0 (descriptor not cached), submit fails
     * with -ENODEV rather than silently mis-round. Symmetric with
     * the OUT chunking path below. */
    bool mps_unavailable = false;
    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_busid_locked(busid);
    if (slot >= 0) {
        dev_addr = s_state.devices[slot].dev_addr;
        if (!is_control && is_in && in_capacity > 0) {
            uint16_t mps = get_endpoint_mps_locked(slot, ep_addr);
            if (mps == 0) {
                mps_unavailable = true;
            } else if ((xfer_payload % mps) != 0) {
                xfer_payload = ((xfer_payload + mps - 1) / mps) * mps;
            }
        }
        ep_mutex = get_ep_submit_mutex_locked(slot, submit_ep);
    }
    xSemaphoreGive(s_state.state_mutex);

    if (dev_addr == 0) {
        return -ENODEV;
    }
    if (mps_unavailable) {
        ESP_LOGE(TAG, "submit_xfer: IN ep=0x%02x has no cached MPS, "
                      "refusing submit (slot=%d, dev=%u)",
                 ep_addr, slot, dev_addr);
        return -ENODEV;
    }

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "submit: dev=%.32s ep=0x%02x dir=%s len=%u",
                 busid, ep_addr,
                 is_control ? "CTRL" : (is_in ? "IN" : "OUT"),
                 (unsigned)payload_len);
    }

    usbhost_inflight_t *inflight = calloc(1, sizeof(*inflight));
    if (!inflight) {
        return -ENOMEM;
    }
    inflight->done_sem    = done_sem;
    inflight->user_cb     = user_cb;
    inflight->user_ctx    = user_ctx;
    inflight->is_in       = is_in;
    inflight->is_control  = is_control;
    inflight->in_data     = in_data;
    inflight->in_capacity = in_capacity;
    inflight->payload_len = payload_len;
    inflight->completed     = 0;
    inflight->slot_idx      = slot;
    inflight->ep_idx        = ep_mutex_index(submit_ep);
    /* R27 watchdog: capture device + ep + xfer-type at submit time so
     * the watchdog task can run recovery without re-resolving busid ->
     * dev_addr, and skip endpoints that legitimately wait indefinitely
     * (interrupt-IN for status notifications, isochronous). */
    inflight->dev_addr      = dev_addr;
    inflight->ep_addr_full  = is_control ? 0 : ep_addr;
    inflight->t_submit_us   = 0;
    inflight->watchdog_armed = 0;
    if (is_control) {
        inflight->ep_xfer_type = 0;  /* TUSB_XFER_CTRL */
    } else {
        xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
        inflight->ep_xfer_type = get_endpoint_xfer_type_locked(slot, ep_addr);
        xSemaphoreGive(s_state.state_mutex);
    }
#ifdef R27_DEADLOCK_TRACE
    {
        static uint32_t s_trace_seq_next = 0;
        inflight->trace_seq = __atomic_add_fetch(&s_trace_seq_next, 1,
                                                 __ATOMIC_RELAXED);
        ESP_LOGI(TAG, "sub: seq=%" PRIu32 " ep=0x%02x dir=%s len=%u "
                      "ifl=%p dev=%u",
                 inflight->trace_seq, ep_addr,
                 is_control ? "CTRL" : (is_in ? "IN" : "OUT"),
                 (unsigned)payload_len, inflight, (unsigned)dev_addr);
    }
#endif

    size_t buf_len = is_control ? (8 + xfer_payload) : xfer_payload;
    if (buf_len == 0) {
        buf_len = 1;
    }
    inflight->buf = usbhost_buf_alloc(buf_len);
    inflight->buf_len = buf_len;
    if (!inflight->buf) {
        /* Nullify done_sem so inflight_free doesn't delete the caller's sem. */
        inflight->done_sem = NULL;
        inflight_free(inflight);
        return -ENOMEM;
    }
    memset(inflight->buf, 0, buf_len);

    if (is_control && setup) {
        memcpy(inflight->buf, setup, 8);
        if (!is_in && out_len > 0 && out_data) {
            size_t copy = (out_len < xfer_payload) ? out_len : xfer_payload;
            memcpy(inflight->buf + 8, out_data, copy);
        }
    } else if (!is_in && out_len > 0 && out_data) {
        size_t copy = (out_len < xfer_payload) ? out_len : xfer_payload;
        memcpy(inflight->buf, out_data, copy);
    }

    /* R27 DMA-mode: bulk-OUT URBs are submitted as a single tuh_edpt_xfer
     * with the full payload. The DWC2 internal-DMA path handles
     * multi-packet bulk-OUT in hardware, so the slave-mode FIFO/NAK
     * race that motivated the Phase 1 chunking workaround does not
     * apply here. Phase 1's chunking commit (34583f1) was reverted in
     * 2026-05-06 once DMA mode became viable on ESP32-S3; see
     * test/integration/phase3/r27-dma-fix-findings.md. */

    tuh_xfer_t xfer;
    memset(&xfer, 0, sizeof(xfer));
    xfer.daddr       = dev_addr;
    xfer.ep_addr     = is_control ? 0 : ep_addr;
    xfer.complete_cb = xfer_complete_cb;
    xfer.user_data   = (uintptr_t)inflight;
    if (is_control) {
        /* tuh_xfer_t.setup and .buflen share a UNION (usbh.h:64-67).
         * For control transfers set ONLY setup; the data-stage size
         * is taken from the setup packet's wLength field, not buflen.
         * Setting buflen here would overwrite setup with the integer
         * cast as a pointer (tuh_control_xfer:744 then dereferences
         * (uint32_t)xfer_payload as a pointer -> NULL+xfer_payload
         * fault). buffer points at the data stage; our inflight->buf
         * layout is [setup(8) | data], so buffer = inflight->buf+8. */
        xfer.setup  = (const tusb_control_request_t *)inflight->buf;
        xfer.buffer = inflight->buf + 8;
    } else {
        xfer.buffer = inflight->buf;
        xfer.buflen = (uint32_t)xfer_payload;
    }

    /* Publish the inflight as the current in-flight on this (slot, ep)
     * BEFORE submitting. usbhost_cancel_ep reads this slot to decide
     * whether to synthesise a completion when TinyUSB's abort path
     * doesn't fire its own callback. The race against xfer_complete_cb
     * (which clears the slot) is benign because we set the slot under
     * state_mutex and the completion clears it under the same mutex. */
    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    if (slot >= 0 && inflight->ep_idx < 32) {
        s_state.devices[slot].current_inflight[inflight->ep_idx] = inflight;
    }
    xSemaphoreGive(s_state.state_mutex);

    if (ep_mutex) {
        xSemaphoreTake(ep_mutex, portMAX_DELAY);
    }
    /* R27 watchdog: stamp t_submit_us BEFORE tuh_*_xfer call so a
     * wedge inside tuh_*_xfer itself is also bounded. */
    inflight->t_submit_us = esp_timer_get_time();
    bool ok = is_control ? tuh_control_xfer(&xfer) : tuh_edpt_xfer(&xfer);
    if (ep_mutex) {
        xSemaphoreGive(ep_mutex);
    }

    if (!ok) {
        ESP_LOGW(TAG, "tuh_%s_xfer rejected ep=0x%02x addr=%u",
                 is_control ? "control" : "edpt", ep_addr, dev_addr);
        /* Submit failed: clear the current_inflight slot we just published. */
        xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
        if (slot >= 0 && inflight->ep_idx < 32 &&
            s_state.devices[slot].current_inflight[inflight->ep_idx] == inflight) {
            s_state.devices[slot].current_inflight[inflight->ep_idx] = NULL;
        }
        xSemaphoreGive(s_state.state_mutex);
        /* Atomic claim before free: a concurrent usbhost_cancel_ep on the
         * same EP may have already synthesised a completion and freed
         * the inflight. CAS resolves the race; if we lose, the cancel
         * path owns the free. */
        uint32_t expected = 0;
        if (!__atomic_compare_exchange_n(&inflight->completed, &expected, 1,
                                         false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
            /* Cancel won. The cancel path called user_cb with -ECONNRESET
             * and freed the inflight. We just return the error to caller;
             * no further cleanup. */
            return -EIO;
        }
        inflight->done_sem = NULL; /* caller still owns done_sem */
        inflight_free(inflight);
        return -EIO;
    }
    return 0;
}

/* -------------------------------------------------------------------------
 * Hot-plug: tuh_mount_hook / tuh_umount_hook
 *
 * mp_usbh.c defines weak tuh_mount_hook / tuh_umount_hook no-ops and calls
 * them from tuh_mount_cb / tuh_umount_cb. We provide strong-linkage
 * overrides here to run our enumeration bookkeeping alongside the Python
 * machine.USBHost API bookkeeping in mp_usbh.c.
 * ------------------------------------------------------------------------- */

static void enumerate_device(uint8_t dev_addr)
{
    tusb_desc_device_t dev_desc;
    memset(&dev_desc, 0, sizeof(dev_desc));
    xfer_result_t res = local_get_device_desc(dev_addr, &dev_desc);
    if (res != XFER_RESULT_SUCCESS) {
        ESP_LOGW(TAG, "get_device_desc failed addr=%u res=%d", dev_addr, (int)res);
        return;
    }

    if (dev_desc.bDeviceClass == TUSB_CLASS_HUB) {
        ESP_LOGI(TAG, "hub at addr=%u, skipping", dev_addr);
        return;
    }

    /* R27 DMA-mode requirement: DWC2 internal-DMA writes to memory in
     * 4-byte words and rounds the destination address DOWN to a 4-byte
     * boundary. If cfg_buf were not 4-byte aligned, the HW would write
     * IN data starting (start & ~3), shifting the data toward lower
     * addresses by `(start & 3)` bytes and clobbering the caller's
     * preceding stack frame. Force 4-byte alignment so wire byte 0
     * lands at cfg_buf[0]. Empirically observed on ESP32-S3 with the
     * Pico CDC: when cfg_buf landed at 0x...b6, the 75-byte config
     * descriptor was written with a 2-byte left shift (cfg_buf[0]
     * held wire byte 2; bLength + bDescriptorType were lost into
     * cfg_buf - 2). */
    uint8_t cfg_buf[512] __attribute__((aligned(4)));
    memset(cfg_buf, 0, sizeof(cfg_buf));
    /* R27 DMA enumeration debug: print cfg_buf address so we can tell
     * whether stack-backed scratch lands in internal SRAM (DMA reaches)
     * or PSRAM (DMA does not on S2/S3, must be flushed). Internal SRAM
     * range on ESP32-S3 is approximately 0x3FC8_8000 - 0x3FCF_FFFF;
     * PSRAM is 0x3C00_0000 - 0x3DFF_FFFF. */
    ESP_LOGI(TAG, "enumerate_device: cfg_buf=%p (stack)", (void*)cfg_buf);
    res = local_get_config_desc(dev_addr, cfg_buf, sizeof(cfg_buf));
    {
        /* Dump the full config descriptor in 32-byte chunks. */
        for (size_t base = 0; base < 256; base += 32) {
            char hex[3 * 32 + 1];
            for (size_t i = 0; i < 32; i++) {
                snprintf(hex + 3 * i, sizeof(hex) - 3 * i, "%02x ", cfg_buf[base + i]);
            }
            ESP_LOGI(TAG, "  cfg_buf[%3u..%3u]= %s",
                     (unsigned)base, (unsigned)(base + 31), hex);
        }
    }
    if (res != XFER_RESULT_SUCCESS) {
        ESP_LOGW(TAG, "get_config_desc failed addr=%u res=%d", dev_addr, (int)res);
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

    if (res == XFER_RESULT_SUCCESS) {
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
            ESP_LOGI(TAG, "hub interface at addr=%u, skipping", dev_addr);
            return;
        }
    }

    snprintf(desc.busid, sizeof(desc.busid), "1-%u", dev_addr);
    snprintf(desc.path,  sizeof(desc.path),  "/esp-usb-host/1-%u", dev_addr);

    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int existing = find_slot_by_devaddr_locked(dev_addr);
    if (existing >= 0) {
        clear_slot_locked(existing);
    }
    int slot = find_free_slot_locked();
    if (slot < 0) {
        xSemaphoreGive(s_state.state_mutex);
        ESP_LOGW(TAG, "no free device slot (max=%d)", USBHOST_MAX_DEVICES);
        return;
    }
    s_state.devices[slot].in_use        = true;
    s_state.devices[slot].dev_addr      = dev_addr;
    s_state.devices[slot].device        = desc;
    s_state.devices[slot].num_endpoints = num_eps;
    memcpy(s_state.devices[slot].endpoints, eps, sizeof(eps[0]) * num_eps);
    xSemaphoreGive(s_state.state_mutex);

    /* Open non-zero endpoints (outside state_mutex to avoid deadlock). */
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

        xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
        int s2 = find_slot_by_devaddr_locked(dev_addr);
        if (s2 >= 0 && i < s_state.devices[s2].num_endpoints) {
            s_state.devices[s2].endpoints[i].opened = ok;
        }
        xSemaphoreGive(s_state.state_mutex);

        if (!ok) {
            ESP_LOGW(TAG, "tuh_edpt_open ep=0x%02x failed addr=%u",
                     eps[i].address, dev_addr);
        }
    }

    ESP_LOGI(TAG, "exported busid=%s vid=%04x pid=%04x num_intf=%u num_ep=%u",
             desc.busid, desc.id_vendor, desc.id_product,
             desc.num_interfaces, num_eps);
}

/* Strong-linkage overrides of the weak no-ops in mp_usbh.c. */
void tuh_mount_hook(uint8_t dev_addr)
{
    enumerate_device(dev_addr);
}

void tuh_umount_hook(uint8_t dev_addr)
{
    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_devaddr_locked(dev_addr);
    char busid[USBIP_BUSID_SIZE] = {0};
    if (slot >= 0) {
        memcpy(busid, s_state.devices[slot].device.busid, sizeof(busid));
        clear_slot_locked(slot);
    }
    xSemaphoreGive(s_state.state_mutex);
    if (slot >= 0) {
        ESP_LOGI(TAG, "device disconnected: %.32s", busid);
    }
}

/* -------------------------------------------------------------------------
 * Pump task
 * ------------------------------------------------------------------------- */

static void usbhost_pump_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "TinyUSB host pump task running on core %d",
             (int)USBHOST_TASK_CORE);
    while (true) {
        tuh_task_ext(0, false);
        if (!tuh_task_event_ready()) {
            vTaskDelay(USBHOST_PUMP_IDLE_TICKS);
        }
    }
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
    s_state.state_mutex = xSemaphoreCreateMutex();
    if (!s_state.state_mutex) {
        return -ENOMEM;
    }

    mp_usbh_init_tuh();
    ESP_LOGI(TAG, "TinyUSB host stack initialised (R24)");

    BaseType_t rc = xTaskCreatePinnedToCore(
        usbhost_pump_task, "usbh_pump",
        USBHOST_PUMP_TASK_STACK, NULL,
        USBHOST_PUMP_TASK_PRIORITY, &s_state.pump_hdl,
        USBHOST_TASK_CORE);
    if (rc != pdPASS) {
        return -ENOMEM;
    }

    /* R27: spawn the URB-completion watchdog. Permanent production
     * code, not gated by R27_DEADLOCK_TRACE. Bounds the worst-case
     * URB-to-completion latency so a stuck URB cannot put kernel-side
     * cdc-acm into usb_poison_urb D-state. See usbhost_watchdog_task
     * for the full mechanism. */
    rc = xTaskCreatePinnedToCore(
        usbhost_watchdog_task, "usbh_wdog",
        USBHOST_WATCHDOG_TASK_STACK, NULL,
        USBHOST_WATCHDOG_TASK_PRIORITY, &s_state.watchdog_hdl,
        USBHOST_TASK_CORE);
    if (rc != pdPASS) {
        ESP_LOGE(TAG, "watchdog task spawn failed (rc=%d); URB-completion "
                      "safety net DISABLED", (int)rc);
        /* Continue: the system is still functional, just without the
         * D-state safety net. Better than refusing to start the host
         * stack entirely. */
    }

    s_state.started = true;
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
    size_t copied = 0;
    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    for (int i = 0; i < USBHOST_MAX_DEVICES && copied < max; i++) {
        if (s_state.devices[i].in_use) {
            out[copied++] = s_state.devices[i].device;
        }
    }
    xSemaphoreGive(s_state.state_mutex);
    return copied;
}

bool usbhost_get_device_by_busid(const char busid[USBIP_BUSID_SIZE],
                                 usbip_dev_record_t *out)
{
    if (!busid || !out) {
        return false;
    }
    bool found = false;
    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_busid_locked(busid);
    if (slot >= 0) {
        *out  = s_state.devices[slot].device;
        found = true;
    }
    xSemaphoreGive(s_state.state_mutex);
    return found;
}

bool usbhost_is_interrupt_endpoint(const char busid[USBIP_BUSID_SIZE],
                                   uint8_t ep_num, uint8_t direction)
{
    if (!busid) {
        return false;
    }
    const uint8_t ep_addr = (uint8_t)((ep_num & 0x7F) | (direction ? 0x80 : 0x00));
    bool is_intr = false;

    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_busid_locked(busid);
    if (slot >= 0) {
        for (uint8_t i = 0; i < s_state.devices[slot].num_endpoints; i++) {
            if (s_state.devices[slot].endpoints[i].address == ep_addr) {
                is_intr = (s_state.devices[slot].endpoints[i].attributes & 0x03) == 0x03;
                break;
            }
        }
    }
    xSemaphoreGive(s_state.state_mutex);
    return is_intr;
}

/* -------------------------------------------------------------------------
 * Public API: usbhost_submit_async
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
                       out_data, out_len, in_data, in_capacity,
                       cb, ctx, NULL);
}

/* -------------------------------------------------------------------------
 * Public API: usbhost_cancel_ep
 * ------------------------------------------------------------------------- */

void usbhost_cancel_ep(const char busid[USBIP_BUSID_SIZE], uint8_t ep_addr)
{
    if (!busid) {
        return;
    }
    uint8_t            dev_addr = 0;
    SemaphoreHandle_t  ep_mutex = NULL;
    int                slot     = -1;
    uint8_t            ep_idx   = ep_mutex_index(ep_addr);

    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    slot = find_slot_by_busid_locked(busid);
    if (slot >= 0) {
        dev_addr = s_state.devices[slot].dev_addr;
        ep_mutex = get_ep_submit_mutex_locked(slot, ep_addr);
    }
    xSemaphoreGive(s_state.state_mutex);

    if (dev_addr == 0) {
        return;
    }
    /* Only act if there's actually an URB in flight at TinyUSB level on
     * this EP. Most UNLINKs target URBs queued in our lane (not yet
     * submitted to TinyUSB); aborting+resetting the EP for those would
     * gratuitously kill an in-flight URB on the same EP that has no
     * relation to the kernel's UNLINK target.
     *
     * Step 3 was over-eager: it reset the EP on EVERY UNLINK, causing
     * 14 close+open cycles per mpremote close-storm and killing valid
     * in-progress data transfers (raw-REPL banner). Only the kernel-
     * targeted URB needs cancelling at TinyUSB level; lane-queued URBs
     * are skipped via the cancel flag check before submit. */
    usbhost_inflight_t *current = NULL;
    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    if (slot >= 0 && ep_idx < 32) {
        current = s_state.devices[slot].current_inflight[ep_idx];
    }
    xSemaphoreGive(s_state.state_mutex);

    if (current == NULL) {
        /* Nothing in flight; URB is either in lane queue (will synthesise
         * on dispatch via cancel flag) or already retired. No EP action. */
        ESP_LOGI(TAG, "cancel_ep: dev=%.32s ep=0x%02x no_current_inflight",
                 busid, ep_addr);
        return;
    }

    ESP_LOGI(TAG, "cancel_ep: dev=%.32s ep=0x%02x (pre)", busid, ep_addr);
    bool abort_ok = tuh_edpt_abort_xfer(dev_addr, ep_addr);
    ESP_LOGI(TAG, "cancel_ep: dev=%.32s ep=0x%02x (post=%d)",
             busid, ep_addr, (int)abort_ok);
    if (!abort_ok) {
        ESP_LOGW(TAG, "tuh_edpt_abort_xfer ep=0x%02x returned false", ep_addr);
    }

    /* Synthesise the completion. TinyUSB's DWC2 hcd_edpt_abort_xfer
     * disables the channel but does NOT reliably fire the user
     * complete_cb (the channel-disable interrupt fires xfer_complete
     * for control transfers via usbh_control_xfer_cb but not for the
     * generic ep_callback path). Without this synthesis, in-flight URBs
     * leak: cancel_done_sem in the read loop times out, the responder
     * never sees the URB, the inflight + buf are leaked, and on a busy
     * pipe (cdc-acm with 16 read URBs) a single mpremote close-storm
     * can leak all 16, with subsequent attaches failing.
     *
     * The atomic completed flag arbitrates: if TinyUSB's natural
     * completion fires first, this synthesis is a no-op. If the
     * synthesis wins, the natural completion is a no-op. Either way
     * exactly one path frees the inflight. */
    /* R24 diagnostic counters. */
    static uint32_t s_synth_calls = 0;
    static uint32_t s_synth_no_inflight = 0;
    static uint32_t s_synth_lost_to_natural = 0;
    static uint32_t s_synth_wins = 0;
    s_synth_calls++;
    usbhost_inflight_t *inflight = NULL;
    /* usbh_edpt_busy is in usbh_pvt.h but we read it via the public
     * usbh.h header path; declare prototype locally to avoid pulling
     * the private header in. */
    extern bool usbh_edpt_busy(uint8_t dev_addr, uint8_t ep_addr);
    bool ep_busy_pre  = usbh_edpt_busy(dev_addr, ep_addr);
    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    if (slot >= 0 && ep_idx < 32) {
        inflight = s_state.devices[slot].current_inflight[ep_idx];
        if (inflight != NULL) {
            s_state.devices[slot].current_inflight[ep_idx] = NULL;
        }
    }
    xSemaphoreGive(s_state.state_mutex);

    if (inflight == NULL) {
        s_synth_no_inflight++;
        ESP_LOGI(TAG, "synth: ep=0x%02x busy_pre=%d NO_INFLIGHT (calls=%" PRIu32
                      " ni=%" PRIu32 " lost=%" PRIu32 " wins=%" PRIu32 ")",
                 ep_addr, (int)ep_busy_pre, s_synth_calls,
                 s_synth_no_inflight, s_synth_lost_to_natural, s_synth_wins);
        return; /* nothing in flight */
    }

    uint32_t expected = 0;
    if (!__atomic_compare_exchange_n(&inflight->completed, &expected, 1,
                                     false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        s_synth_lost_to_natural++;
        ESP_LOGI(TAG, "synth: ep=0x%02x ifl=%p LOST_TO_NATURAL", ep_addr, inflight);
        return; /* TinyUSB callback won the race */
    }
    s_synth_wins++;
#ifdef R27_DEADLOCK_TRACE
    ESP_LOGI(TAG, "synth: seq=%" PRIu32 " ep=0x%02x ifl=%p WON busy_pre=%d "
                  "(calls=%" PRIu32 " wins=%" PRIu32 ")",
             inflight->trace_seq, ep_addr, inflight, (int)ep_busy_pre,
             s_synth_calls, s_synth_wins);
#else
    ESP_LOGI(TAG, "synth: ep=0x%02x ifl=%p WON busy_pre=%d (calls=%" PRIu32
                  " wins=%" PRIu32 ")",
             ep_addr, inflight, (int)ep_busy_pre,
             s_synth_calls, s_synth_wins);
#endif

    /* DWC2 channel reset: after tuh_edpt_abort_xfer, the DWC2 channel
     * stays half-allocated and subsequent tuh_edpt_xfer calls return
     * false (HCD allocation rejected) for the lifetime of the device.
     * Step 1 instrumentation confirmed: ep_status.busy=0 / claimed=0
     * (TinyUSB high level clean), but hcd_edpt_xfer rejects new submits.
     * Closing then re-opening the endpoint forces a clean DWC2 channel
     * teardown + re-allocation. EP0 control is excluded — it is opened
     * implicitly at SetAddress and tuh_edpt_close on EP0 is undefined. */
    if (ep_addr != 0) {
        usbhost_ep_t ep_cache;
        memset(&ep_cache, 0, sizeof(ep_cache));
        bool found = false;
        xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
        if (slot >= 0) {
            for (uint8_t i = 0; i < s_state.devices[slot].num_endpoints; i++) {
                if (s_state.devices[slot].endpoints[i].address == ep_addr) {
                    ep_cache = s_state.devices[slot].endpoints[i];
                    found = true;
                    break;
                }
            }
        }
        xSemaphoreGive(s_state.state_mutex);

        if (found) {
            /* Hold ep_submit_mutex across close+open+CLEAR_FEATURE so a
             * concurrent submit cannot land mid-reset. */
            if (ep_mutex) {
                xSemaphoreTake(ep_mutex, portMAX_DELAY);
            }
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

            /* Send CLEAR_FEATURE(ENDPOINT_HALT) to reset the device-side
             * data-toggle. close+open resets our (host) toggle to DATA0,
             * but the device keeps its own toggle which may be at DATA1
             * mid-stream. After cancel, we and the device disagree on
             * the next expected PID; IN transactions get rejected as
             * stale until both sides are reset. CLEAR_FEATURE is the
             * standard USB way to force device-side reset.
             *
             * This is what the IDF backend's halt+flush+clear sequence
             * was doing under the hood (clear = CLEAR_FEATURE). TinyUSB
             * doesn't expose it as a one-shot helper, so build the
             * setup packet directly. tuh_control_xfer with NULL cb
             * is synchronous (blocks until complete or stack idle). */
            tusb_control_request_t setup;
            memset(&setup, 0, sizeof(setup));
            setup.bmRequestType_bit.recipient = TUSB_REQ_RCPT_ENDPOINT;
            setup.bmRequestType_bit.type      = TUSB_REQ_TYPE_STANDARD;
            setup.bmRequestType_bit.direction = TUSB_DIR_OUT;
            setup.bRequest = TUSB_REQ_CLEAR_FEATURE;
            setup.wValue   = TUSB_REQ_FEATURE_EDPT_HALT;
            setup.wIndex   = ep_addr;
            setup.wLength  = 0;
            tuh_xfer_t cf;
            memset(&cf, 0, sizeof(cf));
            cf.daddr   = dev_addr;
            cf.ep_addr = 0;
            cf.setup   = &setup;
            cf.buffer  = NULL;
            cf.complete_cb = NULL; /* synchronous */
            cf.user_data   = 0;
            bool cf_ok = tuh_control_xfer(&cf);

            if (ep_mutex) {
                xSemaphoreGive(ep_mutex);
            }
            ESP_LOGI(TAG, "ep_reset: ep=0x%02x close=%d open=%d clear_feat=%d",
                     ep_addr, (int)close_ok, (int)open_ok, (int)cf_ok);
        }
    }

    /* We claimed completion. Synthesise -ECONNRESET (cancelled). */
    int    status = -ECONNRESET;
    size_t in_len = 0;

    if (inflight->done_sem) {
        inflight->sync_status = status;
        inflight->sync_in_len = in_len;
        xSemaphoreGive(inflight->done_sem);
        return; /* sync caller frees */
    }

    void (*cb)(void *, int, size_t) = inflight->user_cb;
    void *ctx = inflight->user_ctx;
    inflight_free(inflight);
    if (cb) {
        cb(ctx, status, in_len);
    }
}

/* -------------------------------------------------------------------------
 * R27 URB watchdog: bound URB-completion latency
 *
 * Walks current_inflight[] every USBHOST_WATCHDOG_TICK_MS and finds
 * URBs whose t_submit_us is older than USBHOST_WATCHDOG_TIMEOUT_US.
 * For each stale entry:
 *
 *   1. Atomic CAS on watchdog_armed (0 -> 1) to prevent re-fire on the
 *      next tick while the heavy recovery is in progress. If the CAS
 *      fails, another path (typically the watchdog itself on a previous
 *      tick that hasn't released the inflight yet) is already handling
 *      this URB.
 *
 *   2. Run gotcha #5/#6 recovery without holding any user mutex
 *      (gotcha #7): tuh_edpt_abort_xfer + close + open +
 *      CLEAR_FEATURE(ENDPOINT_HALT). Same shape as usbhost_cancel_ep.
 *
 *   3. Atomic CAS on inflight->completed (0 -> 1) to claim the
 *      synthesised completion. If a natural TinyUSB callback fires
 *      between steps 1 and 3, the natural path wins and we bow out
 *      without firing user_cb a second time.
 *
 *   4. Synthesise -ETIMEDOUT through the user callback so the lane
 *      task's RET_SUBMIT path runs as if the URB completed (with
 *      error). The kernel-side cdc-acm sees a giveback and never
 *      hits usb_poison_urb D-state.
 *
 * The watchdog-vs-cancel race is benign: both paths take the same
 * inflight->completed CAS gate; whichever fires first wins, the other
 * sees completed==1 and skips. Recovery operations (close+open+
 * CLEAR_FEATURE) on the same EP are idempotent in the sense that the
 * second invocation just resets an already-reset state.
 *
 * The cost when nothing is stuck: one task wake every 100 ms, which
 * walks USBHOST_MAX_DEVICES * 32 = 128 pointer slots and exits. No
 * mallocs, no syscalls, no UART output unless a stale URB is found.
 * ------------------------------------------------------------------------- */

static void usbhost_watchdog_recover(usbhost_inflight_t *inflight,
                                     int64_t age_us)
{
    /* Inflight must already have been removed from current_inflight[]
     * by the caller (so the slot is free for new submits while recovery
     * runs). dev_addr / ep_addr_full are captured at submit time so we
     * don't need to re-resolve them. */
    uint8_t  dev_addr     = inflight->dev_addr;
    uint8_t  ep_addr_full = inflight->ep_addr_full;
    int      slot         = inflight->slot_idx;
    uint8_t  ep_idx       = inflight->ep_idx;
    bool     is_control   = inflight->is_control;

    /* newlib-nano printf does not support %lld / %" PRId64 ".
     * Cast to int32_t for the age (URB ages exceeding INT32_MAX us
     * = ~35 minutes are not interesting; the watchdog timeout is
     * seconds). R23 deep-dive established this constraint. */
    int32_t age_us_i32 = (age_us > (int64_t)INT32_MAX) ? INT32_MAX
                       : (age_us < 0)                  ? 0
                                                       : (int32_t)age_us;
#ifdef R27_DEADLOCK_TRACE
    ESP_LOGW(TAG, "watchdog: synth seq=%" PRIu32 " ep=0x%02x dev=%u "
                  "age=%" PRId32 "us ifl=%p (running recovery)",
             inflight->trace_seq, ep_addr_full, (unsigned)dev_addr,
             age_us_i32, inflight);
#else
    ESP_LOGW(TAG, "watchdog: stale URB ep=0x%02x dev=%u age=%" PRId32
                  "us ifl=%p (running recovery)",
             ep_addr_full, (unsigned)dev_addr, age_us_i32, inflight);
#endif

    /* Step 1: abort the stuck transfer. No mutex held (gotcha #7). */
    bool abort_ok = tuh_edpt_abort_xfer(dev_addr, ep_addr_full);
    if (!abort_ok) {
        ESP_LOGW(TAG, "watchdog: tuh_edpt_abort_xfer ep=0x%02x returned false",
                 ep_addr_full);
    }

    /* Step 2: claim the completion BEFORE the heavy recovery so a late-
     * firing natural callback doesn't double-fire user_cb. */
    uint32_t expected = 0;
    if (!__atomic_compare_exchange_n(&inflight->completed, &expected, 1,
                                     false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        /* Natural completion won the race after we removed from
         * current_inflight[]. The natural-callback path will run user_cb;
         * we just exit. inflight is owned by the natural-callback path. */
        ESP_LOGI(TAG, "watchdog: ep=0x%02x ifl=%p natural completion won race",
                 ep_addr_full, inflight);
        return;
    }

    /* Step 3: gotcha #5/#6 recovery. Skip for EP0 control (close on EP0
     * is undefined; the abort alone is sufficient). */
    if (!is_control && ep_addr_full != 0 && slot >= 0 && ep_idx < 32) {
        usbhost_ep_t ep_cache;
        memset(&ep_cache, 0, sizeof(ep_cache));
        bool found = false;
        SemaphoreHandle_t ep_mutex = NULL;
        xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
        if (slot < USBHOST_MAX_DEVICES && s_state.devices[slot].in_use) {
            for (uint8_t i = 0; i < s_state.devices[slot].num_endpoints; i++) {
                if (s_state.devices[slot].endpoints[i].address == ep_addr_full) {
                    ep_cache = s_state.devices[slot].endpoints[i];
                    found = true;
                    break;
                }
            }
            ep_mutex = get_ep_submit_mutex_locked(slot, ep_addr_full);
        }
        xSemaphoreGive(s_state.state_mutex);

        if (found) {
            /* Hold ep_submit_mutex across close+open+CLEAR_FEATURE so a
             * concurrent submit cannot land mid-reset. The mutex is NOT
             * held across abort (gotcha #7) but is safe across the rest
             * of the recovery sequence which is task-context only. */
            if (ep_mutex) {
                xSemaphoreTake(ep_mutex, portMAX_DELAY);
            }
            bool close_ok = tuh_edpt_close(dev_addr, ep_addr_full);
            tusb_desc_endpoint_t ep_desc;
            memset(&ep_desc, 0, sizeof(ep_desc));
            ep_desc.bLength             = 7;
            ep_desc.bDescriptorType     = 0x05;
            ep_desc.bEndpointAddress    = ep_cache.address;
            ep_desc.bmAttributes.xfer   = ep_cache.attributes & 0x03;
            ep_desc.wMaxPacketSize      = ep_cache.max_packet_size;
            ep_desc.bInterval           = ep_cache.interval;
            bool open_ok = tuh_edpt_open(dev_addr, &ep_desc);

            tusb_control_request_t setup;
            memset(&setup, 0, sizeof(setup));
            setup.bmRequestType_bit.recipient = TUSB_REQ_RCPT_ENDPOINT;
            setup.bmRequestType_bit.type      = TUSB_REQ_TYPE_STANDARD;
            setup.bmRequestType_bit.direction = TUSB_DIR_OUT;
            setup.bRequest = TUSB_REQ_CLEAR_FEATURE;
            setup.wValue   = TUSB_REQ_FEATURE_EDPT_HALT;
            setup.wIndex   = ep_addr_full;
            setup.wLength  = 0;
            tuh_xfer_t cf;
            memset(&cf, 0, sizeof(cf));
            cf.daddr   = dev_addr;
            cf.ep_addr = 0;
            cf.setup   = &setup;
            cf.buffer  = NULL;
            cf.complete_cb = NULL; /* synchronous */
            cf.user_data   = 0;
            bool cf_ok = tuh_control_xfer(&cf);

            if (ep_mutex) {
                xSemaphoreGive(ep_mutex);
            }
            ESP_LOGW(TAG, "watchdog: ep_reset ep=0x%02x close=%d open=%d "
                          "clear_feat=%d", ep_addr_full,
                     (int)close_ok, (int)open_ok, (int)cf_ok);
        }
    }

    /* Step 4: synthesise -ETIMEDOUT to the user callback. Mirrors the
     * trailing path of usbhost_cancel_ep but with a different status
     * code so the responder can distinguish a cancel-storm cancellation
     * (-ECONNRESET) from a stuck-URB watchdog fire (-ETIMEDOUT) in
     * any future log analysis. */
    int    status = -ETIMEDOUT;
    size_t in_len = 0;

    if (inflight->done_sem) {
        inflight->sync_status = status;
        inflight->sync_in_len = in_len;
        xSemaphoreGive(inflight->done_sem);
        return; /* sync caller owns inflight */
    }

    void (*cb)(void *, int, size_t) = inflight->user_cb;
    void *ctx = inflight->user_ctx;
    inflight_free(inflight);
    if (cb) {
        cb(ctx, status, in_len);
    }
}

static void usbhost_watchdog_task(void *arg)
{
    (void)arg;
    static uint32_t s_watchdog_ticks = 0;
    static uint32_t s_watchdog_fires = 0;

    ESP_LOGI(TAG, "URB watchdog task running (timeout=%d us, tick=%d ms, "
                  "prio=%d, core=%d)",
             USBHOST_WATCHDOG_TIMEOUT_US, USBHOST_WATCHDOG_TICK_MS,
             USBHOST_WATCHDOG_TASK_PRIORITY, USBHOST_TASK_CORE);

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(USBHOST_WATCHDOG_TICK_MS));
        s_watchdog_ticks++;

        int64_t now = esp_timer_get_time();

        /* Walk current_inflight[]. We collect victims under state_mutex,
         * release the mutex, then run recovery on each. The recovery
         * itself must not hold state_mutex because tuh_control_xfer for
         * CLEAR_FEATURE re-enters TinyUSB layers that may also touch
         * our state from inside their callbacks. */
        usbhost_inflight_t *victims[USBHOST_MAX_DEVICES * 32];
        int64_t             ages[USBHOST_MAX_DEVICES * 32];
        int n_victims = 0;

        xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
        for (int slot = 0; slot < USBHOST_MAX_DEVICES; slot++) {
            if (!s_state.devices[slot].in_use) {
                continue;
            }
            for (uint8_t ep = 0; ep < 32; ep++) {
                usbhost_inflight_t *ifl =
                    s_state.devices[slot].current_inflight[ep];
                if (ifl == NULL) {
                    continue;
                }
                if (ifl->t_submit_us == 0) {
                    /* Submit hasn't returned yet; tuh_*_xfer is in
                     * progress. Skip; the watchdog only fires on
                     * URBs that successfully entered the TinyUSB
                     * stack and then got stuck. */
                    continue;
                }
                /* Skip endpoints that legitimately wait indefinitely
                 * for device-side activity:
                 *   - Interrupt-IN: CDC-ACM modem-status notifications
                 *     (ep 0x81) sit pending forever until line state
                 *     changes. Cancelling spuriously breaks the TTY
                 *     contract.
                 *   - Isochronous: long completion windows, scheduled.
                 *   - Bulk-IN: under DMA mode the controller NAK-loops
                 *     in HW with no IRQ activity, so an IN URB waiting
                 *     for device data appears identical to a wedged
                 *     URB to a software-only watchdog. Cancelling
                 *     spuriously kills legitimate idle reads (the
                 *     CDC-ACM bulk-IN queue normally has many URBs
                 *     pending while the user types nothing). Under
                 *     slave mode the same logic was masked because
                 *     each NAK fired an IRQ; under DMA mode there is
                 *     no such heartbeat.
                 *
                 * The watchdog's remaining responsibility is to bound
                 * bulk-OUT and control completion latency: those are
                 * host-driven (we have data to send) and should
                 * complete in milliseconds; if they wedge for 2 s
                 * something is structurally wrong. */
                if (ifl->ep_xfer_type == 1 /* TUSB_XFER_ISOCHRONOUS */ ||
                    ifl->ep_xfer_type == 3 /* TUSB_XFER_INTERRUPT  */) {
                    continue;
                }
                /* For bulk endpoints, skip IN direction (device-driven).
                 * Watch only OUT (host-driven) and control (handshake). */
                if (ifl->ep_xfer_type == 2 /* TUSB_XFER_BULK */ &&
                    (ifl->ep_addr_full & 0x80) != 0) {
                    continue;
                }
                int64_t age = now - ifl->t_submit_us;
                if (age < (int64_t)USBHOST_WATCHDOG_TIMEOUT_US) {
                    continue;
                }
                /* Atomic CAS on watchdog_armed: 0 -> 1. If the watchdog
                 * fired on this inflight on a prior tick (recovery
                 * running on the worker thread, same watchdog task)
                 * then watchdog_armed is already 1 and we skip; the
                 * inflight will be freed by that earlier recovery
                 * shortly. */
                uint32_t expected = 0;
                if (!__atomic_compare_exchange_n(&ifl->watchdog_armed,
                                                 &expected, 1, false,
                                                 __ATOMIC_ACQ_REL,
                                                 __ATOMIC_ACQUIRE)) {
                    continue;
                }
                /* Claim the slot: clear current_inflight[] before we
                 * run recovery so a parallel cancel_ep on the same EP
                 * sees no_current_inflight and doesn't double-recover. */
                s_state.devices[slot].current_inflight[ep] = NULL;
                if (n_victims < (int)(sizeof(victims)/sizeof(victims[0]))) {
                    victims[n_victims] = ifl;
                    ages[n_victims]    = age;
                    n_victims++;
                }
            }
        }
        xSemaphoreGive(s_state.state_mutex);

        for (int i = 0; i < n_victims; i++) {
            s_watchdog_fires++;
            usbhost_watchdog_recover(victims[i], ages[i]);
        }

        /* Periodic heartbeat at INFO so a tail of logs shows the watchdog
         * is alive. Once every 600 ticks (60 s at 100 ms/tick). The
         * ESP_LOGW lines on actual fires are the loud signal; this is
         * just "watchdog is still running, no URBs stuck". */
        if (s_watchdog_ticks % 600 == 0) {
            ESP_LOGI(TAG, "watchdog: ticks=%" PRIu32 " fires=%" PRIu32,
                     s_watchdog_ticks, s_watchdog_fires);
        }
    }
}

/* -------------------------------------------------------------------------
 * Synchronous wrappers
 *
 * Build the inflight record inline so we retain the pointer throughout
 * and can read sync_status / sync_in_len after xfer_complete_cb fires.
 * ------------------------------------------------------------------------- */

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

    const bool is_in = is_control
        ? (setup != NULL && (setup->bmRequestType & 0x80) != 0)
        : (ep_addr & 0x80) != 0;
    const size_t payload_len = is_in ? in_capacity : out_len;
    if (payload_len > USBHOST_MAX_TRANSFER) {
        return -EMSGSIZE;
    }

    uint8_t dev_addr = 0;
    size_t xfer_payload = payload_len;
    SemaphoreHandle_t ep_mutex = NULL;
    const uint8_t submit_ep = is_control ? 0x00 : ep_addr;

    /* Symmetric MPS lookup with submit_xfer. No hardcoded fallback. */
    bool sync_mps_unavailable = false;
    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_busid_locked(busid);
    if (slot >= 0) {
        dev_addr = s_state.devices[slot].dev_addr;
        if (!is_control && is_in && in_capacity > 0) {
            uint16_t mps = get_endpoint_mps_locked(slot, ep_addr);
            if (mps == 0) {
                sync_mps_unavailable = true;
            } else if ((xfer_payload % mps) != 0) {
                xfer_payload = ((xfer_payload + mps - 1) / mps) * mps;
            }
        }
        ep_mutex = get_ep_submit_mutex_locked(slot, submit_ep);
    }
    xSemaphoreGive(s_state.state_mutex);

    if (dev_addr == 0) {
        return -ENODEV;
    }
    if (sync_mps_unavailable) {
        ESP_LOGE(TAG, "sync_xfer: IN ep=0x%02x has no cached MPS, "
                      "refusing submit (slot=%d, dev=%u)",
                 ep_addr, slot, dev_addr);
        return -ENODEV;
    }

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "sync_submit: dev=%.32s ep=0x%02x dir=%s len=%u",
                 busid, ep_addr,
                 is_control ? "CTRL" : (is_in ? "IN" : "OUT"),
                 (unsigned)payload_len);
    }

    usbhost_inflight_t *inflight = calloc(1, sizeof(*inflight));
    if (!inflight) {
        return -ENOMEM;
    }
    inflight->done_sem    = xSemaphoreCreateBinary();
    inflight->user_cb     = NULL;
    inflight->is_in       = is_in;
    inflight->is_control  = is_control;
    inflight->in_data     = in_data;
    inflight->in_capacity = in_capacity;
    inflight->payload_len = payload_len;

    if (!inflight->done_sem) {
        inflight_free(inflight);
        return -ENOMEM;
    }

    size_t buf_len = is_control ? (8 + xfer_payload) : xfer_payload;
    if (buf_len == 0) {
        buf_len = 1;
    }
    inflight->buf = usbhost_buf_alloc(buf_len);
    inflight->buf_len = buf_len;
    if (!inflight->buf) {
        inflight_free(inflight);
        return -ENOMEM;
    }
    memset(inflight->buf, 0, buf_len);

    if (is_control && setup) {
        memcpy(inflight->buf, setup, 8);
        if (!is_in && out_len > 0 && out_data) {
            size_t copy = (out_len < xfer_payload) ? out_len : xfer_payload;
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
        /* See comment in submit_xfer: tuh_xfer_t.setup and .buflen
         * are in a union; setting buflen for control xfers overwrites
         * setup with the integer cast as a pointer. */
        xfer.setup  = (const tusb_control_request_t *)inflight->buf;
        xfer.buffer = inflight->buf + 8;
    } else {
        xfer.buffer = inflight->buf;
        xfer.buflen = (uint32_t)xfer_payload;
    }

    if (ep_mutex) {
        xSemaphoreTake(ep_mutex, portMAX_DELAY);
    }
    bool ok = is_control ? tuh_control_xfer(&xfer) : tuh_edpt_xfer(&xfer);
    if (ep_mutex) {
        xSemaphoreGive(ep_mutex);
    }

    if (!ok) {
        ESP_LOGW(TAG, "sync_xfer: tuh xfer rejected ep=0x%02x", ep_addr);
        inflight_free(inflight);
        return -EIO;
    }

    const TickType_t poll_ticks = pdMS_TO_TICKS(50);
    uint32_t elapsed_ms = 0;
    bool cancelled = false;

    while (xSemaphoreTake(inflight->done_sem, poll_ticks) != pdTRUE) {
        elapsed_ms += 50;
        if (!cancelled && cancel && *cancel) {
            cancelled = true;
            tuh_edpt_abort_xfer(dev_addr, is_control ? 0 : ep_addr);
        }
        if (is_control && elapsed_ms >= USBHOST_CONTROL_TIMEOUT_MS) {
            tuh_edpt_abort_xfer(dev_addr, 0);
            xSemaphoreTake(inflight->done_sem, poll_ticks);
            break;
        }
    }

    int status = inflight->sync_status;
    *in_len    = inflight->sync_in_len;
    inflight_free(inflight);

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "sync_complete: dev=%.32s ep=0x%02x status=%d actual=%u",
                 busid, ep_addr, status, (unsigned)*in_len);
    }
    return status;
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
