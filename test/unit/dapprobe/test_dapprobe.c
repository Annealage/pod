/* Annealage Pod: WS-C unit tests for the dapprobe protocol layer.
 *
 * Run with `ctest`. Mirrors the test/unit/usbip/ harness shape: no
 * external test framework, just a TEST() macro plus assertion helpers.
 *
 * Coverage:
 *  - DAP_Info command parsing for vendor / product / fw / capabilities
 *    / packet-size / packet-count / SWO buffer size / serial.
 *  - Descriptor blob byte-for-byte expectations against research/
 *    usbip-multiplexing-design.md §2.
 *  - URB routing: control_transfer GET_DESCRIPTOR for device / config /
 *    string indices; data_transfer EP1 OUT -> EP2 IN command-response
 *    pairing; data_transfer EP3 IN cap at 60 bytes (probe-rs #448).
 *
 * The host build stubs the WS-D engine API (swd.h, swo.h) via
 * support/io_stubs.c. The vendored CMSIS-DAP `DAP.c` and `DAP_vendor.c`
 * compile cleanly against the local DAP_config.h port copy. ESP-IDF
 * symbols (ESP_LOG, esp_mac) are stubbed under !ESP_PLATFORM.
 */

#include "dap_core.h"
#include "synthetic_device.h"

#include "DAP_config.h"
#include "DAP.h"

#include "../../../src/c_modules/usbip/virtual_device.h"

#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_failures;
static const char *g_current_test = "<none>";

