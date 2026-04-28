// Protocol-level unit tests for the uartbridge telnet/RFC2217 decoder.
//
// Built by test/unit/uartbridge/run.sh on the host (no IDF, no FreeRTOS).
// Verifies bytewise transparency in raw mode and bytewise filtering in
// telnet mode, including IAC escaping, command-with-option consumption,
// and COM_PORT_OPTION subnegotiation parsing.

#include "uart_bridge_telnet.h"

#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int total_pass = 0;
static int total_fail = 0;

#define CHECK(expr) do {                                                \
    if (expr) {                                                         \
        total_pass++;                                                   \
    } else {                                                            \
        total_fail++;                                                   \
        fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #expr); \
    }                                                                   \
} while (0)

#define CHECK_EQ(a, b) do {                                                  \
    long _a = (long)(a), _b = (long)(b);                                    \
    if (_a == _b) {                                                          \
        total_pass++;                                                        \
    } else {                                                                 \
        total_fail++;                                                        \
        fprintf(stderr, "FAIL %s:%d: %s == %s -> %ld != %ld\n",              \
                __FILE__, __LINE__, #a, #b, _a, _b);                         \
    }                                                                        \
} while (0)

#define CHECK_MEMEQ(buf, expected, len) do {                                 \
    if (memcmp((buf), (expected), (len)) == 0) {                             \
        total_pass++;                                                        \
    } else {                                                                 \
        total_fail++;                                                        \
        fprintf(stderr, "FAIL %s:%d: memcmp differs over %zu bytes\n",       \
                __FILE__, __LINE__, (size_t)(len));                          \
    }                                                                        \
} while (0)

// ---------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------

// Inactive decoder is a memcpy (raw passthrough preserves every byte).
static void test_raw_passthrough(void) {
    uart_bridge_telnet_t t;
    uart_bridge_telnet_init(&t, false, NULL, NULL);

    uint8_t in[256], out[512];
    for (size_t i = 0; i < sizeof(in); ++i) { in[i] = (uint8_t)i; }
    size_t n = uart_bridge_telnet_filter(&t, in, sizeof(in), out, sizeof(out));
    CHECK_EQ(n, sizeof(in));
    CHECK_MEMEQ(out, in, sizeof(in));

    // 0xFF in raw mode passes through untouched.
    uint8_t ff[3] = {0xFF, 0xFF, 0xFF};
    n = uart_bridge_telnet_filter(&t, ff, 3, out, sizeof(out));
    CHECK_EQ(n, 3);
    CHECK_MEMEQ(out, ff, 3);
}

// Telnet mode: IAC IAC escapes to a single 0xFF in the output stream.
static void test_telnet_iac_escape(void) {
    uart_bridge_telnet_t t;
    uart_bridge_telnet_init(&t, true, NULL, NULL);

    uint8_t in[] = {'a', 0xFF, 0xFF, 'b'};
    uint8_t out[16];
    size_t n = uart_bridge_telnet_filter(&t, in, sizeof(in), out, sizeof(out));
    CHECK_EQ(n, 3);
    uint8_t expect[] = {'a', 0xFF, 'b'};
    CHECK_MEMEQ(out, expect, 3);
}

// Telnet mode: IAC DO <opt> consumes three bytes and emits nothing.
static void test_telnet_iac_do(void) {
    uart_bridge_telnet_t t;
    uart_bridge_telnet_init(&t, true, NULL, NULL);

    uint8_t in[] = {'X', 0xFF, UART_BRIDGE_TELNET_DO, 0x18 /*opt*/, 'Y'};
    uint8_t out[16];
    size_t n = uart_bridge_telnet_filter(&t, in, sizeof(in), out, sizeof(out));
    CHECK_EQ(n, 2);
    uint8_t expect[] = {'X', 'Y'};
    CHECK_MEMEQ(out, expect, 2);
}

static int sb_cb_count;
static uart_bridge_telnet_sb_t sb_cb_last;
static void sb_cb(const uart_bridge_telnet_sb_t *sb, void *user) {
    (void)user;
    sb_cb_count++;
    sb_cb_last = *sb;
}

