/*
 * spi2-swd-spike
 *
 * Validates ESP32-S3 SPI2 + GDMA half-duplex 3-wire as the SWD I/O backend.
 * Two test modes are run from app_main():
 *
 *  Test A: Full-duplex loopback at 1..50 MHz in 1 MHz steps. Requires the
 *          user to short GPIO11 (MOSI) to GPIO13 (MISO) with a single jumper.
 *          Sweeps clock, transmits a 256-byte LFSR pattern via DMA, reads it
 *          back on MISO via DMA, verifies byte-for-byte. Reports the highest
 *          rate that passes.
 *
 *  Test B: 3-wire half-duplex SWD frame timing. Configures SPI2 with
 *          SPI_DEVICE_HALFDUPLEX | SPI_DEVICE_3WIRE on GPIO11 (D) and
 *          GPIO12 (CLK), with GPIO14 as the DIR strobe. Sends back-to-back
 *          SWD-shaped sequences:
 *            - 8-bit header  (TX, DIR=out)
 *            - 1-bit Trn     (DIR toggle, line idle one bit cycle)
 *            - 33-bit ACK+data (RX, DIR=in)
 *          and measures end-to-end wall time per frame plus the DIR-toggle
 *          window using the CCOUNT cycle counter. Cannot self-verify data in
 *          this mode without an external loopback rig; only timing is reported.
 *
 * Build: see CMakeLists.txt; standard idf.py build/flash/monitor.
 *
 * Pin map (ESP32-S3 native SPI2 IOMUX pins):
 *   GPIO12  SPI2 CLK   -> SWCLK
 *   GPIO11  SPI2 MOSI  -> SWDIO (3-wire D pin)
 *   GPIO13  SPI2 MISO  -> Test A loopback only; not used in production
 *   GPIO14  GPIO out   -> DIR (level-translator direction strobe)
 */

#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_cpu.h"
#include "esp_private/esp_clk.h"
#include "soc/spi_pins.h"

static const char *TAG = "spi2-swd-spike";

/* Pin assignments. */
#define PIN_SCLK    12
#define PIN_MOSI    11
#define PIN_MISO    13
#define PIN_DIR     14

#define LOOPBACK_LEN_BYTES   256
#define SWEEP_MIN_MHZ        1
#define SWEEP_MAX_MHZ        50
#define SWEEP_STEP_MHZ       1
#define SWEEP_TX_VERIFY_RUNS 4   /* repeat each rate to catch flaky behaviour */

/* SWD-shaped frame parameters. */
#define SWD_HDR_BITS         8
#define SWD_DATA_BITS        33    /* 32 data + 1 parity */
#define SWD_FRAMES_PER_BENCH 1024

/* CCOUNT cycle counter helpers. */
static inline uint32_t ccount_now(void)
{
    return esp_cpu_get_cycle_count();
}

static uint32_t cpu_freq_hz_cached = 0;
static uint32_t cpu_freq_hz(void)
{
    if (cpu_freq_hz_cached == 0) {
        cpu_freq_hz_cached = esp_clk_cpu_freq();
    }
    return cpu_freq_hz_cached;
}

/* LFSR pattern generator: deterministic, non-trivial, distinguishes bit slips. */
static uint8_t lfsr_byte(uint8_t *state)
{
    /* 8-bit Galois LFSR, taps 0xB8 (x^8 + x^6 + x^5 + x^4 + 1). */
    uint8_t s = *state;
    uint8_t lsb = s & 1;
    s >>= 1;
    if (lsb) s ^= 0xB8;
    if (s == 0) s = 0xA5;
    *state = s;
    return s;
}

static void fill_lfsr(uint8_t *buf, size_t n, uint8_t seed)
{
    uint8_t s = seed ? seed : 0xA5;
    for (size_t i = 0; i < n; i++) buf[i] = lfsr_byte(&s);
}

/* ---------------------------------------------------------------------------
 * Test A: full-duplex loopback at sweep clocks.
 * Returns the highest rate that passed integrity checks for all runs.
 * ------------------------------------------------------------------------- */
