/* Annealage Pod: synthetic CDC ACM virtual USB device implementation.
 *
 * Descriptor layout (cdc.py in micropython-lib is the canonical reference):
 *
 *   Device:          18 bytes (bDeviceClass=0xEF IAD-composite)
 *   Config total:    75 bytes
 *     [  0..  8]     configuration header
 *     [  9.. 16]     IAD (interfaces 0+1, class=CDC)
 *     [ 17.. 25]     interface 0 - CDC Control (class=0x02, subclass=0x02)
 *     [ 26.. 30]     CDC Header functional
 *     [ 31.. 35]     CDC Call Management functional (bDataInterface=1)
 *     [ 36.. 39]     CDC ACM functional (bmCapabilities=0x06)
 *     [ 40.. 44]     CDC Union functional (ctrl=0, data=1)
 *     [ 45.. 51]     EP1 IN interrupt (8 bytes, interval=16)
 *     [ 52.. 60]     interface 1 - CDC Data (class=0x0A)
 *     [ 61.. 67]     EP2 OUT bulk (64 bytes, host -> UART TX)
 *     [ 68.. 74]     EP3 IN  bulk (64 bytes, UART RX -> host)
 *
 * EP behaviour:
 *   EP1 IN  (0x81) interrupt: always returns ZLP; we have no SERIAL_STATE
 *                             to push. Linux cdc-acm re-submits and polls.
 *   EP2 OUT (0x02) bulk:      forward bytes to uart_write_bytes().
 *   EP3 IN  (0x83) bulk:      drain uart_read_bytes() with 10 ms timeout;
 *                             return 0 bytes if nothing arrived (ZLP poll).
 *
 * SET_LINE_CODING is applied live via uart_param_config() so baud/parity
 * changes from the host affect the physical UART immediately.
 *
 * Concurrency: all virtual_device ops are called from the usbip
 * per-connection client_task on APP_CPU. UART driver calls are
 * thread-safe per IDF documentation.
 *
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#include "uart_cdc_device.h"

#include "../usbip/virtual_device.h"
#include "../usbip/usbip_server.h"

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "../usbip/usb_string_desc.h"

/* IDF / FreeRTOS headers are only available in the firmware build. */
#ifndef MPY_POD_HOST_TEST_BUILD
#include "freertos/FreeRTOS.h"
#include "driver/uart.h"
#include "esp_log.h"
#define CDC_TAG "uartcdc"
#define CDC_LOGI(fmt, ...) \
    do { if (s_verbose) { ESP_LOGI(CDC_TAG, fmt, ##__VA_ARGS__); } } while (0)
#else
/* Host test build: driver/uart.h shim is on the include path from
 * test/unit/uartcdc/support/. FreeRTOS not available; provide the
 * pdMS_TO_TICKS macro used by uart_read_bytes(). */
#include "driver/uart.h"
#define pdMS_TO_TICKS(ms) ((uint32_t)(ms))
#define CDC_LOGI(fmt, ...) ((void)s_verbose)
#endif

/* -----------------------------------------------------------------------
 * USB descriptor constants (host-portable subset)
 * -------------------------------------------------------------------- */

#define USB_DESC_DEVICE         0x01u
#define USB_DESC_CONFIGURATION  0x02u
#define USB_DESC_STRING         0x03u
#define USB_DESC_ENDPOINT       0x05u
#define USB_DESC_IAD            0x0Bu
#define USB_CS_INTERFACE        0x24u

#define USB_REQ_GET_STATUS        0x00u
#define USB_REQ_CLEAR_FEATURE     0x01u
#define USB_REQ_SET_FEATURE       0x03u
#define USB_REQ_SET_ADDRESS       0x05u
#define USB_REQ_GET_DESCRIPTOR    0x06u
#define USB_REQ_SET_CONFIGURATION 0x09u
#define USB_REQ_GET_CONFIGURATION 0x08u
#define USB_REQ_GET_INTERFACE     0x0Au
#define USB_REQ_SET_INTERFACE     0x0Bu

#define USB_REQ_TYPE_STANDARD   0x00u
#define USB_REQ_TYPE_CLASS      0x20u
#define USB_REQ_TYPE_MASK       0x60u

/* CDC class requests (bmRequestType = 0x21). */
#define CDC_REQ_SET_LINE_CODING         0x20u
#define CDC_REQ_GET_LINE_CODING         0x21u
#define CDC_REQ_SET_CONTROL_LINE_STATE  0x22u
#define CDC_REQ_SEND_BREAK              0x23u

/* CDC functional descriptor subtypes. */
#define CDC_FUNC_HEADER   0x00u
#define CDC_FUNC_CALL_MGT 0x01u
#define CDC_FUNC_ACM      0x02u
#define CDC_FUNC_UNION    0x06u

/* -----------------------------------------------------------------------
 * Device descriptor (18 bytes)
 * -------------------------------------------------------------------- */

static const uint8_t s_device_desc[UART_CDC_DEVICE_DESC_LEN] = {
    0x12,                                   /* bLength */
    USB_DESC_DEVICE,                        /* bDescriptorType */
    0x00, 0x02,                             /* bcdUSB = 2.00 (LE) */
    0xEF,                                   /* bDeviceClass: Misc */
    0x02,                                   /* bDeviceSubClass: Common */
    0x01,                                   /* bDeviceProtocol: IAD */
    0x40,                                   /* bMaxPacketSize0 = 64 */
    (uint8_t)(UART_CDC_VID & 0xFF),
    (uint8_t)(UART_CDC_VID >> 8),
    (uint8_t)(UART_CDC_PID & 0xFF),
    (uint8_t)(UART_CDC_PID >> 8),
    0x00, 0x01,                             /* bcdDevice = 1.00 */
    0x01,                                   /* iManufacturer */
    0x02,                                   /* iProduct */
    0x03,                                   /* iSerialNumber */
    0x01,                                   /* bNumConfigurations */
};

/* -----------------------------------------------------------------------
 * Configuration descriptor (75 bytes)
 * -------------------------------------------------------------------- */

static const uint8_t s_config_desc[UART_CDC_CONFIG_DESC_LEN] = {
    /* [0..8] Configuration header */
    0x09, USB_DESC_CONFIGURATION,
    UART_CDC_CONFIG_DESC_LEN, 0x00,         /* wTotalLength = 75 (LE) */
    0x02,                                   /* bNumInterfaces = 2 */
    0x01,                                   /* bConfigurationValue */
    0x00,                                   /* iConfiguration */
    0x80,                                   /* bmAttributes: bus-powered */
    0xFA,                                   /* bMaxPower = 500 mA */

    /* [9..16] IAD */
    0x08, USB_DESC_IAD,
    0x00,                                   /* bFirstInterface = 0 */
    0x02,                                   /* bInterfaceCount = 2 */
    0x02,                                   /* bFunctionClass: CDC */
    0x02,                                   /* bFunctionSubClass: ACM */
    0x00,                                   /* bFunctionProtocol */
    0x00,                                   /* iFunction */

    /* [17..25] Interface 0 - CDC Control */
    0x09, 0x04,
    0x00,                                   /* bInterfaceNumber = 0 */
    0x00,                                   /* bAlternateSetting */
    0x01,                                   /* bNumEndpoints = 1 */
    0x02,                                   /* bInterfaceClass: CDC */
    0x02,                                   /* bInterfaceSubClass: ACM */
    0x00,                                   /* bInterfaceProtocol */
    0x00,                                   /* iInterface */

    /* [26..30] CDC Header functional */
    0x05, USB_CS_INTERFACE, CDC_FUNC_HEADER,
    0x20, 0x01,                             /* bcdCDC = 1.20 (LE) */

    /* [31..35] CDC Call Management functional */
    0x05, USB_CS_INTERFACE, CDC_FUNC_CALL_MGT,
    0x00,                                   /* bmCapabilities: no call mgmt */
    0x01,                                   /* bDataInterface = 1 */

    /* [36..39] CDC ACM functional */
    0x04, USB_CS_INTERFACE, CDC_FUNC_ACM,
    0x06,                                   /* bmCapabilities: line coding +
                                             * control line state + send_break */

    /* [40..44] CDC Union functional */
    0x05, USB_CS_INTERFACE, CDC_FUNC_UNION,
    0x00,                                   /* bControlInterface = 0 */
    0x01,                                   /* bSubordinateInterface0 = 1 */

    /* [45..51] EP1 IN interrupt (SERIAL_STATE notifications) */
    0x07, USB_DESC_ENDPOINT,
    UART_CDC_EP_NOTIFY_IN,
    0x03,                                   /* bmAttributes: Interrupt */
    0x08, 0x00,                             /* wMaxPacketSize = 8 */
    0x10,                                   /* bInterval = 16 ms */

    /* [52..60] Interface 1 - CDC Data */
    0x09, 0x04,
    0x01,                                   /* bInterfaceNumber = 1 */
    0x00,                                   /* bAlternateSetting */
    0x02,                                   /* bNumEndpoints = 2 */
    0x0A,                                   /* bInterfaceClass: CDC Data */
    0x00,                                   /* bInterfaceSubClass */
    0x00,                                   /* bInterfaceProtocol */
    0x00,                                   /* iInterface */

    /* [61..67] EP2 OUT bulk (host -> UART TX) */
    0x07, USB_DESC_ENDPOINT,
    UART_CDC_EP_DATA_OUT,
    0x02,                                   /* bmAttributes: Bulk */
    0x40, 0x00,                             /* wMaxPacketSize = 64 */
    0x00,                                   /* bInterval */

    /* [68..74] EP3 IN bulk (UART RX -> host) */
    0x07, USB_DESC_ENDPOINT,
    UART_CDC_EP_DATA_IN,
    0x02,                                   /* bmAttributes: Bulk */
    0x40, 0x00,                             /* wMaxPacketSize = 64 */
    0x00,                                   /* bInterval */
};

#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
_Static_assert(sizeof(s_device_desc) == UART_CDC_DEVICE_DESC_LEN,
               "device descriptor size mismatch");
_Static_assert(sizeof(s_config_desc) == UART_CDC_CONFIG_DESC_LEN,
               "config descriptor size mismatch");
#endif

/* -----------------------------------------------------------------------
 * String descriptors (UTF-16LE)
 *
 * Index 0: LANGID (en-US = 0x0409)
 * Index 1: Manufacturer "mpy-pod"
 * Index 2: Product "mpy-pod CDC UART"
 * Index 3: Serial (lower 12 hex of efuse MAC, or all-zeros on host)
 * -------------------------------------------------------------------- */

#define MAX_STR_BUF 64u

static uint8_t s_str0[] = { 0x04, USB_DESC_STRING, 0x09, 0x04 };
static uint8_t s_str1[MAX_STR_BUF];
static uint8_t s_str2[MAX_STR_BUF];
static uint8_t s_str3[MAX_STR_BUF];

static size_t s_str1_len, s_str2_len, s_str3_len;
static bool   s_strings_ready;

static void init_strings(void)
{
    if (s_strings_ready) { return; }
    s_str1_len = encode_string_desc(s_str1, sizeof(s_str1), "mpy-pod");
    s_str2_len = encode_string_desc(s_str2, sizeof(s_str2), "mpy-pod CDC UART");

    char serial[13] = "000000000000";
#ifndef MPY_POD_HOST_TEST_BUILD
    {
        uint8_t mac[6] = {0};
        extern esp_err_t esp_efuse_mac_get_default(uint8_t *mac);
        if (esp_efuse_mac_get_default(mac) == 0) {
            snprintf(serial, sizeof(serial), "%02x%02x%02x%02x%02x%02x",
                     mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        }
    }
#endif
    s_str3_len = encode_string_desc(s_str3, sizeof(s_str3), serial);
    s_strings_ready = true;
}

/* -----------------------------------------------------------------------
 * CDC line-coding and line-state state
 * -------------------------------------------------------------------- */

/* 7-byte CDC line coding: dwDTERate(4LE) bCharFormat bParityType bDataBits.
 * Default 115200 / 8N1. */
static uint8_t s_line_coding[7] = {
    0x00, 0xC2, 0x01, 0x00,   /* dwDTERate = 115200 LE */
    0x00,                      /* bCharFormat: 1 stop bit */
    0x00,                      /* bParityType: none */
    0x08,                      /* bDataBits: 8 */
};

/* -----------------------------------------------------------------------
 * UART state
 * -------------------------------------------------------------------- */

static int  s_uart_num = -1;
static bool s_attached;
static bool s_verbose;

/* Apply the current s_line_coding to the UART. No-op in host test build
 * (the stubs record the call for assertions). */
static void apply_line_coding(void)
{
    if (s_uart_num < 0) { return; }

    uint32_t baud = 0;
    memcpy(&baud, s_line_coding, 4);

    uint8_t fmt    = s_line_coding[4]; /* 0=1stop, 1=1.5stop, 2=2stop */
    uint8_t parity = s_line_coding[5]; /* 0=none,  1=odd,     2=even  */
    uint8_t bits   = s_line_coding[6]; /* 5..8 */

    /* Stop bits: CDC 0/1/2 -> IDF 1/2/3. */
    uart_stop_bits_t stop = (fmt <= 2u)
        ? (uart_stop_bits_t)((int)fmt + 1)
        : UART_STOP_BITS_1;

    /* Parity: CDC 0=none, 1=odd, 2=even; others unsupported -> disable. */
    static const uart_parity_t parity_map[5] = {
        UART_PARITY_DISABLE,
        UART_PARITY_ODD,
        UART_PARITY_EVEN,
        UART_PARITY_DISABLE,
        UART_PARITY_DISABLE,
    };
    uart_parity_t par = (parity < 5u) ? parity_map[parity] : UART_PARITY_DISABLE;

    /* Data bits: CDC value 5..8, IDF enum 0..3 (UART_DATA_5_BITS..8_BITS). */
    uart_word_length_t data = (bits >= 5u && bits <= 8u)
        ? (uart_word_length_t)((int)bits - 5)
        : UART_DATA_8_BITS;

    uart_config_t cfg = {
        .baud_rate  = (int)baud,
        .data_bits  = data,
        .parity     = par,
        .stop_bits  = stop,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .rx_flow_ctrl_thresh = 0,
        .source_clk = UART_SCLK_DEFAULT,
    };
    uart_param_config((uart_port_t)s_uart_num, &cfg);
    CDC_LOGI("line coding applied: baud=%u data=%u parity=%u stop=%u",
             (unsigned)baud, (unsigned)bits, (unsigned)parity, (unsigned)fmt);
}

/* -----------------------------------------------------------------------
 * Helpers
 * -------------------------------------------------------------------- */

static int copy_in(const uint8_t *src, size_t src_len,
                   uint8_t *in_data, size_t in_cap, size_t *in_len)
{
    size_t n = (src_len < in_cap) ? src_len : in_cap;
    memcpy(in_data, src, n);
    *in_len = n;
    return 0;
}

/* -----------------------------------------------------------------------
 * EP0 control transfer
 * -------------------------------------------------------------------- */

static int handle_get_descriptor(const usbip_setup_packet_t *setup,
                                  uint8_t *in_data, size_t in_cap,
                                  size_t *in_len)
{
    const uint8_t type  = (uint8_t)(setup->wValue >> 8);
    const uint8_t index = (uint8_t)(setup->wValue & 0xFFu);

    switch (type) {
    case USB_DESC_DEVICE:
        return copy_in(s_device_desc, sizeof(s_device_desc),
                       in_data, in_cap, in_len);
    case USB_DESC_CONFIGURATION:
        return copy_in(s_config_desc, sizeof(s_config_desc),
                       in_data, in_cap, in_len);
    case USB_DESC_STRING:
        init_strings();
        switch (index) {
        case 0: return copy_in(s_str0, sizeof(s_str0), in_data, in_cap, in_len);
        case 1: return copy_in(s_str1, s_str1_len,     in_data, in_cap, in_len);
        case 2: return copy_in(s_str2, s_str2_len,     in_data, in_cap, in_len);
        case 3: return copy_in(s_str3, s_str3_len,     in_data, in_cap, in_len);
        default: return -EPIPE;
        }
    default:
        return -EPIPE;
    }
}

static uint8_t s_current_config = 1;

static int control_transfer(virtual_device_t *dev,
                             const usbip_setup_packet_t *setup,
                             const uint8_t *out_data, size_t out_len,
                             uint8_t *in_data, size_t in_cap, size_t *in_len)
{
    (void)dev;
    *in_len = 0;

    const uint8_t req_type = setup->bmRequestType & USB_REQ_TYPE_MASK;
    const bool    req_in   = (setup->bmRequestType & USBIP_REQUEST_DIR_IN) != 0u;
    const uint8_t request  = setup->bRequest;

    if (req_type == USB_REQ_TYPE_STANDARD) {
        if (req_in) {
            switch (request) {
            case USB_REQ_GET_DESCRIPTOR:
                return handle_get_descriptor(setup, in_data, in_cap, in_len);
            case USB_REQ_GET_CONFIGURATION:
                if (in_cap < 1u) { return -EPIPE; }
                in_data[0] = s_current_config;
                *in_len = 1u;
                return 0;
            case USB_REQ_GET_INTERFACE:
                if (in_cap < 1u) { return -EPIPE; }
                in_data[0] = 0;
                *in_len = 1u;
                return 0;
            case USB_REQ_GET_STATUS:
                if (in_cap < 2u) { return -EPIPE; }
                in_data[0] = 0; in_data[1] = 0;
                *in_len = 2u;
                return 0;
            default:
                return -EPIPE;
            }
        } else {
            switch (request) {
            case USB_REQ_SET_CONFIGURATION:
                s_current_config = (uint8_t)(setup->wValue & 0xFFu);
                return 0;
            case USB_REQ_SET_INTERFACE:
            case USB_REQ_CLEAR_FEATURE:
            case USB_REQ_SET_FEATURE:
            case USB_REQ_SET_ADDRESS:
                return 0;
            default:
                return -EPIPE;
            }
        }
    }

    if (req_type == USB_REQ_TYPE_CLASS) {
        switch (request) {
        case CDC_REQ_SET_LINE_CODING:
            /* out_data contains the 7-byte line coding body (see
             * usbip_server.c intake_submit: OUT data stage is read
             * before control_transfer is called). */
            if (out_len >= 7u) {
                memcpy(s_line_coding, out_data, 7u);
                apply_line_coding();
            }
            return 0;
        case CDC_REQ_GET_LINE_CODING:
            return copy_in(s_line_coding, sizeof(s_line_coding),
                           in_data, in_cap, in_len);
        case CDC_REQ_SET_CONTROL_LINE_STATE:
            return 0;
        case CDC_REQ_SEND_BREAK:
            return 0;
        default:
            return -EPIPE;
        }
    }

    return -EPIPE;
}

/* -----------------------------------------------------------------------
 * Data transfers
 * -------------------------------------------------------------------- */

static int data_transfer(virtual_device_t *dev,
                          uint8_t ep_addr,
                          const uint8_t *out_data, size_t out_len,
                          uint8_t *in_data, size_t in_cap, size_t *in_len)
{
    (void)dev;
    if (in_len) { *in_len = 0u; }

    switch (ep_addr) {

    case UART_CDC_EP_NOTIFY_IN:
        /* Interrupt IN: no SERIAL_STATE to push; return ZLP. The cdc-acm
         * driver re-submits on ZLP completion and polls continuously. */
        return 0;

    case UART_CDC_EP_DATA_OUT: {
        if (out_len == 0u || s_uart_num < 0) { return 0; }
        int written = uart_write_bytes((uart_port_t)s_uart_num,
                                       (const char *)out_data, out_len);
        CDC_LOGI("ep2_out: %d bytes -> uart", written);
        (void)written;
        return 0;
    }

    case UART_CDC_EP_DATA_IN: {
        /* 10 ms timeout; ZLP if nothing arrived so the host re-submits
         * (same pattern as dapprobe SWO EP3). */
        if (s_uart_num < 0) { return 0; }
        size_t cap = (in_cap > 64u) ? 64u : in_cap;
        int n = uart_read_bytes((uart_port_t)s_uart_num,
                                in_data, (uint32_t)cap,
                                pdMS_TO_TICKS(10));
        if (n > 0) {
            *in_len = (size_t)n;
            CDC_LOGI("ep3_in: %d bytes <- uart", n);
        }
        return 0;
    }

    default:
        return -EPIPE;
    }
}

/* -----------------------------------------------------------------------
 * virtual_device_t binding
 * -------------------------------------------------------------------- */

static const virtual_device_ops_t s_ops = {
    .control_transfer = control_transfer,
    .data_transfer    = data_transfer,
    .on_attach        = NULL,
    .on_detach        = NULL,
};

static virtual_device_t s_device;
static bool             s_device_populated;

static void populate_descriptor(usbip_dev_record_t *desc)
{
    memset(desc, 0, sizeof(*desc));
    desc->present            = true;
    desc->speed              = 2;  /* USB_SPEED_FULL */
    desc->id_vendor          = UART_CDC_VID;
    desc->id_product         = UART_CDC_PID;
    desc->bcd_device         = 0x0100u;
    desc->device_class       = 0xEFu;
    desc->device_subclass    = 0x02u;
    desc->device_protocol    = 0x01u;
    desc->configuration_value = 1u;
    desc->num_configurations  = 1u;
    desc->num_interfaces      = 2u;
    desc->interfaces[0].interface_class    = 0x02u;
    desc->interfaces[0].interface_subclass = 0x02u;
    desc->interfaces[0].interface_protocol = 0x00u;
    desc->interfaces[1].interface_class    = 0x0Au;
    desc->interfaces[1].interface_subclass = 0x00u;
    desc->interfaces[1].interface_protocol = 0x00u;
}

/* -----------------------------------------------------------------------
 * Public API
 * -------------------------------------------------------------------- */

static void ensure_device_populated(void)
{
    if (s_device_populated) { return; }
    init_strings();
    s_device.ops = &s_ops;
    s_device.ctx = NULL;
    populate_descriptor(&s_device.desc);
    s_device_populated = true;
}

int uart_cdc_attach(int uart_num, int tx_pin, int rx_pin, int baud)
{
    if (s_attached) { return 0; }
    ensure_device_populated();

    s_uart_num = uart_num;

    uint32_t b = (uint32_t)baud;
    memcpy(s_line_coding, &b, 4u);

    uart_config_t cfg = {
        .baud_rate           = baud,
        .data_bits           = UART_DATA_8_BITS,
        .parity              = UART_PARITY_DISABLE,
        .stop_bits           = UART_STOP_BITS_1,
        .flow_ctrl           = UART_HW_FLOWCTRL_DISABLE,
        .rx_flow_ctrl_thresh = 0,
        .source_clk          = UART_SCLK_DEFAULT,
    };
    esp_err_t err = uart_driver_install((uart_port_t)uart_num, 2048, 2048, 0, NULL, 0);
    if (err != ESP_OK) { return -EIO; }
    uart_param_config((uart_port_t)uart_num, &cfg);
    if (tx_pin != UART_PIN_NO_CHANGE || rx_pin != UART_PIN_NO_CHANGE) {
        uart_set_pin((uart_port_t)uart_num,
                     tx_pin, rx_pin,
                     UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    }

    int rc = usbip_server_register_virtual_device(&s_device);
    if (rc == 0) {
        s_attached = true;
    }
    return rc;
}

int uart_cdc_detach(void)
{
    if (!s_attached) { return 0; }
    s_attached = false;
    if (s_uart_num >= 0) {
        uart_driver_delete((uart_port_t)s_uart_num);
        s_uart_num = -1;
    }
    return 0;
}

bool uart_cdc_is_attached(void)  { return s_attached; }
void uart_cdc_set_verbose(bool e){ s_verbose = e; }
bool uart_cdc_is_verbose(void)   { return s_verbose; }

const uint8_t *uart_cdc_get_device_desc(size_t *out_len)
{
    if (out_len) { *out_len = sizeof(s_device_desc); }
    return s_device_desc;
}

const uint8_t *uart_cdc_get_config_desc(size_t *out_len)
{
    if (out_len) { *out_len = sizeof(s_config_desc); }
    return s_config_desc;
}

const uint8_t *uart_cdc_get_string_desc(uint8_t index, size_t *out_len)
{
    init_strings();
    switch (index) {
    case 0: if (out_len) { *out_len = sizeof(s_str0); } return s_str0;
    case 1: if (out_len) { *out_len = s_str1_len; }     return s_str1;
    case 2: if (out_len) { *out_len = s_str2_len; }     return s_str2;
    case 3: if (out_len) { *out_len = s_str3_len; }     return s_str3;
    default:
        if (out_len) { *out_len = 0u; }
        return NULL;
    }
}

virtual_device_t *uart_cdc_get_device(void)
{
    ensure_device_populated();
    return &s_device;
}

#ifdef MPY_POD_HOST_TEST_BUILD
void uart_cdc_reset_for_test(void)
{
    s_attached         = false;
    s_device_populated = false;
    s_strings_ready    = false;
    s_uart_num         = -1;
    s_verbose          = false;
    s_current_config   = 1;
    /* Restore default 115200/8N1 line coding. */
    static const uint8_t default_lc[7] = {
        0x00, 0xC2, 0x01, 0x00, 0x00, 0x00, 0x08
    };
    memcpy(s_line_coding, default_lc, 7u);
}
#endif
