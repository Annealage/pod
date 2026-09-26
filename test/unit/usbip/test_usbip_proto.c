/* Annealage Pod: WS-A unit tests for the host-portable USB/IP
 * protocol parser and the virtual-device registry.
 *
 * Run with `ctest`. The harness deliberately uses no external
 * dependencies (no Unity, no Cmocka). Each TEST() macro asserts
 * with a clear message and increments a counter; the binary exits
 * with the failure count so CTest reads the right status.
 */

#include "usbip_proto.h"
#include "usbip_protocol.h"
#include "virtual_device.h"

#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int g_failures;
static const char *g_current_test = "<none>";

/* Forward declaration; definition near end of file. */
static uint32_t htobe32_local(uint32_t x);

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

/* Helper: fill a `usbip_dev_record_t` with the synthetic CMSIS-DAP
 * fields used by docs/design/usbip-server.md and the design doc
 * §1.1.1. The registry assigns busid/path on register; tests fill
 * the rest. */
static void make_synthetic_dap(usbip_dev_record_t *r)
{
    memset(r, 0, sizeof(*r));
    r->present              = true;
    /* path is overwritten by registry. */
    r->busnum               = 2;
    r->devnum               = 1;
    r->speed                = 2; /* USB_SPEED_FULL */
    r->id_vendor            = 0xC251;
    r->id_product           = 0xF00A;
    r->bcd_device           = 0x0100;
    r->device_class         = 0xEF;
    r->device_subclass      = 0x02;
    r->device_protocol      = 0x01;
    r->configuration_value  = 1;
    r->num_configurations   = 1;
    r->num_interfaces       = 1;
    r->interfaces[0].interface_class    = 0xFF;
    r->interfaces[0].interface_subclass = 0x00;
    r->interfaces[0].interface_protocol = 0x00;
}

/* ---------- protocol tests ---------- */

TEST(test_op_common_pack_unpack)
{
    usbip_op_common_t out;
    usbip_proto_pack_op_common(&out, USBIP_OP_REP_DEVLIST, 0);

    /* Verify field-by-field byte order. */
    const uint8_t expected[8] = { 0x01, 0x11, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00 };
    ASSERT_EQ_BYTES(&out, expected, 8);

    uint16_t version = 0, code = 0;
    uint32_t status = 0;
    bool ok = usbip_proto_unpack_op_common(&out, &version, &code, &status);
    ASSERT_TRUE(ok, "unpack_op_common returned false");
    ASSERT_EQ_INT(version, USBIP_VERSION);
    ASSERT_EQ_INT(code, USBIP_OP_REP_DEVLIST);
    ASSERT_EQ_INT(status, 0u);
}

TEST(test_op_common_unpack_request)
{
    /* Real DEVLIST request bytes from a Linux usbip client. */
    const uint8_t req[8] = { 0x01, 0x11, 0x80, 0x05, 0x00, 0x00, 0x00, 0x00 };
    uint16_t version = 0, code = 0;
    uint32_t status = 0xdeadbeef;
    bool ok = usbip_proto_unpack_op_common((const usbip_op_common_t *)req,
                                            &version, &code, &status);
    ASSERT_TRUE(ok, "unpack_op_common returned false");
    ASSERT_EQ_INT(version, USBIP_VERSION);
    ASSERT_EQ_INT(code, USBIP_OP_REQ_DEVLIST);
    ASSERT_EQ_INT(status, 0u);
}

TEST(test_device_desc_size)
{
    /* Layout of the device descriptor on the wire is precisely 0x138
     * bytes; mismatch breaks Linux usbip_attach. */
    ASSERT_EQ_INT(sizeof(usbip_usb_device_t), 0x138);
    ASSERT_EQ_INT(sizeof(usbip_op_common_t), 8);
    ASSERT_EQ_INT(sizeof(usbip_header_t), 48);
    ASSERT_EQ_INT(sizeof(usbip_usb_interface_t), 4);
}

