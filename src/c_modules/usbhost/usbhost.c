/* Annealage Pod: USB host backend.
 *
 * Implements the API in usbhost.h against the ESP-IDF `usb_host`
 * component (`components/usb/include/usb/usb_host.h`).
 *
 * Concurrency model (R15 refactor):
 *
 *   The IDF requires that a single thread of control owns the call
 *   to `usb_host_client_handle_events`. The earlier shape ran every
 *   submit synchronously inside the worker, which serialised all
 *   URBs through one event-pump call. CDC ACM open submits an
 *   interrupt-IN that is intentionally pending forever, then a bulk
 *   OUT class control; with serialised dispatch the bulk OUT queues
 *   behind the pending IN and the device never sees line state.
 *
 *   The worker now does only event pumping. Submit functions are
 *   called from any task context, fill out a per-call inflight
 *   record (held on the caller's stack), allocate an IDF
 *   `usb_transfer_t`, and submit it. The IDF transfer-completion
 *   callback (which fires inside the worker's
 *   `usb_host_client_handle_events`) writes back into the inflight
 *   record and gives `done_sem`. The submit caller blocks on its
 *   own `done_sem`, so many URBs can be in flight concurrently.
 *
 *   Cancellation: when `*cancel` flips true, the submit caller
 *   halts and flushes the endpoint, which causes the IDF to
 *   complete the URB with status CANCELED. The completion callback
 *   then signals `done_sem` as usual.
 *
 *   Interfaces are claimed eagerly in `handle_new_device` (the lazy
 *   path in the previous implementation interacted badly with the
 *   busy-wait event pump). Hubs are still rejected before claim.
 *
 * Vendoring: the slot/pipe topology was shape-equivalent to
 * `referencea/esp-usbip-bridge/main/usb_backend.c`; this file is a
 * clean rewrite of that shape. The wire-protocol structures
 * (usbip_dev_record_t, usbip_setup_packet_t) come from the mpy-pod
 * usbip module headers, not the reference.
 */

#include "usbhost.h"

#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "usb/usb_host.h"
#include "usb/usb_helpers.h"
#include "usb/usb_types_ch9.h"
#include "usb/usb_types_stack.h"

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
/* Control transfers complete in ms; non-control IN URBs may legitimately
 * pend forever (CDC interrupt-IN waits for a notification). 0 = no
 * timeout in the IDF. */
#ifndef USBHOST_CONTROL_TIMEOUT_MS
#define USBHOST_CONTROL_TIMEOUT_MS 5000
#endif
#ifndef USBHOST_EVENT_QUEUE_LEN
#define USBHOST_EVENT_QUEUE_LEN 16
#endif
#ifndef USBHOST_TASK_CORE
#define USBHOST_TASK_CORE 1   /* APP_CPU per spec.md S2 / architecture.md S3 */
#endif
#ifndef USBHOST_DAEMON_TASK_PRIORITY
#define USBHOST_DAEMON_TASK_PRIORITY 10
#endif
#ifndef USBHOST_WORKER_TASK_PRIORITY
/* R25 step 3: bumped from 9 to 20. The worker calls
 * usb_host_client_handle_events which dispatches transfer_done_cb in
 * task context. R23 deep-dive measured avg_round=165 ms which the
 * R25 refill-path trace traced to user-task wakeup latency, NOT HCD
 * pipeline gap. At 9 the worker sat below responder (11) and lwIP
 * (18) on core 1 (APP_CPU); under read-heavy bench load it could
 * stay ready for tens of ms per burst. 20 puts it above both,
 * still below Wi-Fi (23). */
#define USBHOST_WORKER_TASK_PRIORITY 20
#endif
#ifndef USBHOST_DAEMON_TASK_STACK
#define USBHOST_DAEMON_TASK_STACK 4096
#endif
#ifndef USBHOST_WORKER_TASK_STACK
#define USBHOST_WORKER_TASK_STACK 8192
#endif

#ifndef USB_CLASS_HUB
#define USB_CLASS_HUB 0x09
#endif

static const char *TAG = "usbhost";

/* Event queue between the IDF client-event callback (runs in the
 * worker's `usb_host_client_handle_events` context) and the worker's
 * main loop. Decouples the callback from device-attach bookkeeping,
 * which would otherwise re-enter the IDF from inside the callback. */
typedef enum {
    USBHOST_EVENT_NEW_DEV = 1,
    USBHOST_EVENT_DEV_GONE = 2,
} usbhost_event_type_t;

typedef struct {
    usbhost_event_type_t type;
    union {
        uint8_t address;
        usb_device_handle_t dev_hdl;
    } u;
} usbhost_event_t;

/* Cached endpoint description, populated from the active config
 * descriptor at device-attach time. */
typedef struct {
    uint8_t  address;
    uint8_t  attributes;
    uint16_t max_packet_size;
    uint8_t  interval;
} usbhost_ep_t;

/* Per-call inflight record. Heap-allocated and refcounted because the
 * For synchronous submits (submit_xfer), the caller blocks on done_sem
 * until the IDF callback fires. The callback gives done_sem and returns;
 * the caller then owns the record and frees it directly. Single-owner
 * by construction: no ref-count needed.
 *
 * For async submits (usbhost_submit_async), done_sem is NULL and
 * user_cb + user_ctx are set instead. The IDF callback calls user_cb
 * and frees the record directly. Again single-owner by construction. */
typedef struct usbhost_inflight {
    SemaphoreHandle_t      done_sem;   /* NULL for async submits */
    usb_transfer_t        *xfer;
    /* Async completion callback. NULL for synchronous submits. */
    void                 (*user_cb)(void *ctx, int status, size_t in_len);
    void                  *user_ctx;
    /* Cached transfer parameters needed by the callback to extract
     * IN data without re-reading fields from xfer after completion. */
    bool                   is_in;
    bool                   is_control;
    size_t                 payload_len;    /* in_capacity for IN, out_len for OUT */
    uint8_t               *in_data;       /* pointer into caller's buffer (async) */
    size_t                 in_capacity;
    /* R23 deep-dive instrumentation: microsecond timing of the IDF
     * submit -> callback path. Always captured when async; cost is
     * one esp_timer_get_time() call per URB. */
    int64_t                t_submit_pre;   /* before usb_host_transfer_submit */
    int64_t                t_submit_post;  /* after usb_host_transfer_submit returns */
    uint8_t                ep_for_log;     /* ep_addr for log line */
} usbhost_inflight_t;