#define TEST(name) \
    static void name(void); \
    static void run_##name(void) { \
        g_current_test = #name; \
        printf("[RUN ] %s\n", #name); \
        size_t before = (size_t)g_failures; \
        name(); \
        if ((size_t)g_failures == before) { \
            printf("[ OK ] %s\n", #name); \
        } else { \
            printf("[FAIL] %s\n", #name); \
        } \
    } \
    static void name(void)

#define ASSERT_TRUE(cond, fmt, ...)                                              \
    do {                                                                          \
        if (!(cond)) {                                                            \
            fprintf(stderr, "%s: ASSERT_TRUE failed at %s:%d: " fmt "\n",         \
                    g_current_test, __FILE__, __LINE__, ##__VA_ARGS__);           \
            g_failures++;                                                         \
        }                                                                         \
    } while (0)

#define ASSERT_EQ_INT(a, b)                                                      \
    do {                                                                          \
        long long _a = (long long)(a);                                            \
        long long _b = (long long)(b);                                            \
        if (_a != _b) {                                                           \
            fprintf(stderr, "%s: ASSERT_EQ_INT failed at %s:%d: "                 \
                    "%s = %lld, %s = %lld\n",                                     \
                    g_current_test, __FILE__, __LINE__,                           \
                    #a, _a, #b, _b);                                              \
            g_failures++;                                                         \
        }                                                                         \
    } while (0)

#define ASSERT_EQ_BYTES(a, b, len)                                               \
    do {                                                                          \
        if (memcmp((a), (b), (len)) != 0) {                                       \
            fprintf(stderr, "%s: ASSERT_EQ_BYTES failed at %s:%d: "               \
                    "%s != %s (%zu bytes)\n",                                     \
                    g_current_test, __FILE__, __LINE__,                           \
                    #a, #b, (size_t)(len));                                       \
            g_failures++;                                                         \
        }                                                                         \
    } while (0)

/* ===================================================================
 * Descriptor-blob correctness
 * =================================================================*/

TEST(test_device_desc_layout)
{
    size_t len = 0;
    const uint8_t *d = synthetic_device_get_device_desc(&len);
    ASSERT_EQ_INT(len, 18);

    ASSERT_EQ_INT(d[0], 0x12);          /* bLength */
    ASSERT_EQ_INT(d[1], 0x01);          /* DEVICE */
    /* bcdUSB = 0x0210 (LE) so BOS / MS-OS-2.0 are advertised. */
    ASSERT_EQ_INT(d[2], 0x10);
    ASSERT_EQ_INT(d[3], 0x02);
    ASSERT_EQ_INT(d[4], 0xEF);          /* bDeviceClass: misc */
    ASSERT_EQ_INT(d[5], 0x02);          /* bDeviceSubClass: common */
    ASSERT_EQ_INT(d[6], 0x01);          /* bDeviceProtocol: IAD */
    ASSERT_EQ_INT(d[7], 0x40);          /* bMaxPacketSize0 */
    /* idVendor = 0xC251, idProduct = 0xF00A (LE). */
    ASSERT_EQ_INT(d[8], 0x51);
    ASSERT_EQ_INT(d[9], 0xC2);
    ASSERT_EQ_INT(d[10], 0x0A);
    ASSERT_EQ_INT(d[11], 0xF0);
    ASSERT_EQ_INT(d[14], 0x01);         /* iManufacturer */
    ASSERT_EQ_INT(d[15], 0x02);         /* iProduct */
    ASSERT_EQ_INT(d[16], 0x03);         /* iSerial */
    ASSERT_EQ_INT(d[17], 0x01);         /* bNumConfigurations */
}

TEST(test_config_desc_layout)
{
    size_t len = 0;
    const uint8_t *c = synthetic_device_get_config_desc(&len);
    ASSERT_EQ_INT(len, 39);             /* 9 + 9 + 3*7 */

    /* Config header. */
    ASSERT_EQ_INT(c[0], 0x09);
    ASSERT_EQ_INT(c[1], 0x02);
    ASSERT_EQ_INT(c[2], 0x27);          /* wTotalLength = 39 */
    ASSERT_EQ_INT(c[3], 0x00);
    ASSERT_EQ_INT(c[4], 0x01);          /* bNumInterfaces */
    ASSERT_EQ_INT(c[5], 0x01);          /* bConfigurationValue */

    /* Interface 0. */
    ASSERT_EQ_INT(c[9],  0x09);
    ASSERT_EQ_INT(c[10], 0x04);
    ASSERT_EQ_INT(c[11], 0x00);          /* bInterfaceNumber */
    ASSERT_EQ_INT(c[13], 0x03);          /* bNumEndpoints */
    ASSERT_EQ_INT(c[14], 0xFF);          /* bInterfaceClass */
    ASSERT_EQ_INT(c[15], 0x00);          /* bInterfaceSubClass */
    ASSERT_EQ_INT(c[16], 0x00);          /* bInterfaceProtocol */
    ASSERT_EQ_INT(c[17], 0x04);          /* iInterface = "CMSIS-DAP" */

    /* EP1 OUT. */
    ASSERT_EQ_INT(c[18], 0x07);
    ASSERT_EQ_INT(c[19], 0x05);
    ASSERT_EQ_INT(c[20], 0x01);          /* bEndpointAddress = OUT EP1 */
    ASSERT_EQ_INT(c[21], 0x02);          /* Bulk */
    ASSERT_EQ_INT(c[22], 0x40);          /* MaxPacket = 64 */

    /* EP2 IN. */
    ASSERT_EQ_INT(c[27], 0x82);          /* IN EP2 */
    ASSERT_EQ_INT(c[28], 0x02);

    /* EP3 IN (SWO). */
    ASSERT_EQ_INT(c[34], 0x83);          /* IN EP3 */
    ASSERT_EQ_INT(c[35], 0x02);
}

TEST(test_string_desc_iinterface_is_cmsis_dap)
{
    /* The load-bearing string: pyOCD and probe-rs both match on
     * iInterface containing "CMSIS-DAP". String index 4. */
    size_t len = 0;
    const uint8_t *s = synthetic_device_get_string_desc(4, &len);
    ASSERT_TRUE(s != NULL, "iInterface descriptor not populated");
    ASSERT_EQ_INT(s[0], len);            /* bLength */
    ASSERT_EQ_INT(s[1], 0x03);           /* STRING type */

    /* Decode UTF-16LE back to ASCII for verification. */
    char ascii[32] = {0};
    size_t out = 0;
    for (size_t i = 2; i + 1 < len && out + 1 < sizeof(ascii); i += 2) {
        ascii[out++] = (char)s[i];
    }
    ascii[out] = '\0';
    ASSERT_TRUE(strcmp(ascii, "CMSIS-DAP") == 0,
                "iInterface = '%s' (want 'CMSIS-DAP')", ascii);
}

TEST(test_string_desc_manufacturer_and_product)
{
    char buf[64] = {0};
    size_t len = 0;
    const uint8_t *s;

    s = synthetic_device_get_string_desc(1, &len);
    ASSERT_TRUE(s != NULL, "iManufacturer missing");
    size_t out = 0;
    for (size_t i = 2; i + 1 < len && out + 1 < sizeof(buf); i += 2) buf[out++] = (char)s[i];
    buf[out] = '\0';
    ASSERT_TRUE(strcmp(buf, "mpy-pod") == 0, "iManufacturer = '%s'", buf);

    memset(buf, 0, sizeof(buf));
    s = synthetic_device_get_string_desc(2, &len);
    ASSERT_TRUE(s != NULL, "iProduct missing");
    out = 0;
    for (size_t i = 2; i + 1 < len && out + 1 < sizeof(buf); i += 2) buf[out++] = (char)s[i];
    buf[out] = '\0';
    ASSERT_TRUE(strcmp(buf, "mpy-pod synthetic CMSIS-DAP") == 0,
                "iProduct = '%s'", buf);
}

/* ===================================================================
 * DAP_Info command parsing (uses dap_core_process)
 * =================================================================*/

TEST(test_dap_info_vendor_returns_mpy_pod)
{
    ASSERT_EQ_INT(dap_core_init(), 0);

    uint8_t req[2] = { 0x00 /* ID_DAP_Info */, 0x01 /* DAP_ID_VENDOR */ };
    uint8_t resp[64] = {0};
    size_t n = dap_core_process(req, sizeof(req), resp, sizeof(resp));

    /* Response: [ID_DAP_Info=0x00] [length] [string...] */
    ASSERT_TRUE(n >= 3, "dap_core_process returned only %zu bytes", n);
    ASSERT_EQ_INT(resp[0], 0x00);
    /* Length includes the NUL terminator. */
    ASSERT_TRUE(resp[1] == strlen("mpy-pod") + 1,
                "vendor length = %u (want %u)", resp[1],
                (unsigned)(strlen("mpy-pod") + 1));
    ASSERT_TRUE(strcmp((char *)&resp[2], "mpy-pod") == 0,
                "vendor string = '%s'", (char *)&resp[2]);
}

TEST(test_dap_info_product_returns_synthetic_cmsis_dap)
{
    ASSERT_EQ_INT(dap_core_init(), 0);

    uint8_t req[2] = { 0x00, 0x02 /* DAP_ID_PRODUCT */ };
    uint8_t resp[64] = {0};
    size_t n = dap_core_process(req, sizeof(req), resp, sizeof(resp));

    ASSERT_TRUE(n >= 3, "short response %zu", n);
    ASSERT_EQ_INT(resp[0], 0x00);
    ASSERT_TRUE(strcmp((char *)&resp[2], "mpy-pod synthetic CMSIS-DAP") == 0,
                "product string = '%s'", (char *)&resp[2]);
}

TEST(test_dap_info_capabilities_bit_flags)
{
    ASSERT_EQ_INT(dap_core_init(), 0);

    uint8_t req[2] = { 0x00, 0xF0 /* DAP_ID_CAPABILITIES */ };
    uint8_t resp[8] = {0};
    size_t n = dap_core_process(req, sizeof(req), resp, sizeof(resp));

    ASSERT_TRUE(n >= 4, "capabilities response too short: %zu", n);
    ASSERT_EQ_INT(resp[0], 0x00);  /* echo of ID_DAP_Info */
    ASSERT_EQ_INT(resp[1], 0x02);  /* length = 2 capability bytes */
    /* Capability byte 0:
     *   bit0 = SWD     (1)
     *   bit1 = JTAG    (0; DAP_JTAG=0)
     *   bit2 = SWO_UART(1)
     *   bit3 = SWO_MAN (0)
     *   bit4 = atomic  (1)
     *   bit5 = timestamp (0; TIMESTAMP_CLOCK=0)
     *   bit6 = SWO_STREAM (0)
     *   bit7 = DAP_UART (0)
     */
    ASSERT_EQ_INT(resp[2] & 0x01, 0x01);  /* SWD set */
    ASSERT_EQ_INT(resp[2] & 0x02, 0x00);  /* JTAG clear */
    ASSERT_EQ_INT(resp[2] & 0x04, 0x04);  /* SWO_UART set */
    ASSERT_EQ_INT(resp[2] & 0x08, 0x00);  /* SWO Manchester clear */
    ASSERT_EQ_INT(resp[2] & 0x10, 0x10);  /* atomic commands set */
    ASSERT_EQ_INT(resp[2] & 0x20, 0x00);  /* timestamp clear */
    ASSERT_EQ_INT(resp[2] & 0x80, 0x00);  /* DAP_UART clear */
}

TEST(test_dap_info_packet_size)
{
    ASSERT_EQ_INT(dap_core_init(), 0);

    uint8_t req[2] = { 0x00, 0xFF /* DAP_ID_PACKET_SIZE */ };
    uint8_t resp[8] = {0};
    size_t n = dap_core_process(req, sizeof(req), resp, sizeof(resp));

    ASSERT_TRUE(n >= 4, "short response %zu", n);
    ASSERT_EQ_INT(resp[1], 0x02);   /* length = 2 */
    /* DAP_PACKET_SIZE = 512 (LE). */
    ASSERT_EQ_INT(resp[2], 0x00);
    ASSERT_EQ_INT(resp[3], 0x02);
}

TEST(test_dap_info_swo_buffer_size)
{
    ASSERT_EQ_INT(dap_core_init(), 0);

    uint8_t req[2] = { 0x00, 0xFD /* DAP_ID_SWO_BUFFER_SIZE */ };
    uint8_t resp[8] = {0};
    size_t n = dap_core_process(req, sizeof(req), resp, sizeof(resp));

    ASSERT_TRUE(n >= 6, "short response %zu", n);
    ASSERT_EQ_INT(resp[1], 0x04);   /* length = 4 */
    /* SWO_BUFFER_SIZE = 8 MiB = 0x800000 (LE) */
    ASSERT_EQ_INT(resp[2], 0x00);
    ASSERT_EQ_INT(resp[3], 0x00);
    ASSERT_EQ_INT(resp[4], 0x80);
    ASSERT_EQ_INT(resp[5], 0x00);
}

TEST(test_dap_info_serial_is_12_hex)
{
    ASSERT_EQ_INT(dap_core_init(), 0);

    uint8_t req[2] = { 0x00, 0x03 /* DAP_ID_SER_NUM */ };
    uint8_t resp[64] = {0};
    size_t n = dap_core_process(req, sizeof(req), resp, sizeof(resp));

    ASSERT_TRUE(n >= 3, "short response %zu", n);
    ASSERT_EQ_INT(resp[0], 0x00);
    /* Serial is 12 hex digits + NUL on both real and host paths. */
    ASSERT_EQ_INT(resp[1], 13);
    for (size_t i = 0; i < 12; i++) {
        char c = (char)resp[2 + i];
        ASSERT_TRUE((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'),
                    "serial[%zu] = '%c' is not hex", i, c);
    }
}

/* ===================================================================
 * URB routing tests via virtual_device_t ops
 * =================================================================*/

TEST(test_control_transfer_get_device_descriptor)
{
    virtual_device_t *dev = synthetic_device_get();
    ASSERT_TRUE(dev != NULL, "synthetic_device_get returned NULL");
    ASSERT_TRUE(dev->ops != NULL, "ops missing");

    /* GET_DESCRIPTOR(device): bmRequestType=0x80, bRequest=0x06,
     *   wValue = (DEVICE<<8 | 0) = 0x0100, wIndex=0, wLength=18. */
    usbip_setup_packet_t setup = {
        .bmRequestType = 0x80,
        .bRequest      = 0x06,
        .wValue        = 0x0100,
        .wIndex        = 0,
        .wLength       = 18,
    };
    uint8_t in_buf[64] = {0};
    size_t  in_len = 0;
    int rc = dev->ops->control_transfer(dev, &setup, NULL, 0,
                                         in_buf, sizeof(in_buf), &in_len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_EQ_INT(in_len, 18);
    ASSERT_EQ_INT(in_buf[0], 0x12);
    ASSERT_EQ_INT(in_buf[1], 0x01);
}

TEST(test_control_transfer_get_config_descriptor)
{
    virtual_device_t *dev = synthetic_device_get();

    usbip_setup_packet_t setup = {
        .bmRequestType = 0x80,
        .bRequest      = 0x06,
        .wValue        = 0x0200,             /* CONFIGURATION descriptor */
        .wIndex        = 0,
        .wLength       = 39,
    };
    uint8_t in_buf[64] = {0};
    size_t  in_len = 0;
    int rc = dev->ops->control_transfer(dev, &setup, NULL, 0,
                                         in_buf, sizeof(in_buf), &in_len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_EQ_INT(in_len, 39);
    ASSERT_EQ_INT(in_buf[0], 0x09);
    ASSERT_EQ_INT(in_buf[1], 0x02);
    ASSERT_EQ_INT(in_buf[2], 0x27);          /* wTotalLength */
}

TEST(test_control_transfer_get_string_descriptor_iinterface)
{
    virtual_device_t *dev = synthetic_device_get();

    usbip_setup_packet_t setup = {
        .bmRequestType = 0x80,
        .bRequest      = 0x06,
        .wValue        = 0x0304,             /* STRING type, index 4 */
        .wIndex        = 0x0409,
        .wLength       = 64,
    };
    uint8_t in_buf[64] = {0};
    size_t  in_len = 0;
    int rc = dev->ops->control_transfer(dev, &setup, NULL, 0,
                                         in_buf, sizeof(in_buf), &in_len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_TRUE(in_len > 2, "iInterface descriptor empty");
    /* "CMSIS-DAP" in UTF-16LE has 9 chars, total length = 2 + 18 = 20. */
    ASSERT_EQ_INT(in_buf[0], 20);
    ASSERT_EQ_INT(in_buf[1], 0x03);
    /* First char 'C'. */
    ASSERT_EQ_INT(in_buf[2], 'C');
    ASSERT_EQ_INT(in_buf[3], 0x00);
}

TEST(test_control_transfer_unknown_request_stalls)
{
    virtual_device_t *dev = synthetic_device_get();

    usbip_setup_packet_t setup = {
        .bmRequestType = 0xC0,                /* vendor IN */
        .bRequest      = 0xAA,
        .wValue        = 0,
        .wIndex        = 0,
        .wLength       = 0,
    };
    uint8_t in_buf[8] = {0};
    size_t  in_len = 0;
    int rc = dev->ops->control_transfer(dev, &setup, NULL, 0,
                                         in_buf, sizeof(in_buf), &in_len);
    ASSERT_EQ_INT(rc, -EPIPE);
}

TEST(test_data_transfer_ep1_then_ep2_round_trip)
{
    /* Issue a DAP_Info(VENDOR) command via EP1 OUT, then drain the
     * matching response via EP2 IN. */
    ASSERT_EQ_INT(dap_core_init(), 0);
    virtual_device_t *dev = synthetic_device_get();

    /* Reset internal buffers via on_attach. */
    if (dev->ops->on_attach) { (void)dev->ops->on_attach(dev); }

    uint8_t cmd[2] = { 0x00, 0x01 /* DAP_ID_VENDOR */ };
    size_t  unused_in = 0;
    int rc = dev->ops->data_transfer(dev, 0x01 /* EP1 OUT */,
                                      cmd, sizeof(cmd),
                                      NULL, 0, &unused_in);
    ASSERT_EQ_INT(rc, 0);

    uint8_t in_buf[64] = {0};
    size_t  in_len = 0;
    rc = dev->ops->data_transfer(dev, 0x82 /* EP2 IN */,
                                  NULL, 0,
                                  in_buf, sizeof(in_buf), &in_len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_TRUE(in_len >= 3, "EP2 IN gave only %zu bytes", in_len);
    ASSERT_EQ_INT(in_buf[0], 0x00);
    ASSERT_TRUE(strcmp((char *)&in_buf[2], "mpy-pod") == 0,
                "EP2 vendor = '%s'", (char *)&in_buf[2]);
}

TEST(test_data_transfer_ep3_swo_caps_at_60_bytes)
{
    /* The probe-rs #448 ZLP cap requires every SWO Bulk-IN completion
     * to be < 64 bytes. We cap at 60 even when the host requests more. */
    virtual_device_t *dev = synthetic_device_get();

    uint8_t in_buf[256];
    memset(in_buf, 0xCC, sizeof(in_buf));
    size_t in_len = 0;
    int rc = dev->ops->data_transfer(dev, 0x83 /* EP3 IN */,
                                      NULL, 0,
                                      in_buf, sizeof(in_buf), &in_len);
    ASSERT_EQ_INT(rc, 0);
    ASSERT_TRUE(in_len <= 60, "EP3 IN returned %zu bytes (cap is 60)", in_len);
}

TEST(test_data_transfer_unknown_endpoint_stalls)
{
    virtual_device_t *dev = synthetic_device_get();

    uint8_t in_buf[8] = {0};
    size_t  in_len = 0;
    /* EP4 IN does not exist on this device. */
    int rc = dev->ops->data_transfer(dev, 0x84,
                                      NULL, 0,
                                      in_buf, sizeof(in_buf), &in_len);
    ASSERT_EQ_INT(rc, -EPIPE);
}

/* ===================================================================
 * Telemetry surface
 * =================================================================*/

TEST(test_telemetry_initialised_flag)
{
    ASSERT_EQ_INT(dap_core_init(), 0);

    dap_core_telemetry_t t = {0};
    dap_core_telemetry(&t);
    ASSERT_TRUE(t.initialised, "expected initialised=true");
}

/* ===================================================================
 * main
 * =================================================================*/

int main(void)
{
    run_test_device_desc_layout();
    run_test_config_desc_layout();
    run_test_string_desc_iinterface_is_cmsis_dap();
    run_test_string_desc_manufacturer_and_product();

    run_test_dap_info_vendor_returns_mpy_pod();
    run_test_dap_info_product_returns_synthetic_cmsis_dap();
    run_test_dap_info_capabilities_bit_flags();
    run_test_dap_info_packet_size();
    run_test_dap_info_swo_buffer_size();
    run_test_dap_info_serial_is_12_hex();

    run_test_control_transfer_get_device_descriptor();
    run_test_control_transfer_get_config_descriptor();
    run_test_control_transfer_get_string_descriptor_iinterface();
    run_test_control_transfer_unknown_request_stalls();

    run_test_data_transfer_ep1_then_ep2_round_trip();
    run_test_data_transfer_ep3_swo_caps_at_60_bytes();
    run_test_data_transfer_unknown_endpoint_stalls();

    run_test_telemetry_initialised_flag();

    if (g_failures == 0) {
        printf("\nAll tests passed.\n");
        return 0;
    }
    printf("\n%d failure(s).\n", g_failures);
    return 1;
}