TEST(test_pack_device_desc)
{
    usbip_dev_record_t src;
    make_synthetic_dap(&src);
    snprintf(src.path,  sizeof(src.path),  "/sys/devices/platform/annealage_pod/usb2/2-1");
    snprintf(src.busid, sizeof(src.busid), "2-1");

    usbip_usb_device_t wire;
    usbip_proto_pack_device_desc(&src, &wire);

    /* Path/busid copied verbatim. */
    ASSERT_EQ_INT(wire.path[0], '/');
    ASSERT_EQ_INT(wire.busid[0], '2');
    ASSERT_EQ_INT(wire.busid[1], '-');
    ASSERT_EQ_INT(wire.busid[2], '1');
    ASSERT_EQ_INT(wire.busid[3], 0);

    /* Numeric fields converted to network byte order. */
    const uint8_t *p = (const uint8_t *)&wire.busnum;
    ASSERT_EQ_INT(p[0], 0); ASSERT_EQ_INT(p[1], 0);
    ASSERT_EQ_INT(p[2], 0); ASSERT_EQ_INT(p[3], 2);

    p = (const uint8_t *)&wire.idVendor;
    ASSERT_EQ_INT(p[0], 0xC2); ASSERT_EQ_INT(p[1], 0x51);

    p = (const uint8_t *)&wire.idProduct;
    ASSERT_EQ_INT(p[0], 0xF0); ASSERT_EQ_INT(p[1], 0x0A);

    /* 1-byte fields untouched. */
    ASSERT_EQ_INT(wire.bDeviceClass, 0xEF);
    ASSERT_EQ_INT(wire.bNumInterfaces, 1);
}

TEST(test_pack_interface_desc)
{
    usbip_dev_record_t src;
    make_synthetic_dap(&src);
    usbip_usb_interface_t iface;

    bool ok = usbip_proto_pack_interface_desc(&src, 0, &iface);
    ASSERT_TRUE(ok, "pack_interface_desc(0) returned false");
    ASSERT_EQ_INT(iface.bInterfaceClass, 0xFF);
    ASSERT_EQ_INT(iface.bInterfaceSubClass, 0x00);
    ASSERT_EQ_INT(iface.bInterfaceProtocol, 0x00);
    ASSERT_EQ_INT(iface.padding, 0);

    /* Out-of-range index. */
    ok = usbip_proto_pack_interface_desc(&src, 1, &iface);
    ASSERT_TRUE(!ok, "pack_interface_desc(1) should fail when num_interfaces==1");
}

TEST(test_make_devid)
{
    usbip_dev_record_t src;
    make_synthetic_dap(&src);
    src.busnum = 2;
    src.devnum = 1;
    ASSERT_EQ_INT(usbip_proto_make_devid(&src), 0x00020001u);
    src.busnum = 1;
    src.devnum = 5;
    ASSERT_EQ_INT(usbip_proto_make_devid(&src), 0x00010005u);
}

TEST(test_unpack_header_submit)
{
    /* Build a CMD_SUBMIT header by hand and roundtrip it. */
    usbip_header_t raw;
    memset(&raw, 0, sizeof(raw));
    /* command = USBIP_CMD_SUBMIT (1), seqnum = 0x12345678,
     * devid = 0x00020001, direction = USBIP_DIR_IN, ep = 2 */
    raw.base.command   = htobe32_local(USBIP_CMD_SUBMIT);
    raw.base.seqnum    = htobe32_local(0x12345678);
    raw.base.devid     = htobe32_local(0x00020001);
    raw.base.direction = htobe32_local(USBIP_DIR_IN);
    raw.base.ep        = htobe32_local(2);
    raw.u.cmd_submit.transfer_buffer_length = htobe32_local(64);
    raw.u.cmd_submit.number_of_packets = htobe32_local(USBIP_NUMBER_OF_PACKETS_NON_ISO);

    usbip_decoded_header_t hdr;
    usbip_proto_unpack_header(&raw, &hdr);
    ASSERT_EQ_INT(hdr.command, USBIP_CMD_SUBMIT);
    ASSERT_EQ_INT(hdr.seqnum, 0x12345678u);
    ASSERT_EQ_INT(hdr.devid, 0x00020001u);
    ASSERT_EQ_INT(hdr.direction, USBIP_DIR_IN);
    ASSERT_EQ_INT(hdr.ep, 2u);
    ASSERT_EQ_INT(hdr.transfer_buffer_length, 64);
    ASSERT_EQ_INT((uint32_t)hdr.number_of_packets, USBIP_NUMBER_OF_PACKETS_NON_ISO);

    int v = usbip_proto_validate_submit(&hdr, 16 * 1024);
    ASSERT_EQ_INT(v, 0);
}

TEST(test_validate_submit_too_large)
{
    usbip_decoded_header_t hdr = {0};
    hdr.direction = USBIP_DIR_IN;
    hdr.transfer_buffer_length = 32 * 1024;
    hdr.number_of_packets = (int32_t)USBIP_NUMBER_OF_PACKETS_NON_ISO;
    int v = usbip_proto_validate_submit(&hdr, 16 * 1024);
    ASSERT_EQ_INT(v, -EMSGSIZE);
}