typedef struct {
    bool                 in_use;
    bool                 interfaces_claimed;
    usb_device_handle_t  dev_hdl;
    usbip_dev_record_t   device;
    uint8_t              num_endpoints;
    usbhost_ep_t         endpoints[USBHOST_MAX_ENDPOINTS];
    /* Per-endpoint submit mutexes serialise the IDF-side
     * usb_host_transfer_submit call AND the cancel halt/flush/clear
     * sequence so URBs reach the wire in the order the read loop
     * dequeued them, and so concurrent EP commands cannot wedge the
     * IDF's per-EP state machine (the `EP command error:
     * ESP_ERR_INVALID_STATE` symptom seen in iter4). Indexed by
     * ep_addr (0..15 OUT, 0x80..0x9F IN -> high bit folded). Lazy-
     * created on first use. */
    SemaphoreHandle_t    ep_submit_mutex[32];
} usbhost_slot_t;

typedef struct {
    bool                       started;
    SemaphoreHandle_t          state_mutex;
    QueueHandle_t              event_queue;
    TaskHandle_t               worker_hdl;
    TaskHandle_t               daemon_hdl;
    usb_host_client_handle_t   client_hdl;
    usbhost_slot_t             devices[USBHOST_MAX_DEVICES];
} usbhost_state_t;

static usbhost_state_t s_state;

static volatile bool s_urb_verbose = false;

void usbhost_set_verbose(bool enable)
{
    s_urb_verbose = enable;
    ESP_LOGI(TAG, "URB verbose logging %s", enable ? "enabled" : "disabled");
}

bool usbhost_is_verbose(void)
{
    return s_urb_verbose;
}

/* ------------------------------------------------------------------------ */
/* Helpers                                                                  */
/* ------------------------------------------------------------------------ */

static uint32_t speed_to_usbip(usb_speed_t speed)
{
    switch (speed) {
    case USB_SPEED_LOW:  return 1;
    case USB_SPEED_FULL: return 2;
    case USB_SPEED_HIGH: return 3;
    default:             return 0;
    }
}

static int map_transfer_status_to_errno(usb_transfer_status_t status)
{
    switch (status) {
    case USB_TRANSFER_STATUS_COMPLETED: return 0;
    case USB_TRANSFER_STATUS_TIMED_OUT: return -ETIMEDOUT;
    case USB_TRANSFER_STATUS_CANCELED:  return -ECONNRESET;
    case USB_TRANSFER_STATUS_STALL:     return -EPIPE;
    case USB_TRANSFER_STATUS_NO_DEVICE: return -ENODEV;
    default:                            return -EIO;
    }
}

static bool busid_eq(const char a[USBIP_BUSID_SIZE], const char b[USBIP_BUSID_SIZE])
{
    return memcmp(a, b, USBIP_BUSID_SIZE) == 0;
}

/* Walk the active configuration descriptor; populate desc->num_interfaces /
 * interfaces[] and the caller's endpoint cache. */
static void parse_config_desc(const usb_config_desc_t *cfg,
                              usbip_dev_record_t *desc,
                              usbhost_ep_t *eps_out,
                              uint8_t *num_eps_out)
{
    uint8_t intf_count = 0;
    uint8_t ep_count   = 0;

    if (cfg == NULL || desc == NULL || eps_out == NULL || num_eps_out == NULL) {
        return;
    }
    const uint8_t *raw = (const uint8_t *)cfg;
    const size_t total = cfg->wTotalLength;

    for (size_t off = 0; off + 2 <= total; ) {
        const uint8_t dlen  = raw[off];
        const uint8_t dtype = raw[off + 1];
        if (dlen < 2 || off + dlen > total) {
            break;
        }
        if (dtype == USB_B_DESCRIPTOR_TYPE_INTERFACE && dlen >= 9) {
            if (intf_count < USBIP_MAX_INTERFACES) {
                desc->interfaces[intf_count].interface_class    = raw[off + 5];
                desc->interfaces[intf_count].interface_subclass = raw[off + 6];
                desc->interfaces[intf_count].interface_protocol = raw[off + 7];
                intf_count++;
            }
        } else if (dtype == USB_B_DESCRIPTOR_TYPE_ENDPOINT && dlen >= 7) {
            if (ep_count < USBHOST_MAX_ENDPOINTS) {
                eps_out[ep_count].address         = raw[off + 2];
                eps_out[ep_count].attributes      = raw[off + 3];
                eps_out[ep_count].max_packet_size = (uint16_t)raw[off + 4]
                                                  | ((uint16_t)raw[off + 5] << 8);
                eps_out[ep_count].interval        = raw[off + 6];
                ep_count++;
            }
        }
        off += dlen;
    }

    desc->num_interfaces = intf_count;
    *num_eps_out         = ep_count;
}

static bool is_hub_device(const usb_device_desc_t *dev_desc,
                          const usbip_dev_record_t *desc)
{
    if (dev_desc != NULL && dev_desc->bDeviceClass == USB_CLASS_HUB) {
        return true;
    }
    for (uint8_t i = 0; i < desc->num_interfaces; i++) {
        if (desc->interfaces[i].interface_class == USB_CLASS_HUB) {
            return true;
        }
    }
    return false;
}

/* Caller holds state_mutex. */
static int find_slot_by_busid_locked(const char busid[USBIP_BUSID_SIZE])
{
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        if (s_state.devices[i].in_use && busid_eq(busid, s_state.devices[i].device.busid)) {
            return i;
        }
    }
    return -1;
}

static int find_slot_by_handle_locked(usb_device_handle_t dev_hdl)
{
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        if (s_state.devices[i].in_use && s_state.devices[i].dev_hdl == dev_hdl) {
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
        }
    }
    memset(&s_state.devices[slot], 0, sizeof(s_state.devices[slot]));
}