static int test_a_loopback_sweep(void)
{
    spi_bus_config_t buscfg = {
        .mosi_io_num = PIN_MOSI,
        .miso_io_num = PIN_MISO,
        .sclk_io_num = PIN_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = LOOPBACK_LEN_BYTES,
    };
    /* SPI_DMA_CH_AUTO requests GDMA channel allocation. */
    esp_err_t err = spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "test A: bus init failed: %s", esp_err_to_name(err));
        return -1;
    }

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = SPI_MASTER_FREQ_10M,  /* placeholder, overridden per txn */
        .mode = 0,
        .spics_io_num = -1,
        .queue_size = 4,
        .input_delay_ns = 0,
        .flags = 0,  /* full-duplex */
    };
    spi_device_handle_t dev = NULL;
    err = spi_bus_add_device(SPI2_HOST, &devcfg, &dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "test A: add_device failed: %s", esp_err_to_name(err));
        spi_bus_free(SPI2_HOST);
        return -1;
    }

    uint8_t *tx = heap_caps_malloc(LOOPBACK_LEN_BYTES, MALLOC_CAP_DMA);
    uint8_t *rx = heap_caps_malloc(LOOPBACK_LEN_BYTES, MALLOC_CAP_DMA);
    if (!tx || !rx) {
        ESP_LOGE(TAG, "test A: DMA buffer alloc failed");
        if (tx) heap_caps_free(tx);
        if (rx) heap_caps_free(rx);
        spi_bus_remove_device(dev);
        spi_bus_free(SPI2_HOST);
        return -1;
    }
    fill_lfsr(tx, LOOPBACK_LEN_BYTES, 0xA5);

    int last_pass_mhz = 0;
    ESP_LOGI(TAG, "Test A: full-duplex loopback sweep %d..%d MHz step %d MHz",
             SWEEP_MIN_MHZ, SWEEP_MAX_MHZ, SWEEP_STEP_MHZ);
    ESP_LOGI(TAG, "       short GPIO%d (MOSI) to GPIO%d (MISO) with a jumper",
             PIN_MOSI, PIN_MISO);

    for (int mhz = SWEEP_MIN_MHZ; mhz <= SWEEP_MAX_MHZ; mhz += SWEEP_STEP_MHZ) {
        int hz = mhz * 1000 * 1000;
        bool any_fail = false;
        int first_fail_byte = -1;
        uint8_t fail_tx = 0, fail_rx = 0;
        int64_t t0 = 0, t1 = 0;
        size_t total_bytes = 0;

        for (int run = 0; run < SWEEP_TX_VERIFY_RUNS; run++) {
            memset(rx, 0xCC, LOOPBACK_LEN_BYTES);
            spi_transaction_t t = {
                .length = LOOPBACK_LEN_BYTES * 8,
                .rxlength = LOOPBACK_LEN_BYTES * 8,
                .tx_buffer = tx,
                .rx_buffer = rx,
                .override_freq_hz = (uint32_t)hz,
            };
            if (run == 0) t0 = esp_timer_get_time();
            err = spi_device_polling_transmit(dev, &t);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "  %d MHz run %d: transmit error %s",
                         mhz, run, esp_err_to_name(err));
                any_fail = true;
                break;
            }
            for (size_t i = 0; i < LOOPBACK_LEN_BYTES; i++) {
                if (rx[i] != tx[i]) {
                    any_fail = true;
                    if (first_fail_byte < 0) {
                        first_fail_byte = (int)i;
                        fail_tx = tx[i];
                        fail_rx = rx[i];
                    }
                    break;
                }
            }
            total_bytes += LOOPBACK_LEN_BYTES;
            if (run == SWEEP_TX_VERIFY_RUNS - 1) t1 = esp_timer_get_time();
            if (any_fail) break;
        }

        if (!any_fail) {
            int64_t us = t1 - t0;
            uint32_t kbps = us > 0 ? (uint32_t)((total_bytes * 8000ULL) / (uint64_t)us) : 0;
            ESP_LOGI(TAG, "  %2d MHz: PASS  (%u bytes, %lld us, %lu kbit/s)",
                     mhz, (unsigned)total_bytes, (long long)us, (unsigned long)kbps);
            last_pass_mhz = mhz;
        } else {
            ESP_LOGW(TAG, "  %2d MHz: FAIL  (first mismatch byte %d: tx=0x%02x rx=0x%02x)",
                     mhz, first_fail_byte, fail_tx, fail_rx);
            /* Keep sweeping past failures so the user sees if higher rates
             * spuriously pass or if it's a hard ceiling. */
        }
    }

    heap_caps_free(tx);
    heap_caps_free(rx);
    spi_bus_remove_device(dev);
    spi_bus_free(SPI2_HOST);

    ESP_LOGI(TAG, "Test A: highest rate that passed = %d MHz", last_pass_mhz);
    return last_pass_mhz;
}

