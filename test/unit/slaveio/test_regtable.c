// Host-side unit tests for slaveio_regtable.{c,h}.
//
// No IDF or FreeRTOS dependency. Exercises the register-table state
// machine that backs both the I2C-slave and SPI-slave personalities.
// Run via test/unit/slaveio/run.sh.

#include "slaveio_regtable.h"

#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define EXPECT_EQ(a, b) do { \
    if ((a) != (b)) { \
        fprintf(stderr, "FAIL %s:%d: %s (=%lld) != %s (=%lld)\n", \
                __FILE__, __LINE__, #a, (long long)(a), #b, (long long)(b)); \
        return 1; \
    } \
} while (0)

#define EXPECT_TRUE(x) do { \
    if (!(x)) { \
        fprintf(stderr, "FAIL %s:%d: %s is false\n", __FILE__, __LINE__, #x); \
        return 1; \
    } \
} while (0)

// Helper: drive a master write transaction.
static void master_write(slaveio_regtable_t *st, uint8_t reg, const uint8_t *data, size_t n) {
    slaveio_regtable_begin_write(st);
    slaveio_regtable_on_rx_byte(st, reg);
    if (data && n > 0) {
        slaveio_regtable_on_rx_chunk(st, data, n);
    }
    slaveio_regtable_end_write(st);
}

// Helper: drive a master read transaction.
static size_t master_read(slaveio_regtable_t *st, uint8_t *out_buf, size_t len) {
    slaveio_regtable_begin_read(st);
    size_t n = slaveio_regtable_get_tx_chunk(st, out_buf, len);
    slaveio_regtable_end_read(st);
    return n;
}

// 1. Pointer + data write, then read returns the data.
static int test_basic_write_then_read(void) {
    uint8_t rb[64], wb[64];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    rb[4] = 0xAA; rb[5] = 0xBB; rb[6] = 0xCC;

    master_write(&st, 4, NULL, 0);
    EXPECT_EQ(st.reg_ptr, 4u);

    uint8_t got[8] = { 0 };
    size_t n = master_read(&st, got, 3);
    EXPECT_EQ(n, 3u);
    EXPECT_EQ(got[0], 0xAA);
    EXPECT_EQ(got[1], 0xBB);
    EXPECT_EQ(got[2], 0xCC);

    EXPECT_EQ(st.reg_ptr, 7u);

    EXPECT_EQ(st.transfers_total, 2u);
    EXPECT_EQ(st.overflow_count, 0u);

    return 0;
}

// 2. Master writes [reg_ptr][data...] and the data lands in write_buf.
static int test_master_write_payload(void) {
    uint8_t rb[16], wb[16];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    uint8_t payload[] = { 0x11, 0x22, 0x33, 0x44 };
    master_write(&st, 8, payload, sizeof(payload));

    EXPECT_EQ(wb[8],  0x11);
    EXPECT_EQ(wb[9],  0x22);
    EXPECT_EQ(wb[10], 0x33);
    EXPECT_EQ(wb[11], 0x44);
    EXPECT_EQ(st.reg_ptr, 12u);
    EXPECT_EQ(st.last_xact_was_write, true);
    EXPECT_EQ(st.last_xact_offset, 8u);
    EXPECT_EQ(st.last_xact_length, 4u);
    return 0;
}

// 3. Wraparound on writes past the end of the table.
static int test_write_wrap(void) {
    uint8_t rb[8], wb[8];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    uint8_t payload[] = { 0xDE, 0xAD, 0xBE, 0xEF };
    master_write(&st, 6, payload, sizeof(payload));
    EXPECT_EQ(wb[6], 0xDE);
    EXPECT_EQ(wb[7], 0xAD);
    EXPECT_EQ(wb[0], 0xBE);
    EXPECT_EQ(wb[1], 0xEF);
    return 0;
}