// COM-PORT subnegotiation: IAC SB 44 1 <baud32> IAC SE -> baud parsed.
static void test_telnet_set_baudrate(void) {
    uart_bridge_telnet_t t;
    sb_cb_count = 0;
    memset(&sb_cb_last, 0, sizeof(sb_cb_last));
    uart_bridge_telnet_init(&t, true, sb_cb, NULL);

    // 9600 baud big-endian: 0x00002580.
    uint8_t in[] = {
        'A',
        0xFF, UART_BRIDGE_TELNET_SB,
        UART_BRIDGE_TELNET_OPT_COM_PORT, UART_BRIDGE_COM_PORT_SET_BAUDRATE,
        0x00, 0x00, 0x25, 0x80,
        0xFF, UART_BRIDGE_TELNET_SE,
        'B',
    };
    uint8_t out[16];
    size_t n = uart_bridge_telnet_filter(&t, in, sizeof(in), out, sizeof(out));
    CHECK_EQ(n, 2);
    uint8_t expect[] = {'A', 'B'};
    CHECK_MEMEQ(out, expect, 2);
    CHECK_EQ(sb_cb_count, 1);
    CHECK_EQ(sb_cb_last.baud, 9600);
    CHECK_EQ(sb_cb_last.data_bits, -1);
    CHECK_EQ(sb_cb_last.parity, -1);
    CHECK_EQ(sb_cb_last.stop_bits, -1);
}

// Stress: decoder split across multiple filter() calls one byte at a time.
static void test_telnet_set_baudrate_bytewise(void) {
    uart_bridge_telnet_t t;
    sb_cb_count = 0;
    uart_bridge_telnet_init(&t, true, sb_cb, NULL);

    uint8_t stream[] = {
        'A', 'B',
        0xFF, UART_BRIDGE_TELNET_SB,
        UART_BRIDGE_TELNET_OPT_COM_PORT, UART_BRIDGE_COM_PORT_SET_BAUDRATE,
        0x00, 0x01, 0xC2, 0x00,    // 115200
        0xFF, UART_BRIDGE_TELNET_SE,
        'C',
    };
    uint8_t out[64];
    size_t op = 0;
    for (size_t i = 0; i < sizeof(stream); ++i) {
        uint8_t b = stream[i];
        op += uart_bridge_telnet_filter(&t, &b, 1, out + op, sizeof(out) - op);
    }
    CHECK_EQ(op, 3);
    uint8_t expect[] = {'A', 'B', 'C'};
    CHECK_MEMEQ(out, expect, 3);
    CHECK_EQ(sb_cb_count, 1);
    CHECK_EQ(sb_cb_last.baud, 115200);
}

// SET-DATASIZE 7 / SET-PARITY ODD / SET-STOPSIZE 2 combined exchange.
static void test_telnet_set_format(void) {
    uart_bridge_telnet_t t;
    sb_cb_count = 0;
    uart_bridge_telnet_init(&t, true, sb_cb, NULL);

    // Three back-to-back subneg frames, each addressed to the COM_PORT
    // option. Decoder emits one callback per terminator.
    uint8_t in[] = {
        // SET-DATASIZE 7
        0xFF, UART_BRIDGE_TELNET_SB,
        UART_BRIDGE_TELNET_OPT_COM_PORT, UART_BRIDGE_COM_PORT_SET_DATASIZE, 7,
        0xFF, UART_BRIDGE_TELNET_SE,
        // SET-PARITY 2 (RFC2217: 2 = ODD)
        0xFF, UART_BRIDGE_TELNET_SB,
        UART_BRIDGE_TELNET_OPT_COM_PORT, UART_BRIDGE_COM_PORT_SET_PARITY, 2,
        0xFF, UART_BRIDGE_TELNET_SE,
        // SET-STOPSIZE 2
        0xFF, UART_BRIDGE_TELNET_SB,
        UART_BRIDGE_TELNET_OPT_COM_PORT, UART_BRIDGE_COM_PORT_SET_STOPSIZE, 2,
        0xFF, UART_BRIDGE_TELNET_SE,
    };
    uint8_t out[16];
    size_t n = uart_bridge_telnet_filter(&t, in, sizeof(in), out, sizeof(out));
    CHECK_EQ(n, 0);
    CHECK_EQ(sb_cb_count, 3);
    // Last callback = stop bits.
    CHECK_EQ(sb_cb_last.stop_bits, 2);
}