/* Map ep_addr to a 5-bit slot index: number in low 4 bits, direction
 * in bit 4. EP0 IN and OUT collapse to the same entry (control xfers
 * use the same pipe in both directions). */
static uint8_t ep_mutex_index(uint8_t ep_addr)
{
    return (uint8_t)((ep_addr & 0x0F) | ((ep_addr & 0x80) >> 3));
}

/* Lazy-create and return the per-endpoint submit mutex. Called with
 * state_mutex held. Returns NULL on allocation failure; caller falls
 * back to unserialised submit (degrades to pre-fix behaviour for that
 * one URB rather than failing). */
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

static void release_interfaces_locked(int slot)
{
    if (slot < 0 || slot >= USBHOST_MAX_DEVICES) {
        return;
    }
    if (!s_state.devices[slot].in_use || !s_state.devices[slot].interfaces_claimed) {
        return;
    }
    usb_device_handle_t dev_hdl = s_state.devices[slot].dev_hdl;
    const usbip_dev_record_t *desc = &s_state.devices[slot].device;

    for (uint8_t i = 0; i < desc->num_interfaces; i++) {
        esp_err_t err = usb_host_interface_release(s_state.client_hdl, dev_hdl, i);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "interface_release(%u) failed: %s", i, esp_err_to_name(err));
        }
    }
    s_state.devices[slot].interfaces_claimed = false;
}

/* Eagerly claim every interface in the active configuration. Called
 * inside handle_new_device, off the IDF callback path. */
static esp_err_t claim_interfaces_locked(int slot)
{
    if (slot < 0 || slot >= USBHOST_MAX_DEVICES || !s_state.devices[slot].in_use) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_state.devices[slot].interfaces_claimed) {
        return ESP_OK;
    }
    usb_device_handle_t dev_hdl  = s_state.devices[slot].dev_hdl;
    const usbip_dev_record_t *d  = &s_state.devices[slot].device;

    ESP_LOGI(TAG, "claiming %u interfaces for %s", d->num_interfaces, d->busid);
    for (uint8_t i = 0; i < d->num_interfaces; i++) {
        esp_err_t err = usb_host_interface_claim(s_state.client_hdl, dev_hdl, i, 0);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "interface_claim(%u) failed: %s", i, esp_err_to_name(err));
            return err;
        }
    }
    s_state.devices[slot].interfaces_claimed = true;
    return ESP_OK;
}

static void close_slot_locked(int slot)
{
    if (slot < 0 || slot >= USBHOST_MAX_DEVICES || !s_state.devices[slot].in_use) {
        return;
    }
    release_interfaces_locked(slot);

    usb_device_handle_t dev_hdl = s_state.devices[slot].dev_hdl;
    if (dev_hdl != NULL) {
        esp_err_t err = usb_host_device_close(s_state.client_hdl, dev_hdl);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "device_close failed: %s", esp_err_to_name(err));
        }
    }
    clear_slot_locked(slot);
}

/* ------------------------------------------------------------------------ */
/* IDF client event callback                                                */
/* ------------------------------------------------------------------------ */

static void usb_client_event_cb(const usb_host_client_event_msg_t *msg, void *arg)
{
    (void)arg;
    usbhost_event_t evt;

    if (msg->event == USB_HOST_CLIENT_EVENT_NEW_DEV) {
        ESP_LOGI(TAG, "USB device attached: address=%u", msg->new_dev.address);
        evt.type      = USBHOST_EVENT_NEW_DEV;
        evt.u.address = msg->new_dev.address;
    } else if (msg->event == USB_HOST_CLIENT_EVENT_DEV_GONE) {
        ESP_LOGW(TAG, "USB device gone");
        evt.type      = USBHOST_EVENT_DEV_GONE;
        evt.u.dev_hdl = msg->dev_gone.dev_hdl;
    } else {
        return;
    }
    if (xQueueSend(s_state.event_queue, &evt, 0) != pdTRUE) {
        ESP_LOGW(TAG, "event queue full, dropping event type=%d", (int)evt.type);
    }
}

/* ------------------------------------------------------------------------ */
/* Device attach / detach bookkeeping                                       */
/* ------------------------------------------------------------------------ */

