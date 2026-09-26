/* Annealage Pod: hub_device firmware notification beacon.
 *
 * Vendor-class device with a single interrupt IN endpoint (EP1, 2-byte
 * wMaxPacketSize, bInterval=12) on bus 2. EP1 IN delivers [state, 0]
 * for each pending edge in FIFO order; when there are no pending edges
 * the handler blocks for up to ~1 s on a binary semaphore that
 * hub_device_notify gives, then returns ZLP on timeout. The 1 s cap
 * keeps UNLINK responsive on the same per-connection read loop.
 *
 * Used by a host daemon to learn when the DUT (forwarded via usbhost)
 * connects or disconnects.
 *
 * Reference layout adapted from synthetic_device.c (CMSIS-DAP-v2).
 *
 * SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Annealage-firmware-exception
 */

#include "hub_device.h"

#include "../usbip/virtual_device.h"
#include "../usbip/usbip_server.h"
#include "../usbip/usb_string_desc.h"

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#ifndef MPY_POD_HOST_TEST_BUILD
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_log.h"
static const char *TAG = "hub_device";
#define HUB_LOGI(fmt, ...) ESP_LOGI(TAG, fmt, ##__VA_ARGS__)
#define HUB_LOGE(fmt, ...) ESP_LOGE(TAG, fmt, ##__VA_ARGS__)
#else
#define HUB_LOGI(fmt, ...) do { } while (0)
#define HUB_LOGE(fmt, ...) do { } while (0)
#endif

/* EP1 IN poll wait: bounded so UNLINK on the same per-connection read
 * loop is processed within ~1 s of cancellation. Well above any
 * reasonable bInterval. */
#define HUB_EP1_WAIT_MS 1000u

/* Pending-edge ring. Holds the FIFO of unconsumed connect/disconnect
 * edges; entries are 1 (connect) or 0 (disconnect). On overflow the
 * oldest edge is dropped, which is fine: stale edges supersede each
 * other when the daemon is not draining. */
#define HUB_EVENT_RING_SIZE 4u

/* --- USB constants ---------------------------------------------------- */

#define USB_REQ_GET_STATUS        0x00u
#define USB_REQ_CLEAR_FEATURE     0x01u
#define USB_REQ_SET_FEATURE       0x03u
#define USB_REQ_SET_ADDRESS       0x05u
#define USB_REQ_GET_DESCRIPTOR    0x06u
#define USB_REQ_GET_CONFIGURATION 0x08u
#define USB_REQ_SET_CONFIGURATION 0x09u
#define USB_REQ_GET_INTERFACE     0x0Au
#define USB_REQ_SET_INTERFACE     0x0Bu

#define USB_DESC_DEVICE           0x01u
#define USB_DESC_CONFIGURATION    0x02u
#define USB_DESC_STRING           0x03u
#define USB_DESC_INTERFACE        0x04u
#define USB_DESC_ENDPOINT         0x05u

#define USB_REQ_TYPE_STANDARD     0x00u
#define USB_REQ_TYPE_MASK         0x60u

#define HUB_VID                   0xC251u
#define HUB_PID                   0xF00Cu
#define HUB_EP_IN                 0x81u

/* --- Descriptors ------------------------------------------------------ */

#define HUB_DEVICE_DESC_LEN 18u
#define HUB_CONFIG_DESC_LEN (9u + 9u + 7u)

static const uint8_t s_device_desc[HUB_DEVICE_DESC_LEN] = {
    0x12,                         /* bLength */
    USB_DESC_DEVICE,
    0x00, 0x02,                   /* bcdUSB = 2.00 */
    0xFF,                         /* bDeviceClass: vendor */
    0x00,                         /* bDeviceSubClass */
    0x00,                         /* bDeviceProtocol */
    0x40,                         /* bMaxPacketSize0 = 64 */
    (uint8_t)(HUB_VID & 0xFFu),
    (uint8_t)(HUB_VID >> 8),
    (uint8_t)(HUB_PID & 0xFFu),
    (uint8_t)(HUB_PID >> 8),
    0x00, 0x01,                   /* bcdDevice = 1.00 */
    0x01,                         /* iManufacturer */
    0x02,                         /* iProduct */
    0x03,                         /* iSerialNumber */
    0x01,                         /* bNumConfigurations */
};