TEST(test_validate_submit_negative_length)
{
    usbip_decoded_header_t hdr = {0};
    hdr.direction = USBIP_DIR_IN;
    hdr.transfer_buffer_length = -1;
    hdr.number_of_packets = (int32_t)USBIP_NUMBER_OF_PACKETS_NON_ISO;
    int v = usbip_proto_validate_submit(&hdr, 16 * 1024);
    ASSERT_EQ_INT(v, -EINVAL);
}

TEST(test_validate_submit_iso_unsupported)
{
    usbip_decoded_header_t hdr = {0};
    hdr.direction = USBIP_DIR_IN;
    hdr.transfer_buffer_length = 64;
    hdr.number_of_packets = 4;
    int v = usbip_proto_validate_submit(&hdr, 16 * 1024);
    ASSERT_EQ_INT(v, -EOPNOTSUPP);
}

TEST(test_validate_submit_zero_packets_ok)
{
    /* Some clients send number_of_packets = 0 instead of the
     * kernel's 0xFFFFFFFF tag. Both must be accepted. */
    usbip_decoded_header_t hdr = {0};
    hdr.direction = USBIP_DIR_IN;
    hdr.transfer_buffer_length = 0;
    hdr.number_of_packets = 0;
    ASSERT_EQ_INT(usbip_proto_validate_submit(&hdr, 16 * 1024), 0);
}

TEST(test_validate_submit_bad_direction)
{
    usbip_decoded_header_t hdr = {0};
    hdr.direction = 5;
    hdr.transfer_buffer_length = 0;
    hdr.number_of_packets = (int32_t)USBIP_NUMBER_OF_PACKETS_NON_ISO;
    ASSERT_EQ_INT(usbip_proto_validate_submit(&hdr, 16 * 1024), -EINVAL);
}

TEST(test_validate_submit_bad_ep)
{
    /* ep field is a wire uint32; USB-2.0 endpoints are 4-bit.
     * Reject anything above 15 so it can't cast-truncate into the
     * lane-index fold or alias a different endpoint. */
    usbip_decoded_header_t hdr = {0};
    hdr.direction = USBIP_DIR_IN;
    hdr.ep = 16;
    hdr.transfer_buffer_length = 0;
    hdr.number_of_packets = (int32_t)USBIP_NUMBER_OF_PACKETS_NON_ISO;
    ASSERT_EQ_INT(usbip_proto_validate_submit(&hdr, 16 * 1024), -EINVAL);

    hdr.ep = 0xDEADBEEFu;
    ASSERT_EQ_INT(usbip_proto_validate_submit(&hdr, 16 * 1024), -EINVAL);

    hdr.ep = 15u;
    ASSERT_EQ_INT(usbip_proto_validate_submit(&hdr, 16 * 1024), 0);
}

TEST(test_pack_ret_submit_byteorder)
{
    usbip_header_t raw;
    usbip_proto_pack_ret_submit(&raw,
                                /*seqnum*/   0xAABBCCDD,
                                /*devid*/    0x00020001,
                                /*direction*/USBIP_DIR_IN,
                                /*ep*/       2,
                                /*status*/   0,
                                /*actual*/   16);

    /* Verify command field is RET_SUBMIT in big-endian. */
    const uint8_t *p = (const uint8_t *)&raw.base.command;
    ASSERT_EQ_INT(p[0], 0); ASSERT_EQ_INT(p[1], 0);
    ASSERT_EQ_INT(p[2], 0); ASSERT_EQ_INT(p[3], 3);

    /* Verify actual_length = 16 in big-endian. */
    p = (const uint8_t *)&raw.u.ret_submit.actual_length;
    ASSERT_EQ_INT(p[0], 0); ASSERT_EQ_INT(p[1], 0);
    ASSERT_EQ_INT(p[2], 0); ASSERT_EQ_INT(p[3], 16);

    /* For non-iso traffic (all mpy-pod URBs are non-iso), the kernel
     * requires number_of_packets = USBIP_NUMBER_OF_PACKETS_NON_ISO (0xFFFFFFFF).
     * Zero here makes vhci_rx interpret the URB as isochronous and trip
     * "vhci_device speed not set" on every RET_SUBMIT. */
    p = (const uint8_t *)&raw.u.ret_submit.number_of_packets;
    ASSERT_EQ_INT(p[0], 0xff); ASSERT_EQ_INT(p[1], 0xff);
    ASSERT_EQ_INT(p[2], 0xff); ASSERT_EQ_INT(p[3], 0xff);
}