static void handle_new_device(uint8_t address)
{
    usb_device_handle_t dev_hdl = NULL;
    esp_err_t err = usb_host_device_open(s_state.client_hdl, address, &dev_hdl);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "device_open(%u) failed: %s", address, esp_err_to_name(err));
        return;
    }

    const usb_device_desc_t *dev_desc = NULL;
    err = usb_host_get_device_descriptor(dev_hdl, &dev_desc);
    if (err != ESP_OK || dev_desc == NULL) {
        ESP_LOGW(TAG, "get_device_descriptor failed: %s", esp_err_to_name(err));
        usb_host_device_close(s_state.client_hdl, dev_hdl);
        return;
    }

    usb_device_info_t dev_info;
    err = usb_host_device_info(dev_hdl, &dev_info);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "device_info failed: %s", esp_err_to_name(err));
        usb_host_device_close(s_state.client_hdl, dev_hdl);
        return;
    }

    usbip_dev_record_t desc;
    memset(&desc, 0, sizeof(desc));
    desc.present             = true;
    desc.busnum              = 1;
    desc.devnum              = dev_info.dev_addr;
    desc.speed               = speed_to_usbip(dev_info.speed);
    desc.id_vendor           = dev_desc->idVendor;
    desc.id_product          = dev_desc->idProduct;
    desc.bcd_device          = dev_desc->bcdDevice;
    desc.device_class        = dev_desc->bDeviceClass;
    desc.device_subclass     = dev_desc->bDeviceSubClass;
    desc.device_protocol     = dev_desc->bDeviceProtocol;
    desc.num_configurations  = dev_desc->bNumConfigurations;
    desc.configuration_value = dev_info.bConfigurationValue;

    usbhost_ep_t eps[USBHOST_MAX_ENDPOINTS];
    memset(eps, 0, sizeof(eps));
    uint8_t num_eps = 0;

    const usb_config_desc_t *cfg = NULL;
    err = usb_host_get_active_config_descriptor(dev_hdl, &cfg);
    if (err == ESP_OK && cfg != NULL) {
        parse_config_desc(cfg, &desc, eps, &num_eps);
    } else {
        ESP_LOGW(TAG, "active_config_descriptor failed: %s", esp_err_to_name(err));
    }

    if (is_hub_device(dev_desc, &desc)) {
        ESP_LOGI(TAG, "hub at addr=%u, not exporting", address);
        usb_host_device_close(s_state.client_hdl, dev_hdl);
        return;
    }

    snprintf(desc.busid, sizeof(desc.busid), "1-%u", dev_info.dev_addr);
    snprintf(desc.path, sizeof(desc.path), "/esp-usb-host/1-%u", dev_info.dev_addr);

    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);

    int existing = -1;
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        if (s_state.devices[i].in_use && s_state.devices[i].device.devnum == desc.devnum) {
            existing = i;
            break;
        }
    }
    if (existing >= 0) {
        close_slot_locked(existing);
    }

    int slot = find_free_slot_locked();
    if (slot < 0) {
        xSemaphoreGive(s_state.state_mutex);
        ESP_LOGW(TAG, "no free device slot (max=%d)", USBHOST_MAX_DEVICES);
        usb_host_device_close(s_state.client_hdl, dev_hdl);
        return;
    }

    s_state.devices[slot].in_use             = true;
    s_state.devices[slot].interfaces_claimed = false;
    s_state.devices[slot].dev_hdl            = dev_hdl;
    s_state.devices[slot].device             = desc;
    s_state.devices[slot].num_endpoints      = num_eps;
    memcpy(s_state.devices[slot].endpoints, eps, sizeof(eps[0]) * num_eps);

    /* Pre-claim interfaces: the kernel cdc-acm driver pends an
     * interrupt-IN immediately on open, and waiting for that URB to
     * trigger lazy claim deadlocks (the IN never completes until line
     * state is set, which needs the OUT class control to dispatch). */
    esp_err_t cerr = claim_interfaces_locked(slot);
    if (cerr != ESP_OK) {
        ESP_LOGW(TAG, "claim_interfaces failed for %s: %s",
                 desc.busid, esp_err_to_name(cerr));
    }

    xSemaphoreGive(s_state.state_mutex);

    ESP_LOGI(TAG, "exported busid=%s vid=%04x pid=%04x num_intf=%u num_ep=%u",
             desc.busid, desc.id_vendor, desc.id_product,
             desc.num_interfaces, num_eps);
}

static void handle_dev_gone(usb_device_handle_t dev_hdl)
{
    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_handle_locked(dev_hdl);
    if (slot >= 0) {
        char busid[USBIP_BUSID_SIZE];
        memcpy(busid, s_state.devices[slot].device.busid, sizeof(busid));
        close_slot_locked(slot);
        xSemaphoreGive(s_state.state_mutex);
        ESP_LOGI(TAG, "device disconnected: %.32s", busid);
        return;
    }
    xSemaphoreGive(s_state.state_mutex);
}

static void drain_event_queue(void)
{
    usbhost_event_t evt;
    while (xQueueReceive(s_state.event_queue, &evt, 0) == pdTRUE) {
        if (evt.type == USBHOST_EVENT_NEW_DEV) {
            handle_new_device(evt.u.address);
        } else if (evt.type == USBHOST_EVENT_DEV_GONE) {
            handle_dev_gone(evt.u.dev_hdl);
        }
    }
}

/* ------------------------------------------------------------------------ */
/* IDF transfer completion callback                                         */
/* ------------------------------------------------------------------------ */

/* Free an inflight record and its associated IDF transfer.
 * For synchronous submits: called by the submit_xfer caller after
 * done_sem is signalled (single owner at that point). For async
 * submits: called inside transfer_done_cb before invoking user_cb. */
static void inflight_free(usbhost_inflight_t *inflight)
{
    if (inflight == NULL) {
        return;
    }
    if (inflight->done_sem != NULL) {
        vSemaphoreDelete(inflight->done_sem);
    }
    if (inflight->xfer != NULL) {
        usb_host_transfer_free(inflight->xfer);
    }
    free(inflight);
}