/* ---------------------------------------------------------------------------
 * Test B: SWD-shaped frame timing in 3-wire half-duplex.
 * ------------------------------------------------------------------------- */
static void test_b_swd_frame_timing(int target_mhz)
{
    /* DIR strobe on GPIO14. Driven push-pull from S3. */
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << PIN_DIR,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    gpio_set_level(PIN_DIR, 1); /* assume DIR=1 means S3->target initially */

    spi_bus_config_t buscfg = {
        .mosi_io_num = PIN_MOSI,
        .miso_io_num = -1,           /* 3-wire: no separate MISO line */
        .sclk_io_num = PIN_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 8,
    };
    esp_err_t err = spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "test B: bus init failed: %s", esp_err_to_name(err));
        return;
    }

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = target_mhz * 1000 * 1000,
        .mode = 0,
        .spics_io_num = -1,
        .queue_size = 4,
        .input_delay_ns = 0,
        .flags = SPI_DEVICE_HALFDUPLEX | SPI_DEVICE_3WIRE,
    };
    spi_device_handle_t dev = NULL;
    err = spi_bus_add_device(SPI2_HOST, &devcfg, &dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "test B: add_device failed: %s", esp_err_to_name(err));
        spi_bus_free(SPI2_HOST);
        return;
    }

    /* DMA-capable buffers. SWD data phase is 33 bits, round up to 5 bytes. */
    uint8_t *tx_hdr = heap_caps_malloc(8, MALLOC_CAP_DMA);
    uint8_t *rx_data = heap_caps_malloc(8, MALLOC_CAP_DMA);
    if (!tx_hdr || !rx_data) {
        ESP_LOGE(TAG, "test B: DMA buffer alloc failed");
        if (tx_hdr) heap_caps_free(tx_hdr);
        if (rx_data) heap_caps_free(rx_data);
        spi_bus_remove_device(dev);
        spi_bus_free(SPI2_HOST);
        return;
    }
    /* Realistic SWD read header: start=1 APnDP=0 RnW=1 A2=0 A3=0 parity=1
     * stop=0 park=1 -> 0b10100101 = 0xA5. */
    tx_hdr[0] = 0xA5;
    memset(rx_data, 0, 8);

    ESP_LOGI(TAG, "Test B: SWD frame timing at %d MHz (3-wire half-duplex, %u frames)",
             target_mhz, (unsigned)SWD_FRAMES_PER_BENCH);

    /* Measure the DIR-toggle latency in isolation. We toggle DIR around a
     * no-op block to capture the GPIO write cost itself. */
    {
        uint32_t cyc0 = ccount_now();
        for (int i = 0; i < 1000; i++) {
            gpio_set_level(PIN_DIR, 0);
            gpio_set_level(PIN_DIR, 1);
        }
        uint32_t cyc1 = ccount_now();
        uint32_t cycles_per_pair = (cyc1 - cyc0) / 1000;
        ESP_LOGI(TAG, "  GPIO toggle pair: %lu CPU cycles  (~%lu ns at %lu MHz)",
                 (unsigned long)cycles_per_pair,
                 (unsigned long)((uint64_t)cycles_per_pair * 1000000000ULL / cpu_freq_hz()),
                 (unsigned long)(cpu_freq_hz() / 1000000));
        ESP_LOGI(TAG, "  At %d MHz SWCLK, this is ~%lu SWD bit cycles per single toggle",
                 target_mhz,
                 (unsigned long)((uint64_t)cycles_per_pair * target_mhz / 2 / (cpu_freq_hz() / 1000000)));
    }

    /* Frame loop. */
    int64_t t0 = esp_timer_get_time();
    uint32_t cyc_start = ccount_now();
    for (int f = 0; f < SWD_FRAMES_PER_BENCH; f++) {
        /* Phase 1: header TX (8 bits, DIR=out). */
        gpio_set_level(PIN_DIR, 1);
        spi_transaction_t th = {
            .length = SWD_HDR_BITS,
            .rxlength = 0,
            .tx_buffer = tx_hdr,
            .rx_buffer = NULL,
        };
        err = spi_device_polling_transmit(dev, &th);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "test B: frame %d header err %s", f, esp_err_to_name(err));
            break;
        }

        /* Phase 2: DIR flip to "in" before turnaround; DIR latency is the
         * gap that determines per-frame overhead. */
        gpio_set_level(PIN_DIR, 0);

        /* Phase 3: data RX (33 bits, DIR=in). In 3-wire half-duplex, the
         * peripheral tri-states MOSI and reads the line via SIO routing. */
        spi_transaction_t td = {
            .length = 0,
            .rxlength = SWD_DATA_BITS,
            .tx_buffer = NULL,
            .rx_buffer = rx_data,
        };
        err = spi_device_polling_transmit(dev, &td);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "test B: frame %d data err %s", f, esp_err_to_name(err));
            break;
        }
    }
    uint32_t cyc_end = ccount_now();
    int64_t t1 = esp_timer_get_time();

    int64_t us = t1 - t0;
    uint32_t cyc_total = cyc_end - cyc_start;
    uint32_t cyc_per_frame = cyc_total / SWD_FRAMES_PER_BENCH;
    uint64_t us_per_frame_x1000 = (uint64_t)us * 1000 / SWD_FRAMES_PER_BENCH;
    /* Theoretical raw bus time for one frame: hdr 8 + trn 1 + data 33 = 42 bits.
     * At target MHz: 42 / target MHz microseconds. */
    uint32_t raw_us_x1000_per_frame = (uint32_t)((42ULL * 1000) / target_mhz);
    uint64_t fps = us > 0 ? (uint64_t)SWD_FRAMES_PER_BENCH * 1000000ULL / (uint64_t)us : 0;

    ESP_LOGI(TAG, "  total wall: %lld us, %lu CPU cycles", (long long)us, (unsigned long)cyc_total);
    ESP_LOGI(TAG, "  per frame:  %llu.%03llu us, %lu cycles",
             (unsigned long long)(us_per_frame_x1000 / 1000),
             (unsigned long long)(us_per_frame_x1000 % 1000),
             (unsigned long)cyc_per_frame);
    ESP_LOGI(TAG, "  raw bus theoretical: %lu.%03lu us per 42-bit frame at %d MHz",
             (unsigned long)(raw_us_x1000_per_frame / 1000),
             (unsigned long)(raw_us_x1000_per_frame % 1000),
             target_mhz);
    ESP_LOGI(TAG, "  per-frame software overhead: %llu.%03llu us",
             (unsigned long long)((us_per_frame_x1000 - raw_us_x1000_per_frame) / 1000),
             (unsigned long long)((us_per_frame_x1000 - raw_us_x1000_per_frame) % 1000));
    ESP_LOGI(TAG, "  frames/sec achieved: %llu", (unsigned long long)fps);
    ESP_LOGI(TAG, "  effective SWD word rate (32-bit reads): %llu words/s",
             (unsigned long long)fps);

    heap_caps_free(tx_hdr);
    heap_caps_free(rx_data);
    spi_bus_remove_device(dev);
    spi_bus_free(SPI2_HOST);
}

