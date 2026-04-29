/* Annealage Pod: WS-B unit tests for the USB host backend API
 * contract.
 *
 * Run with `ctest`. The harness deliberately uses no external
 * dependencies (no Unity, no Cmocka). Each TEST() macro asserts with
 * a clear message and increments a counter; the binary exits with the
 * failure count so CTest reads the right status.
 *
 * The on-host build links `usbhost_stub.c` (the WS-A-installed stub)
 * because the real `usbhost.c` depends on ESP-IDF + FreeRTOS + the
 * IDF `usb_host` component. The stub honours the documented
 * fallback semantics (return -ENOSYS for transfers, 0 for start,
 * empty results for queries) which the multiplexer's RET_SUBMIT
 * path relies on.
 */

#include "usbhost.h"

#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
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

/* The multiplexer always supplies a NUL-padded 32-byte busid. */
static void make_busid(char out[USBIP_BUSID_SIZE], const char *s)
{
    memset(out, 0, USBIP_BUSID_SIZE);
    size_t n = strlen(s);
    if (n > USBIP_BUSID_SIZE) {
        n = USBIP_BUSID_SIZE;
    }
    memcpy(out, s, n);
}

/* ----- Tests ------------------------------------------------------------- */

TEST(stub_start_returns_zero)
{
    /* Stub semantics: usbhost_start() must return 0 so the rest of
     * firmware boots, even when the real USB host is not present.
     * The real backend also returns 0 on successful install. */
    ASSERT_EQ_INT(usbhost_start(), 0);
}

TEST(stub_get_devices_empty)
{
    usbip_dev_record_t recs[2];
    memset(recs, 0xAB, sizeof(recs));
    size_t n = usbhost_get_devices(recs, 2);
    ASSERT_EQ_INT(n, 0);
}

TEST(stub_get_device_by_busid_miss)
{
    char busid[USBIP_BUSID_SIZE];
    make_busid(busid, "1-1");
    usbip_dev_record_t out;
    memset(&out, 0xCD, sizeof(out));
    bool found = usbhost_get_device_by_busid(busid, &out);
    ASSERT_TRUE(!found, "stub must not pretend to know any device");
}

TEST(stub_control_transfer_returns_enosys)
{
    char busid[USBIP_BUSID_SIZE];
    make_busid(busid, "1-1");

    usbip_setup_packet_t setup = {
        .bmRequestType = 0x80,           /* IN, standard, device */
        .bRequest      = 0x06,           /* GET_DESCRIPTOR */
        .wValue        = 0x0100,
        .wIndex        = 0,
        .wLength       = 18,
    };
    uint8_t in_buf[18];
    size_t in_len = 99;

    int rc = usbhost_control_transfer(busid, &setup,
                                      NULL, 0,
                                      in_buf, sizeof(in_buf), &in_len, NULL);
    ASSERT_EQ_INT(rc, -ENOSYS);
    ASSERT_EQ_INT(in_len, 0);
}

TEST(stub_bulk_transfer_returns_enosys)
{
    char busid[USBIP_BUSID_SIZE];
    make_busid(busid, "1-1");

    uint8_t in_buf[64];
    size_t in_len = 99;
    int rc = usbhost_bulk_transfer(busid, 0x81,
                                   NULL, 0,
                                   in_buf, sizeof(in_buf), &in_len, NULL);
    ASSERT_EQ_INT(rc, -ENOSYS);
    ASSERT_EQ_INT(in_len, 0);
}

TEST(stub_interrupt_transfer_returns_enosys)
{
    char busid[USBIP_BUSID_SIZE];
    make_busid(busid, "1-1");

    uint8_t in_buf[8];
    size_t in_len = 99;
    int rc = usbhost_interrupt_transfer(busid, 0x82,
                                        NULL, 0,
                                        in_buf, sizeof(in_buf), &in_len, NULL);
    ASSERT_EQ_INT(rc, -ENOSYS);
    ASSERT_EQ_INT(in_len, 0);
}

TEST(stub_is_interrupt_endpoint_false)
{
    char busid[USBIP_BUSID_SIZE];
    make_busid(busid, "1-1");
    /* No real device; both directions and a few endpoint numbers. */
    ASSERT_TRUE(!usbhost_is_interrupt_endpoint(busid, 0x81, 1), "in ep1");
    ASSERT_TRUE(!usbhost_is_interrupt_endpoint(busid, 0x01, 0), "out ep1");
    ASSERT_TRUE(!usbhost_is_interrupt_endpoint(busid, 0x82, 1), "in ep2");
}

TEST(stub_out_transfer_no_in_buffer_safe)
{
    /* Multiplexer may legitimately call with in_data == NULL,
     * in_capacity == 0 for an OUT endpoint. The stub must still
     * report a valid -ENOSYS without dereferencing anything. */
    char busid[USBIP_BUSID_SIZE];
    make_busid(busid, "1-2");

    uint8_t out_buf[16] = {0};
    size_t in_len = 99;
    int rc = usbhost_bulk_transfer(busid, 0x02,
                                   out_buf, sizeof(out_buf),
                                   NULL, 0, &in_len, NULL);
    ASSERT_EQ_INT(rc, -ENOSYS);
    /* Stub still zeros in_len for safety. */
    ASSERT_EQ_INT(in_len, 0);
}

TEST(api_constants_match_kernel_values)
{
    /* Sanity check on the constants the multiplexer uses to talk
     * to this module. These must match the kernel's USB/IP
     * conventions. */
    ASSERT_EQ_INT((int)USBIP_BUSID_SIZE, 32);
    ASSERT_EQ_INT((int)USBIP_PATH_SIZE, 256);
    ASSERT_EQ_INT((int)USBIP_REQUEST_DIR_IN, 0x80);
}

/* ----- Test runner ------------------------------------------------------- */

int main(void)
{
    run_stub_start_returns_zero();
    run_stub_get_devices_empty();
    run_stub_get_device_by_busid_miss();
    run_stub_control_transfer_returns_enosys();
    run_stub_bulk_transfer_returns_enosys();
    run_stub_interrupt_transfer_returns_enosys();
    run_stub_is_interrupt_endpoint_false();
    run_stub_out_transfer_no_in_buffer_safe();
    run_api_constants_match_kernel_values();

    if (g_failures == 0) {
        printf("\nALL TESTS PASSED\n");
        return 0;
    }
    fprintf(stderr, "\n%d FAILURES\n", g_failures);
    return g_failures;
}