static void transfer_done_cb(usb_transfer_t *xfer)
{
    usbhost_inflight_t *inflight = (usbhost_inflight_t *)xfer->context;
    if (inflight == NULL) {
        return;
    }

    /* R23 deep-dive timing: capture us-resolution per-URB IDF latency.
     * t_submit_overhead = how long usb_host_transfer_submit took to
     *   accept the request (synchronous part of submit).
     * t_idf_round = time from submit-accepted to callback firing
     *   (wire time + IDF event-loop scheduling + DMA handling).
     * Aggregated over 100 URBs and emitted as a histogram-summary,
     * with per-direction (IN vs OUT) breakdowns (step 3). */
    if (inflight->user_cb != NULL && inflight->t_submit_pre != 0) {
        /* Combined (all directions) counters. */
        static uint32_t s_count = 0;
        static int64_t  s_sum_overhead_us = 0;
        static int64_t  s_sum_round_us = 0;
        static int64_t  s_max_round_us = 0;
        static int64_t  s_min_round_us = INT64_MAX;
        /* Per-direction counters: [0]=OUT, [1]=IN */
        static uint32_t s_dir_count[2]   = {0, 0};
        static int64_t  s_dir_sum_us[2]  = {0, 0};
        static int64_t  s_dir_min_us[2]  = {INT64_MAX, INT64_MAX};
        static int64_t  s_dir_max_us[2]  = {0, 0};

        int64_t t_complete = esp_timer_get_time();
        int64_t t_overhead = inflight->t_submit_post - inflight->t_submit_pre;
        int64_t t_round    = t_complete - inflight->t_submit_post;
        int     dir_idx    = inflight->is_in ? 1 : 0;

        s_count++;
        s_sum_overhead_us += t_overhead;
        s_sum_round_us    += t_round;
        if (t_round > s_max_round_us) s_max_round_us = t_round;
        if (t_round < s_min_round_us) s_min_round_us = t_round;

        s_dir_count[dir_idx]++;
        s_dir_sum_us[dir_idx] += t_round;
        if (t_round > s_dir_max_us[dir_idx]) s_dir_max_us[dir_idx] = t_round;
        if (t_round < s_dir_min_us[dir_idx]) s_dir_min_us[dir_idx] = t_round;

        if ((s_count % 100) == 0) {
            /* newlib-nano on ESP32 does not support 64-bit printf
             * specifiers. Cast to int32_t; all expected timing values
             * fit (max_round for a 500 ms URB = 500000 us << INT32_MAX). */
            ESP_LOGI(TAG, "idf_timing: n=%" PRIu32
                          " avg_submit=%" PRId32 "us avg_round=%" PRId32 "us"
                          " min_round=%" PRId32 "us max_round=%" PRId32 "us",
                     s_count,
                     (int32_t)(s_sum_overhead_us / (int64_t)s_count),
                     (int32_t)(s_sum_round_us / (int64_t)s_count),
                     (int32_t)s_min_round_us,
                     (int32_t)s_max_round_us);
            /* Per-direction breakdown: OUT then IN. */
            if (s_dir_count[0] > 0) {
                ESP_LOGI(TAG, "idf_timing_dir OUT: n=%" PRIu32
                              " avg=%" PRId32 "us"
                              " min=%" PRId32 "us max=%" PRId32 "us",
                         s_dir_count[0],
                         (int32_t)(s_dir_sum_us[0] / (int64_t)s_dir_count[0]),
                         (int32_t)s_dir_min_us[0],
                         (int32_t)s_dir_max_us[0]);
            }
            if (s_dir_count[1] > 0) {
                ESP_LOGI(TAG, "idf_timing_dir IN:  n=%" PRIu32
                              " avg=%" PRId32 "us"
                              " min=%" PRId32 "us max=%" PRId32 "us",
                         s_dir_count[1],
                         (int32_t)(s_dir_sum_us[1] / (int64_t)s_dir_count[1]),
                         (int32_t)s_dir_min_us[1],
                         (int32_t)s_dir_max_us[1]);
            }
        }
    }

    if (inflight->user_cb != NULL) {
        /* Async path: IDF holds sole ownership until this callback.
         * Extract the IN data, free the inflight/xfer, then call the
         * user callback so the responder can reuse the inflight_slot
         * before any further work runs. */
        int status = map_transfer_status_to_errno(xfer->status);
        size_t in_len = 0;
        if (status == 0 && inflight->is_in && inflight->in_data != NULL
                && inflight->in_capacity > 0) {
            if (inflight->is_control) {
                in_len = (xfer->actual_num_bytes > USB_SETUP_PACKET_SIZE)
                       ? (size_t)(xfer->actual_num_bytes - USB_SETUP_PACKET_SIZE)
                       : 0;
            } else {
                in_len = (size_t)xfer->actual_num_bytes;
                if (in_len > inflight->payload_len) {
                    in_len = inflight->payload_len;
                }
            }
            if (in_len > inflight->in_capacity) {
                in_len = inflight->in_capacity;
            }
            const uint8_t *src = inflight->is_control
                ? (xfer->data_buffer + USB_SETUP_PACKET_SIZE)
                : xfer->data_buffer;
            memcpy(inflight->in_data, src, in_len);
        }
        void (*cb)(void *, int, size_t) = inflight->user_cb;
        void *ctx = inflight->user_ctx;
        inflight_free(inflight);
        cb(ctx, status, in_len);
        return;
    }

    /* Synchronous path: give done_sem; the submit_xfer caller wakes up
     * and frees the inflight directly. Single-owner by construction:
     * the caller blocks on done_sem, so it cannot race this give. */
    if (inflight->done_sem != NULL) {
        xSemaphoreGive(inflight->done_sem);
    }
}

/* ------------------------------------------------------------------------ */
/* Tasks                                                                    */
/* ------------------------------------------------------------------------ */

static void usb_host_daemon_task(void *arg)
{
    (void)arg;
    while (true) {
        uint32_t flags = 0;
        esp_err_t err = usb_host_lib_handle_events(portMAX_DELAY, &flags);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "lib_handle_events: %s", esp_err_to_name(err));
            continue;
        }
        if (flags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS) {
            usb_host_device_free_all();
        }
    }
}

/* The worker is the sole owner of `usb_host_client_handle_events`.
 * It pumps in a tight loop with a small block, draining attach/detach
 * events from the IDF callback path and firing transfer-done callbacks
 * for any URBs that completed. Submit calls from other tasks queue
 * IDF-side and block on their own `done_sem`. */
static void usbhost_worker_task(void *arg)
{
    (void)arg;

    usb_host_client_config_t cfg = {
        .is_synchronous   = false,
        .max_num_event_msg = USBHOST_EVENT_QUEUE_LEN,
        .async = {
            .client_event_callback = usb_client_event_cb,
            .callback_arg          = NULL,
        },
    };
    esp_err_t err = usb_host_client_register(&cfg, &s_state.client_hdl);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "client_register: %s", esp_err_to_name(err));
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "USB host worker running (event-pump mode, %d device slots)",
             USBHOST_MAX_DEVICES);

    while (true) {
        err = usb_host_client_handle_events(s_state.client_hdl, pdMS_TO_TICKS(10));
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "client_handle_events: %s", esp_err_to_name(err));
        }
        drain_event_queue();
    }
}

/* ------------------------------------------------------------------------ */
/* Public API                                                               */
/* ------------------------------------------------------------------------ */