// 4. Wraparound on reads past the end of the table.
static int test_read_wrap(void) {
    uint8_t rb[8], wb[8];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    rb[6] = 0x10;
    rb[7] = 0x20;
    rb[0] = 0x30;
    rb[1] = 0x40;

    master_write(&st, 6, NULL, 0);
    uint8_t got[4] = { 0 };
    size_t n = master_read(&st, got, 4);
    EXPECT_EQ(n, 4u);
    EXPECT_EQ(got[0], 0x10);
    EXPECT_EQ(got[1], 0x20);
    EXPECT_EQ(got[2], 0x30);
    EXPECT_EQ(got[3], 0x40);
    return 0;
}

// 5. Notify range registration, dispatch on overlapping write.
static int test_notify_dispatch(void) {
    uint8_t rb[64], wb[64];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    int slot = slaveio_regtable_register_notify(&st, 10, 20, 42);
    EXPECT_TRUE(slot >= 0);

    uint8_t p1[] = { 1, 2, 3 };
    master_write(&st, 5, p1, sizeof(p1));
    uint32_t id = 0;
    EXPECT_EQ(slaveio_regtable_pop_notify(&st, &id), false);

    uint8_t p2[] = { 4, 5 };
    master_write(&st, 18, p2, sizeof(p2));
    EXPECT_EQ(slaveio_regtable_pop_notify(&st, &id), true);
    EXPECT_EQ(id, 42u);
    EXPECT_EQ(slaveio_regtable_pop_notify(&st, &id), false);

    master_write(&st, 19, p1, sizeof(p1));
    EXPECT_EQ(slaveio_regtable_pop_notify(&st, &id), true);
    EXPECT_EQ(id, 42u);
    return 0;
}

// 6. Multiple callbacks on overlapping ranges.
static int test_multi_notify(void) {
    uint8_t rb[64], wb[64];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    EXPECT_TRUE(slaveio_regtable_register_notify(&st, 0,  16, 100) >= 0);
    EXPECT_TRUE(slaveio_regtable_register_notify(&st, 8,  24, 200) >= 0);
    EXPECT_TRUE(slaveio_regtable_register_notify(&st, 32, 40, 300) >= 0);

    uint8_t p[] = { 1, 2, 3, 4 };
    master_write(&st, 10, p, sizeof(p));

    uint32_t a = 0, b = 0, c = 0;
    EXPECT_TRUE(slaveio_regtable_pop_notify(&st, &a));
    EXPECT_TRUE(slaveio_regtable_pop_notify(&st, &b));
    EXPECT_TRUE(!slaveio_regtable_pop_notify(&st, &c));
    EXPECT_TRUE((a == 100 && b == 200) || (a == 200 && b == 100));
    return 0;
}

// 7. Notify unregister + replace by same callback id.
static int test_notify_unregister(void) {
    uint8_t rb[32], wb[32];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    EXPECT_TRUE(slaveio_regtable_register_notify(&st, 0, 8, 7) >= 0);
    EXPECT_TRUE(slaveio_regtable_register_notify(&st, 0, 24, 7) >= 0);

    int active = 0;
    for (int i = 0; i < SLAVEIO_REGTABLE_MAX_NOTIFIES; ++i) {
        if (st.notifies[i].active) { active++; }
    }
    EXPECT_EQ(active, 1);

    uint8_t p[] = { 9 };
    master_write(&st, 16, p, sizeof(p));
    uint32_t id = 0;
    EXPECT_TRUE(slaveio_regtable_pop_notify(&st, &id));
    EXPECT_EQ(id, 7u);

    EXPECT_EQ(slaveio_regtable_unregister_notify(&st, 7), 0);
    master_write(&st, 16, p, sizeof(p));
    EXPECT_TRUE(!slaveio_regtable_pop_notify(&st, &id));
    return 0;
}

// 8. Notify-table full -> register returns -1.
static int test_notify_table_full(void) {
    uint8_t rb[64], wb[64];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    for (int i = 0; i < SLAVEIO_REGTABLE_MAX_NOTIFIES; ++i) {
        EXPECT_TRUE(slaveio_regtable_register_notify(&st, 0, 1, 1000 + i) >= 0);
    }
    EXPECT_EQ(slaveio_regtable_register_notify(&st, 0, 1, 9999), -1);
    return 0;
}

