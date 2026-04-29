/* Annealage Pod: USB host (WS-B) backend.
 *
 * Replaces the WS-A-installed -ENOSYS stub at usbhost_stub.c with a
 * real backend feeding the multiplexer at src/c_modules/usbip/.
 * Implements the API contract in usbhost.h against the ESP-IDF
 * `usb_host` component (`components/usb/include/usb/usb_host.h`).
 *
 * Stack-selection rationale and design documentation:
 * docs/design/usbhost.md.
 *
 * Concurrency: two FreeRTOS tasks pinned to APP_CPU. `usb_host_daemon`
 * pumps `usb_host_lib_handle_events` (the library-wide event pump,
 * required by the IDF API). `usbhost_worker` registers the USB host
 * client and serialises pre-filled per-pipe request slots through
 * `usb_host_client_handle_events`. Caller threads block on a per-slot
 * binary semaphore.
 *
 * Vendoring: the slot/pipe topology is shape-equivalent to
 * `referencea/esp-usbip-bridge/main/usb_backend.c`; no upstream
 * licence is shipped with the reference repo so the implementation
 * is a clean rewrite. The wire-protocol structures consumed here
 * (usbip_dev_record_t, usbip_setup_packet_t) come from the
 * mpy-pod usbip module headers, not the reference.
 */

#include "usbhost.h"

#include <errno.h>
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

#include "usb/usb_host.h"
#include "usb/usb_helpers.h"
#include "usb/usb_types_ch9.h"
#include "usb/usb_types_stack.h"

/* Compile-time defaults; override at link time with -DUSBHOST_*=N. */
#ifndef USBHOST_NUM_PIPES
#define USBHOST_NUM_PIPES 16
#endif
#ifndef USBHOST_MAX_DEVICES
#define USBHOST_MAX_DEVICES 4
#endif
#ifndef USBHOST_MAX_TRANSFER
#define USBHOST_MAX_TRANSFER (64 * 1024)
#endif
#ifndef USBHOST_MAX_ENDPOINTS
#define USBHOST_MAX_ENDPOINTS 16
#endif
#ifndef USBHOST_TRANSFER_TIMEOUT_MS
#define USBHOST_TRANSFER_TIMEOUT_MS 5000
#endif
#ifndef USBHOST_EVENT_QUEUE_LEN
#define USBHOST_EVENT_QUEUE_LEN 16
#endif

#ifndef USBHOST_TASK_CORE
#define USBHOST_TASK_CORE 1   /* APP_CPU per spec.md §2 / architecture.md §3 */
#endif
#ifndef USBHOST_DAEMON_TASK_PRIORITY
#define USBHOST_DAEMON_TASK_PRIORITY 10
#endif
#ifndef USBHOST_WORKER_TASK_PRIORITY
#define USBHOST_WORKER_TASK_PRIORITY 9
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
 * main loop. Decouples the callback from the slow-path bookkeeping. */
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

/* Per-pipe request slot. One slot per USB host pipe. The caller fills
 * a pre-assigned slot (allocated at device-attach time), sets active,
 * notifies the worker, and waits on done_sem. */
typedef struct {
    bool                 assigned;       /* reserved for a (device,ep) */
    volatile bool        active;         /* transfer is pending */
    char                 busid[USBIP_BUSID_SIZE];
    uint8_t              endpoint_addr;  /* 0 for control, 0x80|N for IN, N for OUT */
    usbip_setup_packet_t setup;          /* used iff endpoint_addr == 0 */
    const uint8_t       *out_data;
    size_t               out_len;
    uint8_t             *in_data;
    size_t               in_capacity;
    size_t              *in_len_out;
    int                 *status_out;
    volatile bool       *cancel;
    SemaphoreHandle_t    done_sem;       /* pre-allocated, never freed */
} usbhost_pipe_req_t;

typedef struct {
    bool          done;
    volatile bool *cancel;
} transfer_done_ctx_t;

/* Cached endpoint description, populated from the active config
 * descriptor at device-attach time. */
typedef struct {
    uint8_t  address;
    uint8_t  attributes;
    uint16_t max_packet_size;
    uint8_t  interval;
} usbhost_ep_t;