int usbhost_start(void)
{
    if (s_state.started) {
        return 0;
    }

    memset(&s_state, 0, sizeof(s_state));

    s_state.state_mutex = xSemaphoreCreateMutex();
    if (s_state.state_mutex == NULL) {
        return -ENOMEM;
    }
    s_state.event_queue = xQueueCreate(USBHOST_EVENT_QUEUE_LEN, sizeof(usbhost_event_t));
    if (s_state.event_queue == NULL) {
        return -ENOMEM;
    }

    usb_host_config_t hc = {
        .skip_phy_setup = false,
        .intr_flags     = 0,
    };
    esp_err_t err = usb_host_install(&hc);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "usb_host_install: %s", esp_err_to_name(err));
        return -EIO;
    }

    BaseType_t rc = xTaskCreatePinnedToCore(
        usb_host_daemon_task, "usb_host_daemon",
        USBHOST_DAEMON_TASK_STACK, NULL,
        USBHOST_DAEMON_TASK_PRIORITY, &s_state.daemon_hdl,
        USBHOST_TASK_CORE);
    if (rc != pdPASS) {
        return -ENOMEM;
    }
    rc = xTaskCreatePinnedToCore(
        usbhost_worker_task, "usbhost_worker",
        USBHOST_WORKER_TASK_STACK, NULL,
        USBHOST_WORKER_TASK_PRIORITY, &s_state.worker_hdl,
        USBHOST_TASK_CORE);
    if (rc != pdPASS) {
        return -ENOMEM;
    }
    s_state.started = true;
    return 0;
}

size_t usbhost_get_devices(usbip_dev_record_t *out, size_t max)
{
    if (out == NULL || max == 0) {
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
    if (busid == NULL || out == NULL) {
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

/* Caller holds state_mutex. Returns wMaxPacketSize for ep_addr; 64 if
 * not found. */
static uint16_t get_endpoint_mps_locked(int slot, uint8_t ep_addr)
{
    for (uint8_t i = 0; i < s_state.devices[slot].num_endpoints; i++) {
        if (s_state.devices[slot].endpoints[i].address == ep_addr) {
            uint16_t mps = s_state.devices[slot].endpoints[i].max_packet_size;
            return (mps != 0) ? mps : 64;
        }
    }
    return 64;
}

/* The submit core. Runs entirely in the caller's task. The IDF
 * transfer-completion callback fires inside the worker's
 * usb_host_client_handle_events loop (single-threaded as required by
 * the IDF) and gives our done_sem.
 *
 * The usbhost_submit_order_t parameter is removed in R20 step3.
 * Per-EP lane tasks serialise submit order by construction. */
static int submit_xfer(const char busid[USBIP_BUSID_SIZE],
                       uint8_t ep_addr,
                       bool is_control,
                       const usbip_setup_packet_t *setup,
                       const uint8_t *out_data, size_t out_len,
                       uint8_t *in_data, size_t in_capacity, size_t *in_len,
                       volatile bool *cancel,
                       uint32_t timeout_ms)
{
    if (busid == NULL || in_len == NULL) {
        return -EINVAL;
    }
    *in_len = 0;

    const bool is_in = is_control
        ? (setup != NULL && (setup->bmRequestType & USBIP_REQUEST_DIR_IN) != 0)
        : (ep_addr & 0x80) != 0;
    const size_t payload_len = is_in ? in_capacity : out_len;
    if (payload_len > USBHOST_MAX_TRANSFER) {
        return -EMSGSIZE;
    }

    /* Resolve dev_hdl + MPS-rounded transfer length. */
    usb_device_handle_t dev_hdl = NULL;
    size_t xfer_len = is_control
        ? (USB_SETUP_PACKET_SIZE + payload_len)
        : payload_len;

    SemaphoreHandle_t ep_submit_mutex = NULL;
    const uint8_t submit_ep = is_control ? 0x00 : ep_addr;

    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_busid_locked(busid);
    if (slot >= 0) {
        if (!is_control && is_in && in_capacity > 0) {
            uint16_t mps = get_endpoint_mps_locked(slot, ep_addr);
            if (mps > 0 && (xfer_len % mps) != 0) {
                xfer_len = ((xfer_len + mps - 1) / mps) * mps;
            }
        }
        dev_hdl = s_state.devices[slot].dev_hdl;
        ep_submit_mutex = get_ep_submit_mutex_locked(slot, submit_ep);
    }
    xSemaphoreGive(s_state.state_mutex);

    if (dev_hdl == NULL) {
        return -ENODEV;
    }

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbhost_submit: dev=%.32s ep=0x%02x dir=%s len=%u",
                 busid, ep_addr,
                 is_control ? "CTRL" : (is_in ? "IN" : "OUT"),
                 (unsigned)payload_len);
    }

    usb_transfer_t *xfer = NULL;
    esp_err_t err = usb_host_transfer_alloc(xfer_len > 0 ? xfer_len : 1, 0, &xfer);
    if (err != ESP_OK || xfer == NULL) {
        return -ENOMEM;
    }

    if (is_control) {
        memcpy(xfer->data_buffer, setup, USB_SETUP_PACKET_SIZE);
        if (!is_in && out_len > 0 && out_data != NULL) {
            memcpy(xfer->data_buffer + USB_SETUP_PACKET_SIZE, out_data, out_len);
        }
    } else if (!is_in && out_len > 0 && out_data != NULL) {
        memcpy(xfer->data_buffer, out_data, out_len);
    }

    /* Heap-allocated inflight: single-owner (caller blocks on done_sem
     * until the IDF callback fires; no ref-count needed). */
    usbhost_inflight_t *inflight = calloc(1, sizeof(*inflight));
    if (inflight == NULL) {
        usb_host_transfer_free(xfer);
        return -ENOMEM;
    }
    inflight->done_sem = xSemaphoreCreateBinary();
    inflight->xfer     = xfer;
    if (inflight->done_sem == NULL) {
        free(inflight);
        usb_host_transfer_free(xfer);
        return -ENOMEM;
    }

    xfer->callback         = transfer_done_cb;
    xfer->context          = inflight;
    xfer->device_handle    = dev_hdl;
    xfer->bEndpointAddress = is_control ? 0 : ep_addr;
    xfer->num_bytes        = (int)xfer_len;
    xfer->timeout_ms       = timeout_ms;

    /* Per-endpoint submit serialisation. The per-EP mutex serialises
     * the actual submit call so the IDF only sees one outstanding
     * submit at a time on this EP. With per-EP lane tasks (R20), submit
     * order matches arrival order by construction, so the former
     * ticket-based submit-order hook (usbhost_submit_order_t) is no
     * longer needed and has been removed in R20 step3. */
    if (ep_submit_mutex != NULL) {
        xSemaphoreTake(ep_submit_mutex, portMAX_DELAY);
    }
    if (is_control) {
        err = usb_host_transfer_submit_control(s_state.client_hdl, xfer);
    } else {
        err = usb_host_transfer_submit(xfer);
    }
    if (ep_submit_mutex != NULL) {
        xSemaphoreGive(ep_submit_mutex);
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "transfer_submit ep=0x%02x failed: %s (ctrl=%d)",
                 ep_addr, esp_err_to_name(err), (int)is_control);
        /* IDF rejected submit; it will not call our callback. Free
         * the inflight directly (no ref-count, single owner). */
        inflight_free(inflight);
        return (err == ESP_ERR_INVALID_STATE) ? -ENODEV : -EIO;
    }

    /* Wait for completion or cancel. The worker pumps client events
     * and drives the callback that gives done_sem. The cancel sequence
     * (halt+flush+clear) is held under the per-EP submit mutex so it
     * cannot race a parallel submit on the same EP; the IDF's EP
     * command machine rejects nested commands with INVALID_STATE
     * (iter4 evidence). */
    bool cancelled       = false;
    const uint8_t halt_ep = is_control ? 0x00 : ep_addr;
    while (xSemaphoreTake(inflight->done_sem, pdMS_TO_TICKS(50)) != pdTRUE) {
        if (!cancelled && cancel != NULL && *cancel) {
            cancelled = true;
            if (ep_submit_mutex != NULL) {
                xSemaphoreTake(ep_submit_mutex, portMAX_DELAY);
            }
            esp_err_t herr = usb_host_endpoint_halt(dev_hdl, halt_ep);
            if (herr != ESP_OK && herr != ESP_ERR_INVALID_STATE) {
                ESP_LOGW(TAG, "endpoint_halt(0x%02x) failed: %s",
                         halt_ep, esp_err_to_name(herr));
            }
            herr = usb_host_endpoint_flush(dev_hdl, halt_ep);
            if (herr != ESP_OK && herr != ESP_ERR_INVALID_STATE) {
                ESP_LOGW(TAG, "endpoint_flush(0x%02x) failed: %s",
                         halt_ep, esp_err_to_name(herr));
            }
            herr = usb_host_endpoint_clear(dev_hdl, halt_ep);
            if (herr != ESP_OK && herr != ESP_ERR_INVALID_STATE) {
                ESP_LOGW(TAG, "endpoint_clear(0x%02x) failed: %s",
                         halt_ep, esp_err_to_name(herr));
            }
            if (ep_submit_mutex != NULL) {
                xSemaphoreGive(ep_submit_mutex);
            }
        }
    }

    int status = map_transfer_status_to_errno(xfer->status);
    if (status == 0 && is_in && in_data != NULL && in_capacity > 0) {
        size_t bytes;
        if (is_control) {
            bytes = (xfer->actual_num_bytes > USB_SETUP_PACKET_SIZE)
                  ? (size_t)(xfer->actual_num_bytes - USB_SETUP_PACKET_SIZE)
                  : 0;
        } else {
            bytes = (size_t)xfer->actual_num_bytes;
            if (bytes > payload_len) {
                bytes = payload_len;
            }
        }
        if (bytes > in_capacity) {
            bytes = in_capacity;
        }
        const uint8_t *src = is_control
            ? (xfer->data_buffer + USB_SETUP_PACKET_SIZE)
            : xfer->data_buffer;
        memcpy(in_data, src, bytes);
        *in_len = bytes;
    }

    /* The IDF callback has fired and signalled done_sem. Caller now
     * owns the inflight exclusively; free it directly. */
    inflight_free(inflight);

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbhost_complete: dev=%.32s ep=0x%02x status=%d actual=%u",
                 busid, ep_addr, status, (unsigned)*in_len);
    }
    return status;
}

