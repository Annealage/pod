/* Annealage Pod: unit tests for the uartcdc synthetic CDC device.
 *
 * Run with `ctest`. No external framework; uses the same TEST/ASSERT
 * harness as test/unit/dapprobe/test_dapprobe.c.
 *
 * Coverage:
 *  - Device and config descriptor byte-level layout.
 *  - control_transfer: GET_DESCRIPTOR (device, config, strings),
 *    SET_LINE_CODING (decode + uart_param_config call),
 *    GET_LINE_CODING (round-trip), SET_CONTROL_LINE_STATE.
 *  - data_transfer: EP1 IN ZLP, EP2 OUT -> uart_write, EP3 IN <- uart_read.
 *  - Unknown endpoint stalls.
 */

#include "uart_cdc_device.h"
#include "../../../src/c_modules/usbip/virtual_device.h"
#include "driver/uart.h"

#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* -----------------------------------------------------------------------
 * Test stub declarations (from uart_stubs.c)
 * -------------------------------------------------------------------- */

extern uart_config_t g_last_uart_config;
extern int           g_uart_install_count;
extern int           g_uart_delete_count;
extern int           g_uart_param_config_count;

void   uart_stub_reset(void);
void   uart_stub_inject_rx(const uint8_t *data, size_t len);
size_t uart_stub_drain_tx(uint8_t *out, size_t cap);

/* -----------------------------------------------------------------------
 * usbip registry reset (from virtual_device.c)
 * -------------------------------------------------------------------- */

extern void usbip_virtual_device_reset_for_test(void);

/* -----------------------------------------------------------------------
 * Test harness
 * -------------------------------------------------------------------- */

static int         g_failures;
static const char *g_current_test = "<none>";