static const uint8_t s_config_desc[HUB_CONFIG_DESC_LEN] = {
    /* Configuration header */
    0x09, USB_DESC_CONFIGURATION,
    HUB_CONFIG_DESC_LEN, 0x00,    /* wTotalLength */
    0x01,                         /* bNumInterfaces */
    0x01,                         /* bConfigurationValue */
    0x00,                         /* iConfiguration */
    0xC0,                         /* bmAttributes: self-powered */
    50,                           /* bMaxPower = 100 mA */

    /* Interface 0 (vendor-specific, 1 endpoint) */
    0x09, USB_DESC_INTERFACE,
    0x00,                         /* bInterfaceNumber */
    0x00,                         /* bAlternateSetting */
    0x01,                         /* bNumEndpoints */
    0xFF,                         /* bInterfaceClass: vendor */
    0x00,                         /* bInterfaceSubClass */
    0x00,                         /* bInterfaceProtocol */
    0x00,                         /* iInterface */

    /* EP1 IN, interrupt, wMaxPacketSize=2, bInterval=12 */
    0x07, USB_DESC_ENDPOINT,
    HUB_EP_IN,
    0x03,                         /* bmAttributes: interrupt */
    0x02, 0x00,                   /* wMaxPacketSize = 2 */
    12,                           /* bInterval */
};

#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
_Static_assert(sizeof(s_device_desc) == HUB_DEVICE_DESC_LEN,
               "hub_device descriptor size mismatch");
_Static_assert(sizeof(s_config_desc) == HUB_CONFIG_DESC_LEN,
               "hub_device config descriptor size mismatch");
#endif

/* --- String descriptors ---------------------------------------------- */

#define HUB_STR_BUF 64

static uint8_t s_str0[] = { 0x04, USB_DESC_STRING, 0x09, 0x04 };
static uint8_t s_str1[HUB_STR_BUF];
static uint8_t s_str2[HUB_STR_BUF];
static uint8_t s_str3[HUB_STR_BUF];
static size_t  s_str1_len, s_str2_len, s_str3_len;
static bool    s_strings_init_done;

static void strings_init(void) {
    s_str1_len = encode_string_desc(s_str1, sizeof(s_str1), "mpy-pod");
    s_str2_len = encode_string_desc(s_str2, sizeof(s_str2), "Annealage Pod Notification");
    s_str3_len = encode_string_desc(s_str3, sizeof(s_str3), "1");
}

/* --- State protected by s_mutex -------------------------------------- */

#ifndef MPY_POD_HOST_TEST_BUILD
/* Both primitives are created exactly once in hub_device_register().
 * No lazy init: callers other than register that find these NULL log
 * an error and bail. */
static SemaphoreHandle_t s_mutex;
static SemaphoreHandle_t s_event_sem;
#endif

/* Pending-edge ring (protected by s_mutex). Each slot holds 1 (connect)
 * or 0 (disconnect). FIFO via head/tail; full ring drops the oldest. */
static uint8_t s_event_ring[HUB_EVENT_RING_SIZE];
static uint8_t s_event_head;   /* write index */
static uint8_t s_event_tail;   /* read index */
static uint8_t s_event_count;

static uint8_t s_current_config = 1;
static uint8_t s_current_alt    = 0;

static void state_lock(void) {
#ifndef MPY_POD_HOST_TEST_BUILD
    if (s_mutex == NULL) {
        HUB_LOGE("state_lock: mutex not initialised (register not called)");
        return;
    }
    xSemaphoreTake(s_mutex, portMAX_DELAY);
#endif
}

static void state_unlock(void) {
#ifndef MPY_POD_HOST_TEST_BUILD
    if (s_mutex == NULL) {
        return;
    }
    xSemaphoreGive(s_mutex);
#endif
}

/* Push an edge onto the ring. Must be called with s_mutex held.
 * On overflow, drop the oldest entry to make room. */
static void event_ring_push_locked(uint8_t edge) {
    if (s_event_count >= HUB_EVENT_RING_SIZE) {
        /* Drop oldest. */
        s_event_tail = (uint8_t)((s_event_tail + 1u) % HUB_EVENT_RING_SIZE);
        s_event_count--;
    }
    s_event_ring[s_event_head] = edge ? 1u : 0u;
    s_event_head = (uint8_t)((s_event_head + 1u) % HUB_EVENT_RING_SIZE);
    s_event_count++;
}

/* Pop the oldest edge. Returns true and writes to *out_edge if one
 * was available. Must be called with s_mutex held. */
static bool event_ring_pop_locked(uint8_t *out_edge) {
    if (s_event_count == 0u) {
        return false;
    }
    *out_edge = s_event_ring[s_event_tail];
    s_event_tail = (uint8_t)((s_event_tail + 1u) % HUB_EVENT_RING_SIZE);
    s_event_count--;
    return true;
}