// IAC IAC inside a subneg payload encodes a literal 0xFF in the SB body.
static void test_telnet_sb_iac_escape(void) {
    uart_bridge_telnet_t t;
    sb_cb_count = 0;
    uart_bridge_telnet_init(&t, true, sb_cb, NULL);

    // Bogus subneg whose body contains 0xFF. Use option != COM_PORT so
    // the parser returns false (and the callback should NOT fire).
    uint8_t in[] = {
        0xFF, UART_BRIDGE_TELNET_SB,
        99, /* unrelated option */
        0xFF, 0xFF, /* literal 0xFF in payload */
        'x',
        0xFF, UART_BRIDGE_TELNET_SE,
    };
    uint8_t out[16];
    size_t n = uart_bridge_telnet_filter(&t, in, sizeof(in), out, sizeof(out));
    CHECK_EQ(n, 0);
    // Callback only fires for recognised COM_PORT_OPTION frames.
    CHECK_EQ(sb_cb_count, 0);
}

// Output buffer too small: filter() emits as many bytes as fit and
// drops the rest. The decoder state still advances correctly.
static void test_telnet_output_truncation(void) {
    uart_bridge_telnet_t t;
    uart_bridge_telnet_init(&t, true, NULL, NULL);

    uint8_t in[] = {'a', 'b', 'c', 'd', 'e'};
    uint8_t out[3];
    size_t n = uart_bridge_telnet_filter(&t, in, sizeof(in), out, sizeof(out));
    CHECK_EQ(n, 3);
    CHECK_EQ(out[0], 'a');
    CHECK_EQ(out[1], 'b');
    CHECK_EQ(out[2], 'c');
}

// Direct parse test (no decoder state) of malformed COM-PORT frames.
static void test_parse_sb_bounds(void) {
    uart_bridge_telnet_sb_t sb;

    // Empty -> false.
    CHECK_EQ(uart_bridge_telnet_parse_sb((uint8_t *)"", 0, &sb), false);

    // Wrong option byte.
    uint8_t bad[] = {99, 1, 0, 0, 0x25, 0x80};
    CHECK_EQ(uart_bridge_telnet_parse_sb(bad, sizeof(bad), &sb), false);

    // SET-BAUDRATE truncated.
    uint8_t trunc[] = {UART_BRIDGE_TELNET_OPT_COM_PORT,
                        UART_BRIDGE_COM_PORT_SET_BAUDRATE, 0, 0};
    CHECK_EQ(uart_bridge_telnet_parse_sb(trunc, sizeof(trunc), &sb), false);

    // SET-DATASIZE out of range.
    uint8_t bad_ds[] = {UART_BRIDGE_TELNET_OPT_COM_PORT,
                         UART_BRIDGE_COM_PORT_SET_DATASIZE, 9};
    CHECK_EQ(uart_bridge_telnet_parse_sb(bad_ds, sizeof(bad_ds), &sb), false);

    // Valid SET-PARITY mark (4) -> not in {1,2,3}: false.
    uint8_t mark[] = {UART_BRIDGE_TELNET_OPT_COM_PORT,
                       UART_BRIDGE_COM_PORT_SET_PARITY, 4};
    CHECK_EQ(uart_bridge_telnet_parse_sb(mark, sizeof(mark), &sb), false);
}

// Verify bytewise transparency for every byte value in raw mode.
static void test_raw_full_byte_range(void) {
    uart_bridge_telnet_t t;
    uart_bridge_telnet_init(&t, false, NULL, NULL);

    uint8_t in[256], out[256];
    for (int i = 0; i < 256; ++i) { in[i] = (uint8_t)i; }
    size_t n = uart_bridge_telnet_filter(&t, in, 256, out, 256);
    CHECK_EQ(n, 256);
    for (int i = 0; i < 256; ++i) {
        CHECK_EQ(out[i], (uint8_t)i);
    }
}

int main(void) {
    test_raw_passthrough();
    test_telnet_iac_escape();
    test_telnet_iac_do();
    test_telnet_set_baudrate();
    test_telnet_set_baudrate_bytewise();
    test_telnet_set_format();
    test_telnet_sb_iac_escape();
    test_telnet_output_truncation();
    test_parse_sb_bounds();
    test_raw_full_byte_range();

    fprintf(stderr, "uart_bridge_telnet tests: %d passed, %d failed\n",
            total_pass, total_fail);
    return total_fail == 0 ? 0 : 1;
}