#define TEST(name) \
    static void name(void); \
    static void run_##name(void) { \
        g_current_test = #name; \
        printf("[RUN ] %s\n", #name); \
        size_t _before = (size_t)g_failures; \
        uart_stub_reset(); \
        usbip_virtual_device_reset_for_test(); \
        uart_cdc_reset_for_test(); \
        name(); \
        if ((size_t)g_failures == _before) { \
            printf("[ OK ] %s\n", #name); \
        } else { \
            printf("[FAIL] %s\n", #name); \
        } \
    } \
    static void name(void)

#define ASSERT_TRUE(cond, fmt, ...) \
    do { \
        if (!(cond)) { \
            fprintf(stderr, "%s: ASSERT_TRUE failed at %s:%d: " fmt "\n", \
                    g_current_test, __FILE__, __LINE__, ##__VA_ARGS__); \
            g_failures++; \
        } \
    } while (0)

#define ASSERT_EQ_INT(a, b) \
    do { \
        long long _a = (long long)(a); \
        long long _b = (long long)(b); \
        if (_a != _b) { \
            fprintf(stderr, "%s: ASSERT_EQ_INT at %s:%d: %s=%lld %s=%lld\n", \
                    g_current_test, __FILE__, __LINE__, #a, _a, #b, _b); \
            g_failures++; \
        } \
    } while (0)

/* -----------------------------------------------------------------------
 * Helper: call control_transfer on the static virtual device.
 * -------------------------------------------------------------------- */

static int do_ctrl(uint8_t bm_req_type, uint8_t b_request,
                   uint16_t w_value, uint16_t w_index, uint16_t w_length,
                   const uint8_t *out_data, size_t out_len,
                   uint8_t *in_buf, size_t in_cap, size_t *in_len)
{
    virtual_device_t *dev = uart_cdc_get_device();
    usbip_setup_packet_t setup = {
        .bmRequestType = bm_req_type,
        .bRequest      = b_request,
        .wValue        = w_value,
        .wIndex        = w_index,
        .wLength       = w_length,
    };
    return dev->ops->control_transfer(dev, &setup,
                                       out_data, out_len,
                                       in_buf, in_cap, in_len);
}

/* -----------------------------------------------------------------------
 * Descriptor blob tests
 * -------------------------------------------------------------------- */

TEST(test_device_desc_length_and_class)
{
    size_t len = 0;
    const uint8_t *d = uart_cdc_get_device_desc(&len);
    ASSERT_EQ_INT(len, UART_CDC_DEVICE_DESC_LEN);
    ASSERT_EQ_INT(d[0],  0x12);  /* bLength */
    ASSERT_EQ_INT(d[1],  0x01);  /* bDescriptorType = DEVICE */
    ASSERT_EQ_INT(d[4],  0xEF);  /* bDeviceClass: Misc */
    ASSERT_EQ_INT(d[5],  0x02);  /* bDeviceSubClass: Common */
    ASSERT_EQ_INT(d[6],  0x01);  /* bDeviceProtocol: IAD */
    ASSERT_EQ_INT(d[7],  0x40);  /* bMaxPacketSize0 */
    /* idVendor = 0xC251 LE */
    ASSERT_EQ_INT(d[8],  0x51);
    ASSERT_EQ_INT(d[9],  0xC2);
    /* idProduct = 0xF00B LE */
    ASSERT_EQ_INT(d[10], 0x0B);
    ASSERT_EQ_INT(d[11], 0xF0);
    ASSERT_EQ_INT(d[14], 0x01);  /* iManufacturer */
    ASSERT_EQ_INT(d[15], 0x02);  /* iProduct */
    ASSERT_EQ_INT(d[16], 0x03);  /* iSerial */
    ASSERT_EQ_INT(d[17], 0x01);  /* bNumConfigurations */
}

TEST(test_config_desc_total_length_and_num_interfaces)
{
    size_t len = 0;
    const uint8_t *c = uart_cdc_get_config_desc(&len);
    ASSERT_EQ_INT(len, UART_CDC_CONFIG_DESC_LEN);   /* 75 */
    ASSERT_EQ_INT(c[0], 0x09);  /* bLength */
    ASSERT_EQ_INT(c[1], 0x02);  /* bDescriptorType = CONFIGURATION */
    ASSERT_EQ_INT(c[2], 75);    /* wTotalLength LE low */
    ASSERT_EQ_INT(c[3], 0x00);  /* wTotalLength LE high */
    ASSERT_EQ_INT(c[4], 0x02);  /* bNumInterfaces */
    ASSERT_EQ_INT(c[5], 0x01);  /* bConfigurationValue */
}

TEST(test_config_desc_iad_fields)
{
    const uint8_t *c = uart_cdc_get_config_desc(NULL);
    /* IAD at offset 9. */
    ASSERT_EQ_INT(c[9],  0x08);  /* bLength */
    ASSERT_EQ_INT(c[10], 0x0B);  /* bDescriptorType = IAD */
    ASSERT_EQ_INT(c[11], 0x00);  /* bFirstInterface = 0 */
    ASSERT_EQ_INT(c[12], 0x02);  /* bInterfaceCount = 2 */
    ASSERT_EQ_INT(c[13], 0x02);  /* bFunctionClass = CDC */
    ASSERT_EQ_INT(c[14], 0x02);  /* bFunctionSubClass = ACM */
}

TEST(test_config_desc_union_functional_interface_numbers)
{
    /* The Union functional descriptor must reference the correct control
     * and data interface numbers; otherwise Linux cdc-acm fails to bind. */
    const uint8_t *c = uart_cdc_get_config_desc(NULL);
    /* Union is at offset 40, length 5. */
    ASSERT_EQ_INT(c[40], 0x05);  /* bFunctionLength */
    ASSERT_EQ_INT(c[41], 0x24);  /* bDescriptorType = CS_INTERFACE */
    ASSERT_EQ_INT(c[42], 0x06);  /* bDescriptorSubtype = UNION */
    ASSERT_EQ_INT(c[43], 0x00);  /* bControlInterface = 0 */
    ASSERT_EQ_INT(c[44], 0x01);  /* bSubordinateInterface0 = 1 */
}

TEST(test_config_desc_endpoint_addresses)
{
    const uint8_t *c = uart_cdc_get_config_desc(NULL);
    /* EP1 IN interrupt at offset 45. */
    ASSERT_EQ_INT(c[47], UART_CDC_EP_NOTIFY_IN);   /* 0x81 */
    ASSERT_EQ_INT(c[48], 0x03);                     /* Interrupt */
    ASSERT_EQ_INT(c[49], 0x08);                     /* wMaxPacketSize = 8 */
    /* EP2 OUT bulk at offset 61. */
    ASSERT_EQ_INT(c[63], UART_CDC_EP_DATA_OUT);     /* 0x02 */
    ASSERT_EQ_INT(c[64], 0x02);                     /* Bulk */
    ASSERT_EQ_INT(c[65], 0x40);                     /* wMaxPacketSize = 64 */
    /* EP3 IN bulk at offset 68. */
    ASSERT_EQ_INT(c[70], UART_CDC_EP_DATA_IN);      /* 0x83 */
    ASSERT_EQ_INT(c[71], 0x02);                     /* Bulk */
    ASSERT_EQ_INT(c[72], 0x40);                     /* wMaxPacketSize = 64 */
}

TEST(test_config_desc_acm_capabilities)
{
    /* bmCapabilities = 0x06: supports line coding/state + send_break. */
    const uint8_t *c = uart_cdc_get_config_desc(NULL);
    /* ACM functional at offset 36. */
    ASSERT_EQ_INT(c[36], 0x04);  /* bFunctionLength */
    ASSERT_EQ_INT(c[37], 0x24);  /* CS_INTERFACE */
    ASSERT_EQ_INT(c[38], 0x02);  /* ABSTRACT_CONTROL_MANAGEMENT */
    ASSERT_EQ_INT(c[39], 0x06);  /* bmCapabilities */
}

/* -----------------------------------------------------------------------
 * String descriptor tests
 * -------------------------------------------------------------------- */

static void decode_utf16le(const uint8_t *desc, size_t desc_len,
                            char *out, size_t out_cap)
{
    size_t n = 0;
    for (size_t i = 2; i + 1 < desc_len && n + 1 < out_cap; i += 2) {
        out[n++] = (char)desc[i];
    }
    out[n] = '\0';
}

TEST(test_string_manufacturer_is_mpy_pod)
{
    size_t len = 0;
    const uint8_t *s = uart_cdc_get_string_desc(1, &len);
    ASSERT_TRUE(s != NULL, "iManufacturer missing");
    ASSERT_EQ_INT(s[1], 0x03);  /* STRING type */
    char buf[32] = {0};
    decode_utf16le(s, len, buf, sizeof(buf));
    ASSERT_TRUE(strcmp(buf, "mpy-pod") == 0, "iManufacturer='%s'", buf);
}

TEST(test_string_product_is_cdc_uart)
{
    size_t len = 0;
    const uint8_t *s = uart_cdc_get_string_desc(2, &len);
    ASSERT_TRUE(s != NULL, "iProduct missing");
    char buf[32] = {0};
    decode_utf16le(s, len, buf, sizeof(buf));
    ASSERT_TRUE(strcmp(buf, "mpy-pod CDC UART") == 0, "iProduct='%s'", buf);
}

/* -----------------------------------------------------------------------
 * Control transfer tests
 * -------------------------------------------------------------------- */

TEST(test_ctrl_get_device_descriptor)
{
    uint8_t buf[64] = {0};
    size_t  len = 0;
    int rc = do_ctrl(0x80, 0x06, 0x0100, 0, 18, NULL, 0, buf, sizeof(buf), &len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_EQ_INT(len, 18);
    ASSERT_EQ_INT(buf[1], 0x01);  /* DEVICE */
    ASSERT_EQ_INT(buf[4], 0xEF);  /* bDeviceClass */
}

TEST(test_ctrl_get_config_descriptor)
{
    uint8_t buf[128] = {0};
    size_t  len = 0;
    int rc = do_ctrl(0x80, 0x06, 0x0200, 0, 75, NULL, 0, buf, sizeof(buf), &len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_EQ_INT(len, 75);
    ASSERT_EQ_INT(buf[0], 0x09);  /* config header bLength */
    ASSERT_EQ_INT(buf[4], 0x02);  /* bNumInterfaces */
}

TEST(test_ctrl_set_line_coding_9600_7E2)
{
    /* attach first so s_uart_num is valid and apply_line_coding runs */
    uart_cdc_attach(2, -1, -1, 115200);
    uart_stub_reset();  /* clear param_config count from attach */

    /* 9600 baud, 7 data bits, even parity, 2 stop bits.
     * Wire format: dwDTERate=9600(LE), bCharFormat=2, bParityType=2, bDataBits=7 */
    uint8_t lc[7] = { 0x80, 0x25, 0x00, 0x00,  /* 9600 LE */
                       0x02,                     /* 2 stop bits */
                       0x02,                     /* even parity */
                       0x07 };                   /* 7 data bits */
    uint8_t dummy[1];
    size_t  dummy_len = 0;
    /* bmRequestType=0x21: class | interface | host-to-device */
    int rc = do_ctrl(0x21, 0x20, 0, 0, 7, lc, 7, dummy, sizeof(dummy), &dummy_len);
    ASSERT_EQ_INT(rc, 0);

    ASSERT_EQ_INT(g_uart_param_config_count, 1);
    ASSERT_EQ_INT(g_last_uart_config.baud_rate,  9600);
    ASSERT_EQ_INT(g_last_uart_config.stop_bits,  UART_STOP_BITS_2);
    ASSERT_EQ_INT(g_last_uart_config.parity,     UART_PARITY_EVEN);
    ASSERT_EQ_INT(g_last_uart_config.data_bits,  UART_DATA_7_BITS);
}

TEST(test_ctrl_get_line_coding_round_trip)
{
    /* Set a known line coding, then GET_LINE_CODING should return it. */
    uint8_t set_lc[7] = { 0x00, 0xC2, 0x01, 0x00,  /* 115200 */
                            0x00, 0x01, 0x08 };       /* 1stop, odd, 8bit */
    uint8_t dummy[1];
    size_t  dummy_len = 0;
    do_ctrl(0x21, 0x20, 0, 0, 7, set_lc, 7, dummy, sizeof(dummy), &dummy_len);

    uint8_t get_lc[7] = {0};
    size_t  get_len = 0;
    int rc = do_ctrl(0xA1, 0x21, 0, 0, 7, NULL, 0, get_lc, sizeof(get_lc), &get_len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_EQ_INT(get_len, 7);
    ASSERT_TRUE(memcmp(get_lc, set_lc, 7) == 0, "GET_LINE_CODING mismatch");
}

TEST(test_ctrl_set_control_line_state_succeeds)
{
    uint8_t dummy[1];
    size_t  dummy_len = 0;
    /* wValue = 0x03: DTR + RTS set */
    int rc = do_ctrl(0x21, 0x22, 0x0003, 0, 0, NULL, 0,
                     dummy, sizeof(dummy), &dummy_len);
    ASSERT_EQ_INT(rc, 0);
}

TEST(test_ctrl_unknown_request_stalls)
{
    uint8_t buf[8] = {0};
    size_t  len = 0;
    int rc = do_ctrl(0xC0, 0xAA, 0, 0, 0, NULL, 0, buf, sizeof(buf), &len);
    ASSERT_EQ_INT(rc, -EPIPE);
}

/* -----------------------------------------------------------------------
 * Data transfer tests
 * -------------------------------------------------------------------- */

TEST(test_data_ep_notify_in_returns_zlp)
{
    /* uart_cdc_attach needed so s_uart_num is valid. */
    uart_cdc_attach(2, -1, -1, 115200);

    virtual_device_t *dev = uart_cdc_get_device();
    uint8_t buf[16] = {0};
    size_t  len = 0;
    int rc = dev->ops->data_transfer(dev, UART_CDC_EP_NOTIFY_IN,
                                      NULL, 0, buf, sizeof(buf), &len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_EQ_INT(len, 0);
}

TEST(test_data_ep_bulk_out_writes_to_uart)
{
    uart_cdc_attach(2, -1, -1, 115200);
    uart_stub_reset();

    virtual_device_t *dev = uart_cdc_get_device();
    const uint8_t payload[] = "hello";
    size_t unused = 0;
    int rc = dev->ops->data_transfer(dev, UART_CDC_EP_DATA_OUT,
                                      payload, sizeof(payload) - 1,
                                      NULL, 0, &unused);
    ASSERT_EQ_INT(rc, 0);

    uint8_t captured[16] = {0};
    size_t  n = uart_stub_drain_tx(captured, sizeof(captured));
    ASSERT_EQ_INT(n, 5);
    ASSERT_TRUE(memcmp(captured, "hello", 5) == 0,
                "TX buffer='%.*s'", (int)n, captured);
}

TEST(test_data_ep_bulk_in_reads_from_uart)
{
    uart_cdc_attach(2, -1, -1, 115200);
    uart_stub_reset();

    const uint8_t inject[] = "world";
    uart_stub_inject_rx(inject, sizeof(inject) - 1);

    virtual_device_t *dev = uart_cdc_get_device();
    uint8_t in_buf[64] = {0};
    size_t  in_len = 0;
    int rc = dev->ops->data_transfer(dev, UART_CDC_EP_DATA_IN,
                                      NULL, 0, in_buf, sizeof(in_buf), &in_len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_EQ_INT(in_len, 5);
    ASSERT_TRUE(memcmp(in_buf, "world", 5) == 0,
                "RX buffer='%.*s'", (int)in_len, in_buf);
}

TEST(test_data_ep_bulk_in_returns_zlp_when_no_data)
{
    uart_cdc_attach(2, -1, -1, 115200);
    uart_stub_reset();  /* no injected RX bytes */

    virtual_device_t *dev = uart_cdc_get_device();
    uint8_t buf[64] = {0};
    size_t  len = 0;
    int rc = dev->ops->data_transfer(dev, UART_CDC_EP_DATA_IN,
                                      NULL, 0, buf, sizeof(buf), &len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_EQ_INT(len, 0);
}

TEST(test_data_unknown_endpoint_stalls)
{
    uart_cdc_attach(2, -1, -1, 115200);

    virtual_device_t *dev = uart_cdc_get_device();
    uint8_t buf[8] = {0};
    size_t  len = 0;
    int rc = dev->ops->data_transfer(dev, 0x84 /* non-existent EP4 IN */,
                                      NULL, 0, buf, sizeof(buf), &len);
    ASSERT_EQ_INT(rc, -EPIPE);
}

/* -----------------------------------------------------------------------
 * Virtual device registration
 * -------------------------------------------------------------------- */

TEST(test_attach_registers_device_in_registry)
{
    int rc = uart_cdc_attach(2, -1, -1, 115200);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_TRUE(uart_cdc_is_attached(), "is_attached should be true");
    ASSERT_EQ_INT(g_uart_install_count, 1);
}

TEST(test_detach_removes_uart_driver)
{
    uart_cdc_attach(2, -1, -1, 115200);
    uart_stub_reset();
    int rc = uart_cdc_detach();
    ASSERT_EQ_INT(rc, 0);
    ASSERT_TRUE(!uart_cdc_is_attached(), "is_attached should be false");
    ASSERT_EQ_INT(g_uart_delete_count, 1);
}

/* -----------------------------------------------------------------------
 * main
 * -------------------------------------------------------------------- */

int main(void)
{
    run_test_device_desc_length_and_class();
    run_test_config_desc_total_length_and_num_interfaces();
    run_test_config_desc_iad_fields();
    run_test_config_desc_union_functional_interface_numbers();
    run_test_config_desc_endpoint_addresses();
    run_test_config_desc_acm_capabilities();

    run_test_string_manufacturer_is_mpy_pod();
    run_test_string_product_is_cdc_uart();

    run_test_ctrl_get_device_descriptor();
    run_test_ctrl_get_config_descriptor();
    run_test_ctrl_set_line_coding_9600_7E2();
    run_test_ctrl_get_line_coding_round_trip();
    run_test_ctrl_set_control_line_state_succeeds();
    run_test_ctrl_unknown_request_stalls();

    run_test_data_ep_notify_in_returns_zlp();
    run_test_data_ep_bulk_out_writes_to_uart();
    run_test_data_ep_bulk_in_reads_from_uart();
    run_test_data_ep_bulk_in_returns_zlp_when_no_data();
    run_test_data_unknown_endpoint_stalls();

    run_test_attach_registers_device_in_registry();
    run_test_detach_removes_uart_driver();

    printf("\n%s: %d failure(s)\n",
           g_failures == 0 ? "PASS" : "FAIL", g_failures);
    return g_failures == 0 ? 0 : 1;
}
