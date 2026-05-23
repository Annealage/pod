/* Annealage Pod: synthetic CMSIS-DAP-v2 USB device implementation.
 *
 * Owns the descriptor blobs and the virtual_device_t ops that route
 * USB/IP URBs into the CMSIS-DAP interpreter and the SWO ring.
 *
 * Mapping (research/usbip-multiplexing-design.md §3):
 *
 *   EP0 control: handle GET_DESCRIPTOR (device, config, string, BOS),
 *                SET_CONFIGURATION, GET_CONFIGURATION, SET_INTERFACE,
 *                GET_STATUS, CLEAR_FEATURE, SET_FEATURE; STALL on
 *                unknown vendor requests.
 *
 *   EP1 OUT     bulk: forward bytes straight into dap_core_process()
 *                     and return the response synchronously. The host
 *                     has issued the matching EP2 IN URB ahead of time
 *                     in lock-step via libusb's bulk-transfer pattern.
 *                     We answer the OUT URB with actual_length=out_len
 *                     immediately; the response sits in our static
 *                     response_buf until the EP2 IN URB arrives.
 *
 *   EP2 IN      bulk: drain the response_buf produced by the matching
 *                     EP1 OUT. If empty, return success with 0 bytes
 *                     (host re-submits).
 *
 *   EP3 IN      bulk: drain the SWO tier-2 ring via dap_core_swo_read,
 *                     capped at 60 bytes per completion to avoid the
 *                     wMaxPacketSize=64 boundary trap (probe-rs #448).
 *
 * Concurrency: all four data_transfer paths are called from the
 * usbip per-connection client_task on APP_CPU. The CMSIS-DAP request /
 * response buffer pair is accessed only from that task, so no
 * synchronisation primitive is required for it. The SWO ring is
 * drained under whatever lock swo_read installs internally.
 *
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#include "synthetic_device.h"

#include "dap_core.h"

#include "../usbip/virtual_device.h"
#include "../usbip/usbip_server.h"
#include "../usbip/usb_string_desc.h"

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* On-target firmware always logs through the IDF; host unit tests are
 * gated by MPY_POD_HOST_TEST_BUILD. The earlier ESP_PLATFORM gate
 * silently disabled tracing because the user-c-module elf-side
 * compile of this TU does not get ESP_PLATFORM defined (only the
 * __idf_main side does) and the elf-side copy is the one the linker
 * keeps. Switch to the same gate usbip_server.c uses. */