TEST(test_pack_ret_submit_negative_status)
{
    /* EPIPE on Linux is 32; the wire status carries -EPIPE in
     * two's complement big-endian. */
    usbip_header_t raw;
    usbip_proto_pack_ret_submit(&raw, 1, 1, USBIP_DIR_IN, 1,
                                /*status*/-EPIPE, 0);
    const uint8_t *p = (const uint8_t *)&raw.u.ret_submit.status;
    /* Reconstruct the wire-order int32 and compare numerically. */
    int32_t reconstructed = (int32_t)((uint32_t)p[0] << 24 |
                                      (uint32_t)p[1] << 16 |
                                      (uint32_t)p[2] << 8  |
                                      (uint32_t)p[3]);
    ASSERT_EQ_INT(reconstructed, -EPIPE);
}

TEST(test_pack_ret_unlink)
{
    usbip_header_t raw;
    usbip_proto_pack_ret_unlink(&raw, 7, 0x00020001, USBIP_DIR_IN, 1, 0);
    const uint8_t *p = (const uint8_t *)&raw.base.command;
    ASSERT_EQ_INT(p[3], 4); /* USBIP_RET_UNLINK = 4 */
    p = (const uint8_t *)&raw.base.seqnum;
    ASSERT_EQ_INT(p[3], 7);
}

/* ---------- virtual_device registry tests ---------- */

static int g_attach_calls;
static int g_detach_calls;
static int g_ctrl_calls;
static int g_data_calls;
static uint8_t g_last_ep;

static int test_control(virtual_device_t *dev,
                        const usbip_setup_packet_t *setup,
                        const uint8_t *out_data, size_t out_len,
                        uint8_t *in_data, size_t in_capacity, size_t *in_len)
{
    (void)dev; (void)out_data; (void)out_len;
    g_ctrl_calls++;
    /* Echo wValue into the IN buffer if there is room. */
    if (in_capacity >= 2) {
        in_data[0] = setup->wValue & 0xFF;
        in_data[1] = (setup->wValue >> 8) & 0xFF;
        *in_len = 2;
    } else {
        *in_len = 0;
    }
    return 0;
}

static int test_data(virtual_device_t *dev, uint8_t ep_addr,
                     const uint8_t *out_data, size_t out_len,
                     uint8_t *in_data, size_t in_capacity, size_t *in_len)
{
    (void)dev; (void)out_data; (void)out_len;
    (void)in_data; (void)in_capacity;
    g_data_calls++;
    g_last_ep = ep_addr;
    *in_len = 0;
    return 0;
}

static int test_attach(virtual_device_t *dev)
{
    (void)dev;
    g_attach_calls++;
    return 0;
}

static void test_detach(virtual_device_t *dev)
{
    (void)dev;
    g_detach_calls++;
}

static const virtual_device_ops_t s_ops = {
    .control_transfer = test_control,
    .data_transfer    = test_data,
    .on_attach        = test_attach,
    .on_detach        = test_detach,
};

TEST(test_register_assigns_busid)
{
    usbip_virtual_device_reset_for_test();

    virtual_device_t dev = {0};
    dev.ops = &s_ops;
    make_synthetic_dap(&dev.desc);

    int rc = usbip_register_virtual_device(&dev);
    ASSERT_EQ_INT(rc, 0);

    /* Registry assigns "2-1" for the first synthetic device. */
    ASSERT_EQ_INT(dev.desc.busnum, USBIP_VIRTUAL_DEVICE_BUSNUM);
    ASSERT_EQ_INT(dev.desc.devnum, 1);
    ASSERT_TRUE(strcmp(dev.desc.busid, "2-1") == 0,
                "busid '%s' != expected '2-1'", dev.desc.busid);
    ASSERT_TRUE(dev.desc.path[0] != '\0', "path was not auto-populated");
}

TEST(test_register_two_synthetic_devices)
{
    usbip_virtual_device_reset_for_test();

    virtual_device_t dev1 = {0};
    virtual_device_t dev2 = {0};
    dev1.ops = &s_ops;
    dev2.ops = &s_ops;
    make_synthetic_dap(&dev1.desc);
    make_synthetic_dap(&dev2.desc);

    ASSERT_EQ_INT(usbip_register_virtual_device(&dev1), 0);
    ASSERT_EQ_INT(usbip_register_virtual_device(&dev2), 0);

    ASSERT_TRUE(strcmp(dev1.desc.busid, "2-1") == 0,
                "dev1 busid '%s' != '2-1'", dev1.desc.busid);
    ASSERT_TRUE(strcmp(dev2.desc.busid, "2-2") == 0,
                "dev2 busid '%s' != '2-2'", dev2.desc.busid);
    ASSERT_EQ_INT(usbip_virtual_device_count(), 2);
}