/* --- EP0 helpers ----------------------------------------------------- */

static int copy_in(const uint8_t *src, size_t src_len,
                   uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    size_t n = (src_len < in_capacity) ? src_len : in_capacity;
    memcpy(in_data, src, n);
    *in_len = n;
    return 0;
}

static int handle_get_descriptor(const usbip_setup_packet_t *setup,
                                 uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    const uint8_t desc_type  = (uint8_t)(setup->wValue >> 8);
    const uint8_t desc_index = (uint8_t)(setup->wValue & 0xFFu);

    switch (desc_type) {
    case USB_DESC_DEVICE:
        return copy_in(s_device_desc, sizeof(s_device_desc),
                       in_data, in_capacity, in_len);
    case USB_DESC_CONFIGURATION:
        return copy_in(s_config_desc, sizeof(s_config_desc),
                       in_data, in_capacity, in_len);
    case USB_DESC_STRING:
        switch (desc_index) {
        case 0: return copy_in(s_str0, sizeof(s_str0), in_data, in_capacity, in_len);
        case 1: return copy_in(s_str1, s_str1_len, in_data, in_capacity, in_len);
        case 2: return copy_in(s_str2, s_str2_len, in_data, in_capacity, in_len);
        case 3: return copy_in(s_str3, s_str3_len, in_data, in_capacity, in_len);
        default: return -EPIPE;
        }
    default:
        return -EPIPE;
    }
}

/* --- virtual_device_t ops -------------------------------------------- */

static int control_transfer(virtual_device_t *dev,
                            const usbip_setup_packet_t *setup,
                            const uint8_t *out_data, size_t out_len,
                            uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    (void)dev;
    (void)out_data;
    (void)out_len;

    *in_len = 0;

    const uint8_t req_type = setup->bmRequestType & USB_REQ_TYPE_MASK;
    const bool    req_in   = (setup->bmRequestType & USBIP_REQUEST_DIR_IN) != 0;
    const uint8_t request  = setup->bRequest;

    if (req_type != USB_REQ_TYPE_STANDARD) {
        return -EPIPE;
    }

    if (req_in) {
        switch (request) {
        case USB_REQ_GET_DESCRIPTOR:
            return handle_get_descriptor(setup, in_data, in_capacity, in_len);
        case USB_REQ_GET_CONFIGURATION:
            if (in_capacity < 1) return -EPIPE;
            in_data[0] = s_current_config;
            *in_len = 1;
            return 0;
        case USB_REQ_GET_INTERFACE:
            if (in_capacity < 1) return -EPIPE;
            in_data[0] = s_current_alt;
            *in_len = 1;
            return 0;
        case USB_REQ_GET_STATUS:
            if (in_capacity < 2) return -EPIPE;
            in_data[0] = 0; in_data[1] = 0;
            *in_len = 2;
            return 0;
        default:
            return -EPIPE;
        }
    }

    /* OUT requests with no data stage. */
    switch (request) {
    case USB_REQ_SET_CONFIGURATION:
        s_current_config = (uint8_t)(setup->wValue & 0xFFu);
        return 0;
    case USB_REQ_SET_INTERFACE:
        s_current_alt = (uint8_t)(setup->wValue & 0xFFu);
        return 0;
    case USB_REQ_CLEAR_FEATURE:
    case USB_REQ_SET_FEATURE:
    case USB_REQ_SET_ADDRESS:
        return 0;
    default:
        return -EPIPE;
    }
}

/* Try to pop a pending edge under the mutex and emit the 2-byte
 * payload. Returns true if an edge was delivered. */
static bool try_deliver_edge(uint8_t *in_data, size_t *in_len) {
    uint8_t edge = 0u;
    bool delivered = false;
    state_lock();
    if (event_ring_pop_locked(&edge)) {
        in_data[0] = edge;
        in_data[1] = 0u;
        *in_len = 2;
        delivered = true;
    }
    state_unlock();
    if (delivered) {
        HUB_LOGI("ep1_in: delivered state=%u", (unsigned)edge);
    }
    return delivered;
}