// 9. Notify queue overflow drops; counter increments.
static int test_notify_queue_overflow(void) {
    uint8_t rb[64], wb[64];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    EXPECT_TRUE(slaveio_regtable_register_notify(&st, 0, 64, 1) >= 0);

    for (int i = 0; i < SLAVEIO_REGTABLE_NOTIFY_QUEUE + 4; ++i) {
        uint8_t p = (uint8_t)i;
        master_write(&st, 0, &p, 1);
    }
    EXPECT_EQ(st.notify_queue_dropped, 4u);
    return 0;
}

// 10. Zero-size buffers: writes are dropped, reads return 0xFF.
static int test_zero_size_buffers(void) {
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, NULL, 0, NULL, 0);

    uint8_t p[] = { 1, 2, 3 };
    master_write(&st, 0, p, sizeof(p));
    EXPECT_TRUE(st.overflow_count >= 3);

    uint8_t got = 0;
    bool ok = slaveio_regtable_get_tx_byte(&st, &got);
    EXPECT_EQ(ok, false);
    EXPECT_EQ(got, 0xFF);
    return 0;
}

// 11. Reads after a pure-pointer-set write start from the right place.
static int test_pointer_only_write_then_read(void) {
    uint8_t rb[16], wb[16];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    for (int i = 0; i < 16; ++i) { rb[i] = (uint8_t)(0x80 + i); }

    master_write(&st, 3, NULL, 0);
    uint8_t got[4] = { 0 };
    size_t n = master_read(&st, got, 4);
    EXPECT_EQ(n, 4u);
    EXPECT_EQ(got[0], 0x83);
    EXPECT_EQ(got[1], 0x84);
    EXPECT_EQ(got[2], 0x85);
    EXPECT_EQ(got[3], 0x86);
    return 0;
}

// 12. Statistics tracking across mixed transactions.
static int test_stats(void) {
    uint8_t rb[64], wb[64];
    slaveio_regtable_t st;
    slaveio_regtable_init(&st, rb, sizeof(rb), wb, sizeof(wb));

    uint8_t p[] = { 0xFF };
    master_write(&st, 1, p, 1);
    EXPECT_EQ(st.transfers_total, 1u);
    EXPECT_EQ(st.last_xact_was_write, true);
    EXPECT_EQ(st.last_xact_length, 1u);

    uint8_t got[2];
    master_read(&st, got, 2);
    EXPECT_EQ(st.transfers_total, 2u);
    EXPECT_EQ(st.last_xact_was_write, false);
    EXPECT_EQ(st.last_xact_length, 2u);
    return 0;
}

typedef struct { const char *name; int (*fn)(void); } tcase_t;

int main(void) {
    static const tcase_t tests[] = {
        { "basic_write_then_read",       test_basic_write_then_read       },
        { "master_write_payload",        test_master_write_payload        },
        { "write_wrap",                  test_write_wrap                  },
        { "read_wrap",                   test_read_wrap                   },
        { "notify_dispatch",             test_notify_dispatch             },
        { "multi_notify",                test_multi_notify                },
        { "notify_unregister",           test_notify_unregister           },
        { "notify_table_full",           test_notify_table_full           },
        { "notify_queue_overflow",       test_notify_queue_overflow       },
        { "zero_size_buffers",           test_zero_size_buffers           },
        { "pointer_only_write_then_read", test_pointer_only_write_then_read },
        { "stats",                       test_stats                       },
    };
    int failed = 0;
    for (size_t i = 0; i < sizeof(tests) / sizeof(tests[0]); ++i) {
        int rc = tests[i].fn();
        printf("  %-32s %s\n", tests[i].name, rc == 0 ? "ok" : "FAILED");
        if (rc != 0) { failed++; }
    }
    printf("%zu tests, %d failed\n", sizeof(tests) / sizeof(tests[0]), failed);
    return failed == 0 ? 0 : 1;
}