void app_main(void)
{
    ESP_LOGI(TAG, "spi2-swd-spike starting");
    ESP_LOGI(TAG, "CPU = %lu MHz", (unsigned long)(cpu_freq_hz() / 1000000));
    ESP_LOGI(TAG, "Pin map: SCLK=GPIO%d MOSI=GPIO%d MISO=GPIO%d DIR=GPIO%d",
             PIN_SCLK, PIN_MOSI, PIN_MISO, PIN_DIR);

    /* Settle. */
    vTaskDelay(pdMS_TO_TICKS(500));

    int max_mhz_pass = test_a_loopback_sweep();

    /* Settle, give serial buffers time to drain. */
    vTaskDelay(pdMS_TO_TICKS(200));

    /* Run Test B at a few representative rates. */
    int b_rates[] = {10, 20, 25, 40};
    for (size_t i = 0; i < sizeof(b_rates)/sizeof(b_rates[0]); i++) {
        if (b_rates[i] <= max_mhz_pass || max_mhz_pass == 0) {
            test_b_swd_frame_timing(b_rates[i]);
            vTaskDelay(pdMS_TO_TICKS(100));
        } else {
            ESP_LOGI(TAG, "Skip Test B at %d MHz (Test A only passed up to %d MHz)",
                     b_rates[i], max_mhz_pass);
        }
    }

    ESP_LOGI(TAG, "spi2-swd-spike done");
    while (1) vTaskDelay(pdMS_TO_TICKS(1000));
}