static int handle_ep1_in(uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    if (in_capacity < 2) {
        /* Host must always offer at least wMaxPacketSize. Defensive. */
        *in_len = 0;
        return 0;
    }

    /* Fast path: deliver immediately if an edge is already pending. */
    if (try_deliver_edge(in_data, in_len)) {
        return 0;
    }

    /* Slow path: block (without holding the mutex) until notify gives
     * the semaphore or the bounded wait expires. UNLINK arrives via the
     * same per-connection read loop, so the wait must be bounded. */
#ifndef MPY_POD_HOST_TEST_BUILD
    if (s_event_sem != NULL) {
        (void)xSemaphoreTake(s_event_sem, pdMS_TO_TICKS(HUB_EP1_WAIT_MS));
    }
#endif

    /* Re-check after wake (or timeout). If still nothing, return ZLP
     * and the host will resubmit; we'll block again on the next call. */
    if (try_deliver_edge(in_data, in_len)) {
        return 0;
    }
    *in_len = 0;
    return 0;
}

static int data_transfer(virtual_device_t *dev,
                         uint8_t ep_addr,
                         const uint8_t *out_data, size_t out_len,
                         uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    (void)dev;
    (void)out_data;
    (void)out_len;
    if (in_len) { *in_len = 0; }

    if (ep_addr == HUB_EP_IN) {
        return handle_ep1_in(in_data, in_capacity, in_len);
    }
    return -EPIPE;
}

/* --- Registration ----------------------------------------------------- */

static const virtual_device_ops_t s_ops = {
    .control_transfer = control_transfer,
    .data_transfer    = data_transfer,
    .on_attach        = NULL,
    .on_detach        = NULL,
};

static virtual_device_t s_device;
static bool             s_registered;

static void populate_descriptor(usbip_dev_record_t *desc) {
    memset(desc, 0, sizeof(*desc));
    desc->present              = true;
    desc->speed                = 2;  /* USB_SPEED_FULL */
    desc->id_vendor            = HUB_VID;
    desc->id_product           = HUB_PID;
    desc->bcd_device           = 0x0100;
    desc->device_class         = 0xFF;
    desc->device_subclass      = 0x00;
    desc->device_protocol      = 0x00;
    desc->configuration_value  = 1;
    desc->num_configurations   = 1;
    desc->num_interfaces       = 1;
    desc->interfaces[0].interface_class    = 0xFF;
    desc->interfaces[0].interface_subclass = 0x00;
    desc->interfaces[0].interface_protocol = 0x00;
}

int hub_device_register(void) {
    /* s_registered is the single re-entry gate. If we have already
     * registered with the usbip server, this call is a no-op. */
    if (s_registered) {
        return 0;
    }
    if (!s_strings_init_done) {
        strings_init();
        s_strings_init_done = true;
    }

#ifndef MPY_POD_HOST_TEST_BUILD
    if (s_mutex == NULL) {
        s_mutex = xSemaphoreCreateMutex();
        if (s_mutex == NULL) {
            HUB_LOGE("register: xSemaphoreCreateMutex failed");
            return -ENOMEM;
        }
    }
    if (s_event_sem == NULL) {
        s_event_sem = xSemaphoreCreateBinary();
        if (s_event_sem == NULL) {
            HUB_LOGE("register: xSemaphoreCreateBinary failed");
            return -ENOMEM;
        }
    }
#endif

    s_device.ops = &s_ops;
    s_device.ctx = NULL;
    populate_descriptor(&s_device.desc);

    int rc = usbip_register_virtual_device(&s_device);
    if (rc != 0) {
        return rc;
    }
    /* Set s_registered only after the underlying registration succeeds,
     * so a failed call can be retried. */
    s_registered = true;
    HUB_LOGI("registered: busid=%.32s vid=0x%04x pid=0x%04x",
             s_device.desc.busid, (unsigned)HUB_VID, (unsigned)HUB_PID);
    return 0;
}

bool hub_device_is_registered(void) {
    return s_registered;
}

void hub_device_notify(bool dut_connected) {
#ifndef MPY_POD_HOST_TEST_BUILD
    if (s_mutex == NULL || s_event_sem == NULL) {
        /* Must not lazy-create from the notify path: it can race with
         * the register path and runs on a different task. */
        HUB_LOGE("notify: called before hub_device_register (dropping edge)");
        return;
    }
#endif
    state_lock();
    event_ring_push_locked(dut_connected ? 1u : 0u);
    state_unlock();
#ifndef MPY_POD_HOST_TEST_BUILD
    /* Wake any handler blocked in handle_ep1_in. Binary semaphore:
     * extra gives coalesce, which is fine because the handler always
     * drains the ring after a wake. */
    (void)xSemaphoreGive(s_event_sem);
#endif
    HUB_LOGI("notify: dut_connected=%u", (unsigned)(dut_connected ? 1u : 0u));
}