/* Non-blocking submit. The IDF transfer-completion callback (transfer_done_cb)
 * invokes cb(ctx, status, in_len) when the transfer completes. Returns 0 if
 * the IDF accepted the submit, negative errno if it rejected it (in which case
 * the callback will NOT be called). ep_addr high bit = direction (0x8N = IN).
 *
 * IMPORTANT: cb runs in the IDF worker context (priority 9). It must be short.
 * Route all work through a higher-priority responder queue; do NOT do TCP sends
 * or heavy bookkeeping inside cb. */
int usbhost_submit_async(const char busid[USBIP_BUSID_SIZE],
                         uint8_t ep_addr, bool is_control,
                         const usbip_setup_packet_t *setup,
                         const uint8_t *out_data, size_t out_len,
                         uint8_t *in_data, size_t in_capacity,
                         void (*cb)(void *ctx, int status, size_t in_len),
                         void *ctx)
{
    if (busid == NULL || cb == NULL) {
        return -EINVAL;
    }

    const bool is_in = is_control
        ? (setup != NULL && (setup->bmRequestType & USBIP_REQUEST_DIR_IN) != 0)
        : (ep_addr & 0x80) != 0;
    const size_t payload_len = is_in ? in_capacity : out_len;
    if (payload_len > USBHOST_MAX_TRANSFER) {
        return -EMSGSIZE;
    }

    usb_device_handle_t dev_hdl = NULL;
    size_t xfer_len = is_control
        ? (USB_SETUP_PACKET_SIZE + payload_len)
        : payload_len;

    SemaphoreHandle_t ep_submit_mutex = NULL;
    const uint8_t submit_ep = is_control ? 0x00 : ep_addr;

    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_busid_locked(busid);
    if (slot >= 0) {
        if (!is_control && is_in && in_capacity > 0) {
            uint16_t mps = get_endpoint_mps_locked(slot, ep_addr);
            if (mps > 0 && (xfer_len % mps) != 0) {
                xfer_len = ((xfer_len + mps - 1) / mps) * mps;
            }
        }
        dev_hdl = s_state.devices[slot].dev_hdl;
        ep_submit_mutex = get_ep_submit_mutex_locked(slot, submit_ep);
    }
    xSemaphoreGive(s_state.state_mutex);

    if (dev_hdl == NULL) {
        return -ENODEV;
    }

    usb_transfer_t *xfer = NULL;
    esp_err_t err = usb_host_transfer_alloc(xfer_len > 0 ? xfer_len : 1, 0, &xfer);
    if (err != ESP_OK || xfer == NULL) {
        return -ENOMEM;
    }

    if (is_control) {
        memcpy(xfer->data_buffer, setup, USB_SETUP_PACKET_SIZE);
        if (!is_in && out_len > 0 && out_data != NULL) {
            memcpy(xfer->data_buffer + USB_SETUP_PACKET_SIZE, out_data, out_len);
        }
    } else if (!is_in && out_len > 0 && out_data != NULL) {
        memcpy(xfer->data_buffer, out_data, out_len);
    }

    /* Async inflight: IDF owns it from submit until transfer_done_cb fires.
     * No done_sem (caller does not block). Single-owner; no ref-count. */
    usbhost_inflight_t *inflight = calloc(1, sizeof(*inflight));
    if (inflight == NULL) {
        usb_host_transfer_free(xfer);
        return -ENOMEM;
    }
    inflight->xfer        = xfer;
    inflight->user_cb     = cb;
    inflight->user_ctx    = ctx;
    inflight->is_in       = is_in;
    inflight->is_control  = is_control;
    inflight->payload_len = payload_len;
    inflight->in_data     = in_data;
    inflight->in_capacity = in_capacity;
    inflight->ep_for_log  = ep_addr;

    xfer->callback         = transfer_done_cb;
    xfer->context          = inflight;
    xfer->device_handle    = dev_hdl;
    xfer->bEndpointAddress = is_control ? 0 : ep_addr;
    xfer->num_bytes        = (int)xfer_len;
    xfer->timeout_ms       = is_control ? USBHOST_CONTROL_TIMEOUT_MS : 0;

    /* Per-endpoint submit serialisation: same as submit_xfer. */
    if (ep_submit_mutex != NULL) {
        xSemaphoreTake(ep_submit_mutex, portMAX_DELAY);
    }
    inflight->t_submit_pre = esp_timer_get_time();
    if (is_control) {
        err = usb_host_transfer_submit_control(s_state.client_hdl, xfer);
    } else {
        err = usb_host_transfer_submit(xfer);
    }
    inflight->t_submit_post = esp_timer_get_time();
    if (ep_submit_mutex != NULL) {
        xSemaphoreGive(ep_submit_mutex);
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "submit_async ep=0x%02x failed: %s", ep_addr, esp_err_to_name(err));
        /* IDF rejected; callback will never fire. Free directly. */
        inflight_free(inflight);
        return (err == ESP_ERR_INVALID_STATE) ? -ENODEV : -EIO;
    }
    return 0;
}