#ifndef MPY_POD_HOST_TEST_BUILD
#include "esp_log.h"
#define DAP_TRACE_TAG  "dapprobe"
#define DAP_TRACE_INFO(fmt, ...) \
    do { if (s_urb_verbose) { ESP_LOGI(DAP_TRACE_TAG, fmt, ##__VA_ARGS__); } } while (0)
#else
#define DAP_TRACE_INFO(fmt, ...) do { (void)s_urb_verbose; } while (0)
#endif

/* Per-URB observability flag, toggled at runtime via
 * synthetic_device_set_verbose(). Default off. */
static volatile bool s_urb_verbose = false;

void synthetic_device_set_verbose(bool enable)
{
    s_urb_verbose = enable;
#ifndef MPY_POD_HOST_TEST_BUILD
    ESP_LOGI(DAP_TRACE_TAG, "URB verbose logging %s",
             enable ? "enabled" : "disabled");
#endif
}

bool synthetic_device_is_verbose(void)
{
    return s_urb_verbose;
}

/* ---------------------------------------------------------------------
 * USB-class request constants (subset, host-portable)
 * ------------------------------------------------------------------ */

#define USB_REQ_GET_STATUS        0x00u
#define USB_REQ_CLEAR_FEATURE     0x01u
#define USB_REQ_SET_FEATURE       0x03u
#define USB_REQ_SET_ADDRESS       0x05u
#define USB_REQ_GET_DESCRIPTOR    0x06u
#define USB_REQ_SET_DESCRIPTOR    0x07u
#define USB_REQ_GET_CONFIGURATION 0x08u
#define USB_REQ_SET_CONFIGURATION 0x09u
#define USB_REQ_GET_INTERFACE     0x0Au
#define USB_REQ_SET_INTERFACE     0x0Bu

#define USB_DESC_DEVICE           0x01u
#define USB_DESC_CONFIGURATION    0x02u
#define USB_DESC_STRING           0x03u
#define USB_DESC_INTERFACE        0x04u
#define USB_DESC_ENDPOINT         0x05u
#define USB_DESC_BOS              0x0Fu

#define USB_REQ_TYPE_STANDARD     0x00u
#define USB_REQ_TYPE_VENDOR       0x40u
#define USB_REQ_TYPE_MASK         0x60u

#define MS_OS20_VENDOR_CODE       0x01u
#define MS_OS20_FEATURE_DESCRIPTOR_INDEX 0x07u

/* ---------------------------------------------------------------------
 * Device descriptor (18 bytes, USB 2.1)
 * ------------------------------------------------------------------ */

static const uint8_t s_device_desc[SYNTHETIC_DAP_DEVICE_DESC_LEN] = {
    0x12,                         /* bLength */
    USB_DESC_DEVICE,              /* bDescriptorType */
    0x10, 0x02,                   /* bcdUSB = 2.10 (LE) */
    0xEF,                         /* bDeviceClass: Misc Class */
    0x02,                         /* bDeviceSubClass: Common Class */
    0x01,                         /* bDeviceProtocol: IAD */
    0x40,                         /* bMaxPacketSize0 = 64 */
    (uint8_t)(SYNTHETIC_DAP_VID & 0xFF),
    (uint8_t)(SYNTHETIC_DAP_VID >> 8),
    (uint8_t)(SYNTHETIC_DAP_PID & 0xFF),
    (uint8_t)(SYNTHETIC_DAP_PID >> 8),
    0x00, 0x01,                   /* bcdDevice = 1.00 */
    0x01,                         /* iManufacturer */
    0x02,                         /* iProduct */
    0x03,                         /* iSerialNumber */
    0x01,                         /* bNumConfigurations */
};

/* ---------------------------------------------------------------------
 * Configuration descriptor (39 bytes total = 9 + 9 + 3*7)
 *
 *   [0..8]     : configuration header (bus-powered, 500 mA)
 *   [9..17]    : interface 0 (vendor-specific, 3 endpoints, iInterface=4)
 *   [18..24]   : EP1 OUT bulk, MaxPacket=64
 *   [25..31]   : EP2 IN  bulk, MaxPacket=64
 *   [32..38]   : EP3 IN  bulk, MaxPacket=64 (SWO)
 * ------------------------------------------------------------------ */

static const uint8_t s_config_desc[SYNTHETIC_DAP_CONFIG_DESC_LEN] = {
    /* Configuration header */
    0x09, USB_DESC_CONFIGURATION,
    SYNTHETIC_DAP_CONFIG_DESC_LEN, 0x00,   /* wTotalLength */
    0x01,                                   /* bNumInterfaces */
    0x01,                                   /* bConfigurationValue */
    0x00,                                   /* iConfiguration */
    0x80,                                   /* bmAttributes: bus-powered */
    0xFA,                                   /* bMaxPower = 500 mA */

    /* Interface 0 (vendor-specific, 3 endpoints) */
    0x09, USB_DESC_INTERFACE,
    0x00,                                   /* bInterfaceNumber */
    0x00,                                   /* bAlternateSetting */
    0x03,                                   /* bNumEndpoints */
    0xFF,                                   /* bInterfaceClass: vendor-specific */
    0x00,                                   /* bInterfaceSubClass */
    0x00,                                   /* bInterfaceProtocol */
    0x04,                                   /* iInterface = "CMSIS-DAP" */

    /* EP1 Bulk-OUT (DAP commands) */
    0x07, USB_DESC_ENDPOINT,
    SYNTHETIC_DAP_EP_OUT_CMD,
    0x02,                                   /* Bulk */
    SYNTHETIC_DAP_BULK_MAX_PACKET, 0x00,
    0x00,                                   /* bInterval */

    /* EP2 Bulk-IN (DAP responses) */
    0x07, USB_DESC_ENDPOINT,
    SYNTHETIC_DAP_EP_IN_RESP,
    0x02,
    SYNTHETIC_DAP_BULK_MAX_PACKET, 0x00,
    0x00,

    /* EP3 Bulk-IN (SWO) */
    0x07, USB_DESC_ENDPOINT,
    SYNTHETIC_DAP_EP_IN_SWO,
    0x02,
    SYNTHETIC_DAP_BULK_MAX_PACKET, 0x00,
    0x00,
};

#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
_Static_assert(sizeof(s_device_desc) == SYNTHETIC_DAP_DEVICE_DESC_LEN,
               "device descriptor size mismatch");
_Static_assert(sizeof(s_config_desc) == SYNTHETIC_DAP_CONFIG_DESC_LEN,
               "configuration descriptor size mismatch");
#endif

/* ---------------------------------------------------------------------
 * String descriptors (UTF-16LE)
 *
 * Index 0: LANGID list (just en-US = 0x0409).
 * Index 1: Manufacturer "mpy-pod".
 * Index 2: Product "mpy-pod synthetic CMSIS-DAP".
 * Index 3: iSerial - lower 12 hex of efuse MAC, populated at init.
 * Index 4: iInterface "CMSIS-DAP" - load-bearing for pyOCD/probe-rs.
 * ------------------------------------------------------------------ */

#define MAX_STRING_BUF 64

static uint8_t s_str0[]  = { 0x04, USB_DESC_STRING, 0x09, 0x04 };

static uint8_t s_str1[MAX_STRING_BUF];
static uint8_t s_str2[MAX_STRING_BUF];
static uint8_t s_str3[MAX_STRING_BUF];
static uint8_t s_str4[MAX_STRING_BUF];

static size_t s_str1_len, s_str2_len, s_str3_len, s_str4_len;

static void synthetic_device_strings_init(void) {
    s_str1_len = encode_string_desc(s_str1, sizeof(s_str1), "mpy-pod");
    s_str2_len = encode_string_desc(s_str2, sizeof(s_str2), "mpy-pod synthetic CMSIS-DAP");
    const char *serial = dap_core_serial_string();
    if (serial == NULL || serial[0] == '\0') {
        serial = "000000000000";
    }
    s_str3_len = encode_string_desc(s_str3, sizeof(s_str3), serial);
    s_str4_len = encode_string_desc(s_str4, sizeof(s_str4), "CMSIS-DAP");
}

/* ---------------------------------------------------------------------
 * BOS / MS-OS-2.0 descriptors (Windows WinUSB auto-binding).
 *
 * Phase 2 scope is Linux hosts (pyOCD / probe-rs / OpenOCD) so we
 * stub these to "no platform capabilities", which Linux ignores
 * cleanly. A future commit can lift the windowsair blob.
 * ------------------------------------------------------------------ */

static const uint8_t s_bos_desc[] = {
    0x05,             /* bLength */
    USB_DESC_BOS,
    0x05, 0x00,       /* wTotalLength = 5 */
    0x00,             /* bNumDeviceCaps = 0 */
};

/* ---------------------------------------------------------------------
 * DAP cmd/response buffering
 *
 * EP1 OUT pushes a request frame into s_dap_cmd_buf and triggers the
 * synchronous DAP_ExecuteCommand. The response is parked in
 * s_dap_resp_buf (length s_dap_resp_len) until the matching EP2 IN
 * URB arrives. Buffer ownership is the per-connection client_task; no
 * cross-task races.
 * ------------------------------------------------------------------ */

#define DAP_CMD_BUF_SIZE   1024u  /* matches DAP_PACKET_SIZE max + slack */
#define DAP_RESP_BUF_SIZE  1024u

static uint8_t s_dap_cmd_buf [DAP_CMD_BUF_SIZE];
static uint8_t s_dap_resp_buf[DAP_RESP_BUF_SIZE];
static size_t  s_dap_resp_len;

/* ---------------------------------------------------------------------
 * Helpers
 * ------------------------------------------------------------------ */

static int copy_in(const uint8_t *src, size_t src_len,
                   uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    size_t n = (src_len < in_capacity) ? src_len : in_capacity;
    memcpy(in_data, src, n);
    *in_len = n;
    return 0;
}

/* ---------------------------------------------------------------------
 * EP0 control transfer
 * ------------------------------------------------------------------ */

static int handle_get_descriptor(const usbip_setup_packet_t *setup,
                                 uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    const uint8_t  desc_type = (uint8_t)(setup->wValue >> 8);
    const uint8_t  desc_index = (uint8_t)(setup->wValue & 0xFFu);
    const uint16_t requested = setup->wLength;
    (void)requested;

    switch (desc_type) {
    case USB_DESC_DEVICE:
        return copy_in(s_device_desc, sizeof(s_device_desc),
                       in_data, in_capacity, in_len);
    case USB_DESC_CONFIGURATION:
        return copy_in(s_config_desc, sizeof(s_config_desc),
                       in_data, in_capacity, in_len);
    case USB_DESC_BOS:
        return copy_in(s_bos_desc, sizeof(s_bos_desc),
                       in_data, in_capacity, in_len);
    case USB_DESC_STRING:
        switch (desc_index) {
        case 0: return copy_in(s_str0, sizeof(s_str0), in_data, in_capacity, in_len);
        case 1: return copy_in(s_str1, s_str1_len, in_data, in_capacity, in_len);
        case 2: return copy_in(s_str2, s_str2_len, in_data, in_capacity, in_len);
        case 3: return copy_in(s_str3, s_str3_len, in_data, in_capacity, in_len);
        case 4: return copy_in(s_str4, s_str4_len, in_data, in_capacity, in_len);
        default: return -EPIPE;
        }
    default:
        return -EPIPE;
    }
}

static uint8_t s_current_config = 1;
static uint8_t s_current_alt    = 0;

static int control_transfer(virtual_device_t *dev,
                            const usbip_setup_packet_t *setup,
                            const uint8_t *out_data, size_t out_len,
                            uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    (void)dev;
    (void)out_data;
    (void)out_len;

    *in_len = 0;

    const uint8_t req_type   = setup->bmRequestType & USB_REQ_TYPE_MASK;
    const bool    req_in     = (setup->bmRequestType & USBIP_REQUEST_DIR_IN) != 0;
    const uint8_t request    = setup->bRequest;

    if (req_type == USB_REQ_TYPE_STANDARD) {
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
        } else {
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
    }

    if (req_type == USB_REQ_TYPE_VENDOR) {
        /* MS-OS-2.0 vendor request: bRequest = MS_OS20_VENDOR_CODE,
         * wIndex = 7. We do not ship MS-OS-2.0 in Phase 2; STALL so
         * Windows hosts fall back to the .inf path. */
        if (req_in && request == MS_OS20_VENDOR_CODE &&
            setup->wIndex == MS_OS20_FEATURE_DESCRIPTOR_INDEX) {
            return -EPIPE;
        }
        return -EPIPE;
    }

    return -EPIPE;
}

/* ---------------------------------------------------------------------
 * EP1 / EP2 / EP3 data transfer
 * ------------------------------------------------------------------ */

static int handle_ep1_out(const uint8_t *out_data, size_t out_len) {
    if (out_len == 0 || out_len > DAP_CMD_BUF_SIZE) {
        return -EPIPE;
    }
    DAP_TRACE_INFO("dap_cmd_in: cmd=0x%02x len=%u b0..7=%02x %02x %02x %02x %02x %02x %02x %02x",
                   (unsigned)out_data[0], (unsigned)out_len,
                   (unsigned)(out_len > 0 ? out_data[0] : 0),
                   (unsigned)(out_len > 1 ? out_data[1] : 0),
                   (unsigned)(out_len > 2 ? out_data[2] : 0),
                   (unsigned)(out_len > 3 ? out_data[3] : 0),
                   (unsigned)(out_len > 4 ? out_data[4] : 0),
                   (unsigned)(out_len > 5 ? out_data[5] : 0),
                   (unsigned)(out_len > 6 ? out_data[6] : 0),
                   (unsigned)(out_len > 7 ? out_data[7] : 0));
    memcpy(s_dap_cmd_buf, out_data, out_len);
    s_dap_resp_len = dap_core_process(s_dap_cmd_buf, out_len,
                                       s_dap_resp_buf, sizeof(s_dap_resp_buf));
    DAP_TRACE_INFO("dap_resp_out: cmd=0x%02x len=%u b0..7=%02x %02x %02x %02x %02x %02x %02x %02x",
                   (unsigned)s_dap_cmd_buf[0], (unsigned)s_dap_resp_len,
                   (unsigned)(s_dap_resp_len > 0 ? s_dap_resp_buf[0] : 0),
                   (unsigned)(s_dap_resp_len > 1 ? s_dap_resp_buf[1] : 0),
                   (unsigned)(s_dap_resp_len > 2 ? s_dap_resp_buf[2] : 0),
                   (unsigned)(s_dap_resp_len > 3 ? s_dap_resp_buf[3] : 0),
                   (unsigned)(s_dap_resp_len > 4 ? s_dap_resp_buf[4] : 0),
                   (unsigned)(s_dap_resp_len > 5 ? s_dap_resp_buf[5] : 0),
                   (unsigned)(s_dap_resp_len > 6 ? s_dap_resp_buf[6] : 0),
                   (unsigned)(s_dap_resp_len > 7 ? s_dap_resp_buf[7] : 0));
    return 0;
}

static int handle_ep2_in(uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    if (s_dap_resp_len == 0) {
        *in_len = 0;
        return 0;
    }
    size_t n = (s_dap_resp_len < in_capacity) ? s_dap_resp_len : in_capacity;
    memcpy(in_data, s_dap_resp_buf, n);
    *in_len = n;

    /* Move any remainder forward so a follow-on EP2 IN drains it. The
     * canonical CMSIS-DAP-v2 host issues a single EP2 IN with capacity
     * == DAP_PACKET_SIZE so the remainder is always 0; defend anyway. */
    if (n < s_dap_resp_len) {
        memmove(s_dap_resp_buf, s_dap_resp_buf + n, s_dap_resp_len - n);
        s_dap_resp_len -= n;
    } else {
        s_dap_resp_len = 0;
    }
    return 0;
}

/* probe-rs #448 ZLP cap: at FullSpeed wMaxPacketSize=64, completions
 * that are exactly a multiple of 64 stall some hosts. Cap the SWO
 * Bulk-IN payload at 60 so any natural completion is automatically
 * a short packet. The host re-submits and drains the rest. */
#define SWO_EP3_PAYLOAD_CAP 60u

static int handle_ep3_in(uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    size_t cap = in_capacity;
    if (cap > SWO_EP3_PAYLOAD_CAP) {
        cap = SWO_EP3_PAYLOAD_CAP;
    }
    ssize_t n = dap_core_swo_read(in_data, cap);
    *in_len = (n < 0) ? 0 : (size_t)n;
#ifndef MPY_POD_HOST_TEST_BUILD
    if (s_urb_verbose && *in_len > 0) {
        dap_core_telemetry_t t = {0};
        dap_core_telemetry(&t);
        DAP_TRACE_INFO("dap_swo_out: len=%u overruns=%u",
                       (unsigned)*in_len, (unsigned)t.swo_overruns_total);
    }
#endif
    return 0;
}

static int data_transfer(virtual_device_t *dev,
                         uint8_t ep_addr,
                         const uint8_t *out_data, size_t out_len,
                         uint8_t *in_data, size_t in_capacity, size_t *in_len) {
    (void)dev;
    if (in_len) { *in_len = 0; }

    switch (ep_addr) {
    case SYNTHETIC_DAP_EP_OUT_CMD:
        return handle_ep1_out(out_data, out_len);
    case SYNTHETIC_DAP_EP_IN_RESP:
        return handle_ep2_in(in_data, in_capacity, in_len);
    case SYNTHETIC_DAP_EP_IN_SWO:
        return handle_ep3_in(in_data, in_capacity, in_len);
    default:
        return -EPIPE;
    }
}

static int on_attach(virtual_device_t *dev) {
    (void)dev;
    /* Reset the EP buffers and any sticky state on host attach. */
    s_dap_resp_len = 0;
    return 0;
}

static void on_detach(virtual_device_t *dev) {
    (void)dev;
    s_dap_resp_len = 0;
}

/* ---------------------------------------------------------------------
 * virtual_device_t binding
 * ------------------------------------------------------------------ */

static const virtual_device_ops_t s_ops = {
    .control_transfer = control_transfer,
    .data_transfer    = data_transfer,
    .on_attach        = on_attach,
    .on_detach        = on_detach,
};

static virtual_device_t s_device;
static bool s_strings_init_done;
static bool s_descriptor_populated;

static void populate_descriptor(usbip_dev_record_t *desc) {
    memset(desc, 0, sizeof(*desc));
    desc->present = true;
    /* path/busid/busnum/devnum filled in by usbip_register_virtual_device. */
    desc->speed              = 2;  /* USB_SPEED_FULL */
    desc->id_vendor          = SYNTHETIC_DAP_VID;
    desc->id_product         = SYNTHETIC_DAP_PID;
    desc->bcd_device         = 0x0100;
    desc->device_class       = 0xEF;
    desc->device_subclass    = 0x02;
    desc->device_protocol    = 0x01;
    desc->configuration_value = 1;
    desc->num_configurations  = 1;
    desc->num_interfaces      = 1;
    desc->interfaces[0].interface_class    = 0xFF;
    desc->interfaces[0].interface_subclass = 0x00;
    desc->interfaces[0].interface_protocol = 0x00;
}

int synthetic_device_register(void) {
    if (!s_strings_init_done) {
        synthetic_device_strings_init();
        s_strings_init_done = true;
    }
    if (!s_descriptor_populated) {
        s_device.ops = &s_ops;
        s_device.ctx = NULL;
        populate_descriptor(&s_device.desc);
        s_descriptor_populated = true;
    }
    return usbip_server_register_virtual_device(&s_device);
}

virtual_device_t *synthetic_device_get(void) {
    if (!s_descriptor_populated) {
        if (!s_strings_init_done) {
            synthetic_device_strings_init();
            s_strings_init_done = true;
        }
        s_device.ops = &s_ops;
        s_device.ctx = NULL;
        populate_descriptor(&s_device.desc);
        s_descriptor_populated = true;
    }
    return &s_device;
}

const uint8_t *synthetic_device_get_device_desc(size_t *out_len) {
    if (out_len) { *out_len = sizeof(s_device_desc); }
    return s_device_desc;
}

const uint8_t *synthetic_device_get_config_desc(size_t *out_len) {
    if (out_len) { *out_len = sizeof(s_config_desc); }
    return s_config_desc;
}

const uint8_t *synthetic_device_get_string_desc(uint8_t index, size_t *out_len) {
    if (!s_strings_init_done) {
        synthetic_device_strings_init();
        s_strings_init_done = true;
    }
    switch (index) {
    case 0: if (out_len) *out_len = sizeof(s_str0); return s_str0;
    case 1: if (out_len) *out_len = s_str1_len;     return s_str1;
    case 2: if (out_len) *out_len = s_str2_len;     return s_str2;
    case 3: if (out_len) *out_len = s_str3_len;     return s_str3;
    case 4: if (out_len) *out_len = s_str4_len;     return s_str4;
    default:
        if (out_len) *out_len = 0;
        return NULL;
    }
}