typedef struct {
    bool                 in_use;
    bool                 interfaces_claimed;
    usb_device_handle_t  dev_hdl;
    usbip_dev_record_t   device;            /* mirrored to multiplexer */
    int                  ep0_pipe;          /* pipe index for EP0, -1 if none */
    int                  ep_pipes[USBHOST_MAX_ENDPOINTS];
    uint8_t              num_endpoints;
    usbhost_ep_t         endpoints[USBHOST_MAX_ENDPOINTS];
} usbhost_slot_t;

typedef struct {
    bool                       started;
    SemaphoreHandle_t          state_mutex;
    QueueHandle_t              event_queue;
    TaskHandle_t               worker_hdl;
    TaskHandle_t               daemon_hdl;
    usb_host_client_handle_t   client_hdl;
    usbhost_pipe_req_t         pipes[USBHOST_NUM_PIPES];
    usbhost_slot_t             devices[USBHOST_MAX_DEVICES];
} usbhost_state_t;

static usbhost_state_t s_state;

/* Per-URB observability flag, toggled at runtime via
 * usbhost_set_verbose(). Default off. */
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

/* Walk the active configuration descriptor; populate the device's
 * num_interfaces / interfaces[] and the caller's endpoint cache from
 * the USB 2.0 chapter 9 fields. The caller holds state_mutex. */
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
                /* USB 2.0 §9.6.5: bInterfaceClass at offset 5,
                 * bInterfaceSubClass at 6, bInterfaceProtocol at 7. */
                desc->interfaces[intf_count].interface_class    = raw[off + 5];
                desc->interfaces[intf_count].interface_subclass = raw[off + 6];
                desc->interfaces[intf_count].interface_protocol = raw[off + 7];
                intf_count++;
            }
        } else if (dtype == USB_B_DESCRIPTOR_TYPE_ENDPOINT && dlen >= 7) {
            if (ep_count < USBHOST_MAX_ENDPOINTS) {
                /* USB 2.0 §9.6.6: bEndpointAddress at offset 2,
                 * bmAttributes at 3, wMaxPacketSize at 4 (LE 16-bit),
                 * bInterval at 6. */
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

static int alloc_pipe_locked(void)
{
    for (int i = 0; i < USBHOST_NUM_PIPES; i++) {
        if (!s_state.pipes[i].assigned) {
            s_state.pipes[i].assigned = true;
            s_state.pipes[i].active   = false;
            return i;
        }
    }
    return -1;
}

static void free_pipe_locked(int pipe)
{
    if (pipe >= 0 && pipe < USBHOST_NUM_PIPES) {
        s_state.pipes[pipe].assigned = false;
        s_state.pipes[pipe].active   = false;
    }
}

static void clear_slot_locked(int slot)
{
    if (slot < 0 || slot >= USBHOST_MAX_DEVICES) {
        return;
    }
    free_pipe_locked(s_state.devices[slot].ep0_pipe);
    for (int i = 0; i < USBHOST_MAX_ENDPOINTS; i++) {
        free_pipe_locked(s_state.devices[slot].ep_pipes[i]);
    }
    memset(&s_state.devices[slot], 0, sizeof(s_state.devices[slot]));
    s_state.devices[slot].ep0_pipe = -1;
    for (int i = 0; i < USBHOST_MAX_ENDPOINTS; i++) {
        s_state.devices[slot].ep_pipes[i] = -1;
    }
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
    for (int i = 0; i < USBHOST_MAX_ENDPOINTS; i++) {
        free_pipe_locked(s_state.devices[slot].ep_pipes[i]);
        s_state.devices[slot].ep_pipes[i] = -1;
    }
    s_state.devices[slot].interfaces_claimed = false;
}

/* Composite-device support: claim every interface in the active
 * configuration. CDC + MSC simultaneous use (typical MicroPython DUT
 * shape per spec.md §4.5) needs both interfaces claimed concurrently,
 * which is the IDF default behaviour for a single client when the
 * interfaces don't overlap on endpoints. */
static esp_err_t ensure_interfaces_claimed_locked(int slot)
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
    /* One pipe slot per (cached) endpoint. */
    for (uint8_t i = 0; i < s_state.devices[slot].num_endpoints; i++) {
        int pipe = alloc_pipe_locked();
        if (pipe < 0) {
            ESP_LOGE(TAG, "no free pipe slots for ep 0x%02x on %s",
                     s_state.devices[slot].endpoints[i].address, d->busid);
            return ESP_ERR_NO_MEM;
        }
        s_state.devices[slot].ep_pipes[i] = pipe;
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
    desc.busnum              = 1;                            /* real DUT bus */
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

    /* Busid namespace: real DUT lives on bus 1 per
     * research/usbip-multiplexing-design.md §1.1.2. */
    snprintf(desc.busid, sizeof(desc.busid), "1-%u", dev_info.dev_addr);
    snprintf(desc.path, sizeof(desc.path), "/esp-usb-host/1-%u", dev_info.dev_addr);

    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);

    /* If the device address re-used an existing slot, close it first. */
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

    /* EP0 always gets a pipe slot. */
    s_state.devices[slot].ep0_pipe = alloc_pipe_locked();
    if (s_state.devices[slot].ep0_pipe < 0) {
        ESP_LOGW(TAG, "no pipe slot for EP0 on %s", desc.busid);
    }
    for (int i = 0; i < USBHOST_MAX_ENDPOINTS; i++) {
        s_state.devices[slot].ep_pipes[i] = -1;
    }
    /* Non-zero endpoint pipes are allocated lazily in
     * ensure_interfaces_claimed_locked. */

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
/* URB submission (called on the worker task)                               */
/* ------------------------------------------------------------------------ */

static void transfer_done_cb(usb_transfer_t *xfer)
{
    transfer_done_ctx_t *ctx = (transfer_done_ctx_t *)xfer->context;
    ctx->done = true;
}

/* Caller holds state_mutex. Returns the wMaxPacketSize for `ep_addr`
 * on the slot's cached endpoint table, defaulting to 64 if not found. */
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

static int process_pipe_request(usbhost_pipe_req_t *req)
{
    if (req->in_len_out != NULL) {
        *req->in_len_out = 0;
    }

    const bool is_control = (req->endpoint_addr == 0);
    const bool is_in = is_control
        ? (req->setup.bmRequestType & USBIP_REQUEST_DIR_IN) != 0
        : (req->endpoint_addr & 0x80) != 0;

    /* Resolve dev_hdl + ensure interfaces are claimed under the mutex. */
    usb_device_handle_t dev_hdl = NULL;
    size_t xfer_len = is_control
        ? (USB_SETUP_PACKET_SIZE + (is_in ? req->in_capacity : req->out_len))
        : (is_in ? req->in_capacity : req->out_len);

    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_busid_locked(req->busid);
    if (slot >= 0) {
        if (!is_control) {
            esp_err_t cerr = ensure_interfaces_claimed_locked(slot);
            if (cerr != ESP_OK) {
                xSemaphoreGive(s_state.state_mutex);
                return -EIO;
            }
            /* Round IN bulk/interrupt up to MPS to satisfy the IDF
             * transfer constraint that data length be a multiple of
             * wMaxPacketSize. */
            if (is_in && req->in_capacity > 0) {
                uint16_t mps = get_endpoint_mps_locked(slot, req->endpoint_addr);
                if (mps > 0 && (xfer_len % mps) != 0) {
                    xfer_len = ((xfer_len + mps - 1) / mps) * mps;
                }
            }
        }
        dev_hdl = s_state.devices[slot].dev_hdl;
    }
    xSemaphoreGive(s_state.state_mutex);

    if (dev_hdl == NULL) {
        return -ENODEV;
    }
    const size_t payload_len = is_in ? req->in_capacity : req->out_len;
    if (payload_len > USBHOST_MAX_TRANSFER) {
        return -EMSGSIZE;
    }

    usb_transfer_t *xfer = NULL;
    esp_err_t err = usb_host_transfer_alloc(xfer_len > 0 ? xfer_len : 1, 0, &xfer);
    if (err != ESP_OK || xfer == NULL) {
        return -ENOMEM;
    }

    if (is_control) {
        /* USB chapter 9: setup packet (8 bytes) followed by data stage. */
        memcpy(xfer->data_buffer, &req->setup, USB_SETUP_PACKET_SIZE);
        if (!is_in && req->out_len > 0 && req->out_data != NULL) {
            memcpy(xfer->data_buffer + USB_SETUP_PACKET_SIZE,
                   req->out_data, req->out_len);
        }
    } else if (!is_in && req->out_len > 0 && req->out_data != NULL) {
        memcpy(xfer->data_buffer, req->out_data, req->out_len);
    }

    transfer_done_ctx_t done_ctx = {.done = false, .cancel = req->cancel};
    xfer->callback         = transfer_done_cb;
    xfer->context          = &done_ctx;
    xfer->device_handle    = dev_hdl;
    xfer->bEndpointAddress = req->endpoint_addr;
    xfer->num_bytes        = (int)xfer_len;
    xfer->timeout_ms       = USBHOST_TRANSFER_TIMEOUT_MS;

    if (is_control) {
        err = usb_host_transfer_submit_control(s_state.client_hdl, xfer);
    } else {
        err = usb_host_transfer_submit(xfer);
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "transfer_submit ep=0x%02x failed: %s",
                 req->endpoint_addr, esp_err_to_name(err));
        usb_host_transfer_free(xfer);
        return (err == ESP_ERR_INVALID_STATE) ? -ENODEV : -EIO;
    }

    /* Drive client events until the transfer's callback fires or the
     * caller flips *cancel. The IDF API requires a single thread of
     * control through usb_host_client_handle_events; the worker task
     * is the only caller, so this nested call is safe. */
    bool cancelled = false;
    while (!done_ctx.done) {
        if (!cancelled && req->cancel != NULL && *req->cancel) {
            cancelled = true;
        }
        err = usb_host_client_handle_events(s_state.client_hdl, pdMS_TO_TICKS(10));
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "client_handle_events err=%s ep=0x%02x done=%d",
                     esp_err_to_name(err), req->endpoint_addr, done_ctx.done);
            break;
        }
    }
    if (cancelled) {
        usb_host_transfer_free(xfer);
        return -ECONNRESET;
    }

    int status = map_transfer_status_to_errno(xfer->status);
    if (status == 0 && is_in && req->in_data != NULL && req->in_capacity > 0) {
        size_t bytes;
        if (is_control) {
            bytes = (xfer->actual_num_bytes > USB_SETUP_PACKET_SIZE)
                  ? (size_t)(xfer->actual_num_bytes - USB_SETUP_PACKET_SIZE)
                  : 0;
        } else {
            bytes = (size_t)xfer->actual_num_bytes;
            if (bytes > payload_len) {
                bytes = payload_len;     /* IN was MPS-rounded; trim. */
            }
        }
        if (bytes > req->in_capacity) {
            bytes = req->in_capacity;
        }
        const uint8_t *src = is_control
            ? (xfer->data_buffer + USB_SETUP_PACKET_SIZE)
            : xfer->data_buffer;
        memcpy(req->in_data, src, bytes);
        if (req->in_len_out != NULL) {
            *req->in_len_out = bytes;
        }
    }
    usb_host_transfer_free(xfer);
    return status;
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
    ESP_LOGI(TAG, "USB host worker running (%d pipe slots, %d device slots)",
             USBHOST_NUM_PIPES, USBHOST_MAX_DEVICES);

    while (true) {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(10));

        err = usb_host_client_handle_events(s_state.client_hdl, 0);
        if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "client_handle_events: %s", esp_err_to_name(err));
        }
        drain_event_queue();

        for (int i = 0; i < USBHOST_NUM_PIPES; i++) {
            usbhost_pipe_req_t *p = &s_state.pipes[i];
            if (!p->active) {
                continue;
            }
            int status = process_pipe_request(p);
            if (p->status_out != NULL) {
                *p->status_out = status;
            }
            p->active = false;
            xSemaphoreGive(p->done_sem);
        }
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
    for (int i = 0; i < USBHOST_MAX_DEVICES; i++) {
        s_state.devices[i].ep0_pipe = -1;
        for (int j = 0; j < USBHOST_MAX_ENDPOINTS; j++) {
            s_state.devices[i].ep_pipes[j] = -1;
        }
    }

    s_state.state_mutex = xSemaphoreCreateMutex();
    if (s_state.state_mutex == NULL) {
        return -ENOMEM;
    }
    for (int i = 0; i < USBHOST_NUM_PIPES; i++) {
        s_state.pipes[i].done_sem = xSemaphoreCreateBinary();
        if (s_state.pipes[i].done_sem == NULL) {
            return -ENOMEM;
        }
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

/* Internal: pre-fill a pipe req slot, notify the worker, wait. */
static int submit_pipe(const char busid[USBIP_BUSID_SIZE],
                       uint8_t ep_addr,
                       const usbip_setup_packet_t *setup,
                       const uint8_t *out_data, size_t out_len,
                       uint8_t *in_data, size_t in_capacity, size_t *in_len,
                       volatile bool *cancel)
{
    if (busid == NULL || in_len == NULL) {
        return -EINVAL;
    }
    *in_len = 0;

    /* Look up the pre-allocated pipe slot for (device, endpoint). For
     * a non-zero endpoint that hasn't had its interface claimed yet
     * we use the EP0 slot temporarily; the worker claims interfaces
     * before the actual IDF transfer. */
    int pipe_idx = -1;
    xSemaphoreTake(s_state.state_mutex, portMAX_DELAY);
    int slot = find_slot_by_busid_locked(busid);
    if (slot >= 0) {
        if (ep_addr == 0) {
            pipe_idx = s_state.devices[slot].ep0_pipe;
        } else {
            for (uint8_t i = 0; i < s_state.devices[slot].num_endpoints; i++) {
                if (s_state.devices[slot].endpoints[i].address == ep_addr) {
                    pipe_idx = s_state.devices[slot].ep_pipes[i];
                    break;
                }
            }
            if (pipe_idx < 0) {
                pipe_idx = s_state.devices[slot].ep0_pipe;
            }
        }
    }
    xSemaphoreGive(s_state.state_mutex);

    if (slot < 0) {
        return -ENODEV;
    }
    if (pipe_idx < 0) {
        ESP_LOGW(TAG, "no pipe slot for ep=0x%02x on %.32s", ep_addr, busid);
        return -ENODEV;
    }

    usbhost_pipe_req_t *p = &s_state.pipes[pipe_idx];
    int status = -EIO;

    memcpy(p->busid, busid, sizeof(p->busid));
    p->endpoint_addr = ep_addr;
    if (setup != NULL) {
        p->setup = *setup;
    }
    p->out_data    = out_data;
    p->out_len     = out_len;
    p->in_data     = in_data;
    p->in_capacity = in_capacity;
    p->in_len_out  = in_len;
    p->status_out  = &status;
    p->cancel      = cancel;

    if (s_urb_verbose) {
        const bool is_in = (ep_addr & 0x80) != 0;
        const size_t len = is_in ? in_capacity : out_len;
        ESP_LOGI(TAG, "usbhost_submit: dev=%.32s ep=0x%02x dir=%s len=%zu",
                 busid, ep_addr,
                 (ep_addr == 0) ? "CTRL" : (is_in ? "IN" : "OUT"), len);
    }

    p->active = true;
    if (s_state.worker_hdl != NULL) {
        xTaskNotifyGive(s_state.worker_hdl);
    }
    /* The done_sem is pre-allocated and never freed under us. */
    xSemaphoreTake(p->done_sem, portMAX_DELAY);

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbhost_complete: dev=%.32s ep=0x%02x status=%d actual=%zu",
                 busid, ep_addr, status, *in_len);
    }
    return status;
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
    return submit_pipe(busid, 0, setup,
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
    return submit_pipe(busid, ep_addr, NULL,
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
    return submit_pipe(busid, ep_addr, NULL,
                       out_data, out_len, in_data, in_capacity, in_len, cancel);
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
                /* USB 2.0 §9.6.6: bmAttributes bits 0..1 == 0x03 -> Interrupt. */
                is_intr = (s_state.devices[slot].endpoints[i].attributes & 0x03) == 0x03;
                break;
            }
        }
    }
    xSemaphoreGive(s_state.state_mutex);
    return is_intr;
}