/* Synchronous halt+flush+clear on ep_addr. Called from the UNLINK handler
 * under the per-EP submit mutex to force any in-flight URB on this pipe to
 * complete with cancelled status. The IDF then calls our callback with
 * USB_TRANSFER_STATUS_CANCELED. */
void usbhost_cancel_ep(const char busid[USBIP_BUSID_SIZE], uint8_t ep_addr)
{
    if (busid == NULL) {
        return;
    }
    usb_device_handle_t dev_hdl = NULL;
    SemaphoreHandle_t ep_submit_mutex = NULL;

    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_busid_locked(busid);
    if (slot >= 0) {
        dev_hdl = s_state.devices[slot].dev_hdl;
        ep_submit_mutex = get_ep_submit_mutex_locked(slot, ep_addr);
    }
    xSemaphoreGive(s_state.state_mutex);

    if (dev_hdl == NULL) {
        return;
    }
    /* Hold per-EP submit mutex around halt/flush/clear to avoid racing
     * a concurrent submit on the same EP (the IDF EP-command machine
     * rejects nested commands with INVALID_STATE; see iter4 evidence). */
    if (ep_submit_mutex != NULL) {
        xSemaphoreTake(ep_submit_mutex, portMAX_DELAY);
    }
    esp_err_t err;
    err = usb_host_endpoint_halt(dev_hdl, ep_addr);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "cancel_ep: endpoint_halt(0x%02x) failed: %s",
                 ep_addr, esp_err_to_name(err));
    }
    err = usb_host_endpoint_flush(dev_hdl, ep_addr);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "cancel_ep: endpoint_flush(0x%02x) failed: %s",
                 ep_addr, esp_err_to_name(err));
    }
    err = usb_host_endpoint_clear(dev_hdl, ep_addr);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "cancel_ep: endpoint_clear(0x%02x) failed: %s",
                 ep_addr, esp_err_to_name(err));
    }
    if (ep_submit_mutex != NULL) {
        xSemaphoreGive(ep_submit_mutex);
    }
}

int usbhost_control_transfer(const char busid[USBIP_BUSID_SIZE],
                             const usbip_setup_packet_t *setup,
                             const uint8_t *out_data, size_t out_len,
                             uint8_t *in_data, size_t in_capacity, size_t *in_len,
                             volatile bool *cancel)
{
    if (setup == NULL) {
        return -EINVAL;
    }
    return submit_xfer(busid, 0, true, setup,
                       out_data, out_len, in_data, in_capacity, in_len,
                       cancel, USBHOST_CONTROL_TIMEOUT_MS);
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
    /* No timeout for bulk; OUT completes when ACK lands, IN may pend
     * indefinitely on a starved device until cancellation. */
    return submit_xfer(busid, ep_addr, false, NULL,
                       out_data, out_len, in_data, in_capacity, in_len,
                       cancel, 0);
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
    /* No timeout for interrupt IN; the cdc-acm interrupt-IN URB may
     * legitimately pend for seconds waiting for a notification. */
    return submit_xfer(busid, ep_addr, false, NULL,
                       out_data, out_len, in_data, in_capacity, in_len,
                       cancel, 0);
}

bool usbhost_is_interrupt_endpoint(const char busid[USBIP_BUSID_SIZE],
                                   uint8_t ep_num, uint8_t direction)
{
    if (busid == NULL) {
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