TEST(test_find_by_busid_padding)
{
    usbip_virtual_device_reset_for_test();

    virtual_device_t dev = {0};
    dev.ops = &s_ops;
    make_synthetic_dap(&dev.desc);
    usbip_register_virtual_device(&dev);

    /* Lookup with NUL-padded 32-byte buffer (matches wire format). */
    char busid[USBIP_BUSID_SIZE] = {0};
    snprintf(busid, sizeof(busid), "2-1");

    virtual_device_t *found = usbip_find_virtual_device(busid);
    ASSERT_TRUE(found == &dev, "find_by_busid returned %p, expected %p",
                (void *)found, (void *)&dev);

    /* Mismatched busid returns NULL. */
    char other[USBIP_BUSID_SIZE] = {0};
    snprintf(other, sizeof(other), "1-1");
    ASSERT_TRUE(usbip_find_virtual_device(other) == NULL,
                "find_by_busid('1-1') should be NULL");
}

TEST(test_register_invalid)
{
    usbip_virtual_device_reset_for_test();

    /* NULL ops -> EINVAL. */
    virtual_device_t bad = {0};
    int rc = usbip_register_virtual_device(&bad);
    ASSERT_EQ_INT(rc, -EINVAL);
}

TEST(test_register_full)
{
    usbip_virtual_device_reset_for_test();

    virtual_device_t devs[USBIP_VIRTUAL_DEVICE_MAX + 1] = {0};
    for (size_t i = 0; i < USBIP_VIRTUAL_DEVICE_MAX; i++) {
        devs[i].ops = &s_ops;
        make_synthetic_dap(&devs[i].desc);
        ASSERT_EQ_INT(usbip_register_virtual_device(&devs[i]), 0);
    }
    devs[USBIP_VIRTUAL_DEVICE_MAX].ops = &s_ops;
    make_synthetic_dap(&devs[USBIP_VIRTUAL_DEVICE_MAX].desc);
    ASSERT_EQ_INT(usbip_register_virtual_device(&devs[USBIP_VIRTUAL_DEVICE_MAX]),
                  -ENOMEM);
}

TEST(test_get_all)
{
    usbip_virtual_device_reset_for_test();

    virtual_device_t dev1 = {0};
    virtual_device_t dev2 = {0};
    dev1.ops = &s_ops;
    dev2.ops = &s_ops;
    make_synthetic_dap(&dev1.desc);
    make_synthetic_dap(&dev2.desc);
    usbip_register_virtual_device(&dev1);
    usbip_register_virtual_device(&dev2);

    usbip_dev_record_t out[8];
    size_t n = usbip_get_virtual_devices(out, 8);
    ASSERT_EQ_INT(n, 2);
    ASSERT_TRUE(strcmp(out[0].busid, "2-1") == 0, "out[0].busid mismatch");
    ASSERT_TRUE(strcmp(out[1].busid, "2-2") == 0, "out[1].busid mismatch");
}

/* Helper for tests above: portable big-endian convert without
 * pulling in arpa/inet.h on the host build. The protocol module
 * already has its own bswap; this is local to tests. */
static uint32_t htobe32_local(uint32_t x)
{
    uint8_t b[4] = { (x >> 24) & 0xFF, (x >> 16) & 0xFF,
                     (x >> 8) & 0xFF, x & 0xFF };
    uint32_t r;
    memcpy(&r, b, 4);
    return r;
}

/* ---------- main ---------- */

int main(void)
{
    run_test_op_common_pack_unpack();
    run_test_op_common_unpack_request();
    run_test_device_desc_size();
    run_test_pack_device_desc();
    run_test_pack_interface_desc();
    run_test_make_devid();
    run_test_unpack_header_submit();
    run_test_validate_submit_too_large();
    run_test_validate_submit_negative_length();
    run_test_validate_submit_iso_unsupported();
    run_test_validate_submit_zero_packets_ok();
    run_test_validate_submit_bad_direction();
    run_test_validate_submit_bad_ep();
    run_test_pack_ret_submit_byteorder();
    run_test_pack_ret_submit_negative_status();
    run_test_pack_ret_unlink();
    run_test_register_assigns_busid();
    run_test_register_two_synthetic_devices();
    run_test_find_by_busid_padding();
    run_test_register_invalid();
    run_test_register_full();
    run_test_get_all();

    if (g_failures == 0) {
        printf("\nAll tests passed.\n");
        return 0;
    } else {
        printf("\n%d test(s) failed.\n", g_failures);
        return g_failures;
    }
}
