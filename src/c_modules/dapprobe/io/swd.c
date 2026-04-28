// Annealage Pod: SWD I/O engine, SPI2 + dedic_gpio backend.
//
// Implementation strategy (see docs/design/swd-swo-engine.md):
//
//  1. spi_bus_initialize() with SPI_DMA_CH_AUTO so the IDF allocates a GDMA
//     channel and binds it to SPI2; we register one device to claim the bus
//     and grab the cached spi_dev_t pointer via SPI_LL_GET_HW().
//  2. Per-transaction work uses spi_ll_* HAL helpers and direct register
//     writes (windowsair pattern). The standard spi_master polling driver
//     adds ~25 us per call which dominates real bus time at 25-40 MHz, so we
//     bypass it on the hot path.
//  3. SWDIO direction strobe (translator DIR pin) is a dedic_gpio channel.
//     dedic_gpio_bundle_write() lowers per-toggle cost from ~335 ns to a
//     single CPU cycle, which is essential at 25 MHz SCLK where one bit cycle
//     is 40 ns and gpio_set_level() consumes ~8 SWD bit cycles.
//
// The engine implements SWD-DP-only; JTAG is not in scope (CMSIS-DAP can be
// configured SWD-only in DAP_config.h).

#include "swd.h"

#include <string.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/dedic_gpio.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_rom_gpio.h"
#include "hal/spi_hal.h"
#include "hal/spi_ll.h"
#include "soc/gpio_periph.h"
#include "soc/gpio_sig_map.h"
#include "soc/io_mux_reg.h"
#include "soc/spi_periph.h"
#include "soc/spi_pins.h"

static const char *TAG = "swd";

// ----------------------------------------------------------------------------
// Module state.
// ----------------------------------------------------------------------------

typedef struct {
    bool initialised;
    swd_config_t cfg;
    spi_dev_t *hw;                       // SPI2 register base
    spi_device_handle_t spi_dev;         // owned for bus claim only
    dedic_gpio_bundle_handle_t dir_bundle;
    uint32_t dir_mask_out;
    uint32_t realised_clock_hz;
    uint32_t transfers_total;
} swd_state_t;

static swd_state_t s_state;

// ----------------------------------------------------------------------------
// Pin / peripheral binding helpers.
// ----------------------------------------------------------------------------

// Bind SCLK on an arbitrary GPIO via the GPIO matrix.
// SPI2 is host index 1 in spi_periph_signal[].
static void swd_bind_sclk(int gpio)
{
    const spi_signal_conn_t *sig = &spi_periph_signal[1];
    gpio_set_direction(gpio, GPIO_MODE_OUTPUT);
    esp_rom_gpio_connect_out_signal(gpio, sig->spiclk_out, false, false);
}

// Bind SWDIO (data) on the SPI2 native IOMUX MOSI pin (GPIO11) for max
// performance. Output uses spid_out via IOMUX, input via spid_in (the SIO
// half-duplex routing handles both directions on the same pin).
static esp_err_t swd_bind_swdio_iomux(int gpio)
{
    const spi_signal_conn_t *sig = &spi_periph_signal[1];
    if (gpio == SPI2_IOMUX_PIN_NUM_MOSI) {
        // IOMUX direct: select FUNC for SPI2 on this pad and enable input so
        // the peripheral can read back the line in 3-wire half-duplex.
        PIN_FUNC_SELECT(GPIO_PIN_MUX_REG[gpio], sig->func);
        PIN_INPUT_ENABLE(GPIO_PIN_MUX_REG[gpio]);
        return ESP_OK;
    }
    // GPIO matrix fallback for non-IOMUX pins.
    gpio_set_direction(gpio, GPIO_MODE_INPUT_OUTPUT);
    esp_rom_gpio_connect_out_signal(gpio, sig->spid_out, false, false);
    esp_rom_gpio_connect_in_signal(gpio, sig->spid_in, false);
    return ESP_OK;
}

static esp_err_t swd_setup_dir_pin(int gpio, dedic_gpio_bundle_handle_t *out_bundle, uint32_t *out_mask)
{
    // The DIR strobe must be single-cycle. Allocate a one-pin dedic_gpio bundle.
    gpio_set_direction(gpio, GPIO_MODE_OUTPUT);
    int gpio_array[1] = { gpio };
    dedic_gpio_bundle_config_t cfg = {
        .gpio_array = gpio_array,
        .array_size = 1,
        .flags = {
            .out_en = 1,
            .in_en = 0,
        },
    };
    dedic_gpio_bundle_handle_t bundle = NULL;
    esp_err_t err = dedic_gpio_new_bundle(&cfg, &bundle);
    if (err != ESP_OK) {
        return err;
    }
    uint32_t mask = 0;
    err = dedic_gpio_get_out_mask(bundle, &mask);
    if (err != ESP_OK) {
        dedic_gpio_del_bundle(bundle);
        return err;
    }
    *out_bundle = bundle;
    *out_mask = mask;
    return ESP_OK;
}

static IRAM_ATTR inline void swd_dir_drive_out(void)
{
    // DIR=high -> S3 drives the SWDIO line; A side drives B side on the translator.
    if (s_state.cfg.dir_active_high) {
        dedic_gpio_bundle_write(s_state.dir_bundle, s_state.dir_mask_out, s_state.dir_mask_out);
    } else {
        dedic_gpio_bundle_write(s_state.dir_bundle, s_state.dir_mask_out, 0);
    }
}

static IRAM_ATTR inline void swd_dir_drive_in(void)
{
    // DIR=low -> target drives the line; B side drives A side back to S3.
    if (s_state.cfg.dir_active_high) {
        dedic_gpio_bundle_write(s_state.dir_bundle, s_state.dir_mask_out, 0);
    } else {
        dedic_gpio_bundle_write(s_state.dir_bundle, s_state.dir_mask_out, s_state.dir_mask_out);
    }
}

// ----------------------------------------------------------------------------
// SPI2 low-level transaction primitives.
// ----------------------------------------------------------------------------
//
// The hot path bypasses spi_master and writes register fields directly,
// using spi_ll_* helpers as much as possible. Non-DMA mode is used because
// SWD frames are small (<= 64 bits) and fit in the SPI hardware FIFO; DMA
// setup overhead would dominate.

static IRAM_ATTR inline void swd_ll_apply_and_start(spi_dev_t *hw)
{
    spi_ll_apply_config(hw);
    spi_ll_user_start(hw);
    while (!spi_ll_usr_is_done(hw)) { /* spin */ }
    spi_ll_clear_intr(hw, SPI_LL_INTR_TRANS_DONE);
}

// Send `bits` bits of `data` over MOSI with MISO disabled. Used for header
// out, write data out, line-reset sequences, idle clock generation.
static IRAM_ATTR void swd_send_bits(uint32_t data_lo, uint32_t data_hi, int bits)
{
    spi_dev_t *hw = s_state.hw;
    // Configure: enable MOSI, disable MISO. Stay in 3-wire / sio mode so MOSI
    // is on the d pin (SWDIO).
    hw->user.usr_command = 0;
    hw->user.usr_addr = 0;
    hw->user.usr_dummy = 0;
    hw->user.usr_mosi = 1;
    hw->user.usr_miso = 0;
    spi_ll_set_mosi_bitlen(hw, bits);
    hw->data_buf[0] = data_lo;
    if (bits > 32) {
        hw->data_buf[1] = data_hi;
    }
    swd_ll_apply_and_start(hw);
}

// Receive `bits` bits via the SIO routing on the MOSI pin (3-wire half-duplex).
// Returns the value in two 32-bit halves; high half is undefined for bits <= 32.
static IRAM_ATTR void swd_recv_bits(uint32_t *out_lo, uint32_t *out_hi, int bits)
{
    spi_dev_t *hw = s_state.hw;
    hw->user.usr_command = 0;
    hw->user.usr_addr = 0;
    hw->user.usr_dummy = 0;
    hw->user.usr_mosi = 0;
    hw->user.usr_miso = 1;
    spi_ll_set_miso_bitlen(hw, bits);
    swd_ll_apply_and_start(hw);
    *out_lo = hw->data_buf[0];
    if (bits > 32) {
        *out_hi = hw->data_buf[1];
    } else {
        *out_hi = 0;
    }
}

// ----------------------------------------------------------------------------
// SWD frame primitives.
// ----------------------------------------------------------------------------

static IRAM_ATTR uint8_t swd_parity32(uint32_t v)
{
    v ^= v >> 16;
    v ^= v >> 8;
    v ^= v >> 4;
    v ^= v >> 2;
    v ^= v >> 1;
    return (uint8_t)(v & 1);
}

// SWD frame phases are split into two separate SPI transactions because the
// 74LVC1T45 translator needs its DIR pin flipped synchronously with the bus
// direction change. A single SPI transaction with `usr_mosi=1, usr_miso=1`
// would internally tri-state MOSI for the MISO phase, but the translator's
// DIR pin is on a separate GPIO and cannot be driven from inside the SPI
// peripheral - it must follow.
//
// Per-frame structure:
//   1. DIR=out, send 8-bit header (TX-only transaction)
//   2. DIR=in,  receive Trn(1) + ACK(3) + Trn-or-data (RX-only transaction)
//   3. DIR=out (or stays in for read-data), send/receive payload
//
// The DIR strobe between transactions is one dedic_gpio cycle (~4 ns at
// 240 MHz CPU), which falls inside one SWD bit cycle even at 40 MHz SCLK.
// This keeps the translator and SPI peripheral synchronised without the
// idle-clock waste that gpio_set_level() would have caused.

// Send 8-bit header (TX-only, MOSI driven, DIR must be=out before call).
static IRAM_ATTR void swd_phase_send_header(uint8_t header)
{
    spi_dev_t *hw = s_state.hw;
    hw->user.usr_command = 0;
    hw->user.usr_addr = 0;
    hw->user.usr_dummy = 0;
    hw->user.usr_mosi = 1;
    hw->user.usr_miso = 0;
    spi_ll_set_mosi_bitlen(hw, 8);
    hw->data_buf[0] = (uint32_t)header;
    swd_ll_apply_and_start(hw);
}

// Receive Trn(1) + ACK(3) + (extra Trn for read=1, no Trn for write=0).
// Returns the 3-bit ACK code.
static IRAM_ATTR uint8_t swd_phase_recv_ack(int trn_after_ack)
{
    spi_dev_t *hw = s_state.hw;
    hw->user.usr_command = 0;
    hw->user.usr_addr = 0;
    hw->user.usr_dummy = 0;
    hw->user.usr_mosi = 0;
    hw->user.usr_miso = 1;
    spi_ll_set_miso_bitlen(hw, 1 + 3 + trn_after_ack);
    swd_ll_apply_and_start(hw);
    uint32_t lo = hw->data_buf[0];
    // Bit 0 is Trn (junk), bits 1..3 are ACK.
    return (uint8_t)((lo >> 1) & 0x07);
}

// ----------------------------------------------------------------------------
// Public API.
// ----------------------------------------------------------------------------

swd_config_t swd_default_config(void)
{
    swd_config_t cfg = {
        .pin_swclk = 10,
        .pin_swdio = SPI2_IOMUX_PIN_NUM_MOSI,   // GPIO11
        .pin_dir = 12,
        .pin_nrst = 14,
        .default_clock_hz = 10 * 1000 * 1000,
        .dir_active_high = true,
    };
    return cfg;
}

esp_err_t swd_init(const swd_config_t *config)
{
    if (s_state.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    swd_config_t cfg = config ? *config : swd_default_config();
    if (cfg.default_clock_hz == 0) {
        cfg.default_clock_hz = 10 * 1000 * 1000;
    }

    // Step 1: claim SPI2 + GDMA via spi_master. We don't use spi_master for
    // transactions; we just need it to set up the GDMA channel binding, the
    // peripheral clock gate and the bus-mutex.
    spi_bus_config_t buscfg = {
        .mosi_io_num = cfg.pin_swdio,
        .miso_io_num = -1,            // 3-wire half-duplex
        .sclk_io_num = cfg.pin_swclk,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 16,        // SWD frames <= 64 bits, plenty
    };
    esp_err_t err = spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "spi_bus_initialize failed: %s", esp_err_to_name(err));
        return err;
    }
    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = (int)cfg.default_clock_hz,
        .mode = 0,
        .spics_io_num = -1,
        .queue_size = 1,
        .input_delay_ns = 0,
        .flags = SPI_DEVICE_HALFDUPLEX | SPI_DEVICE_3WIRE,
    };
    err = spi_bus_add_device(SPI2_HOST, &devcfg, &s_state.spi_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "spi_bus_add_device failed: %s", esp_err_to_name(err));
        spi_bus_free(SPI2_HOST);
        return err;
    }

    // Acquire the bus mutex permanently so spi_master is locked out and we
    // own the peripheral. spi_device_acquire_bus blocks any other queued
    // transactions until released.
    err = spi_device_acquire_bus(s_state.spi_dev, portMAX_DELAY);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "spi_device_acquire_bus failed: %s", esp_err_to_name(err));
        spi_bus_remove_device(s_state.spi_dev);
        spi_bus_free(SPI2_HOST);
        return err;
    }

    s_state.hw = SPI_LL_GET_HW(SPI2_HOST);

    // Step 2: re-bind pins. spi_bus_initialize routed SCLK and MOSI on its
    // own; ensure SCLK is on cfg.pin_swclk (which may not be the IOMUX pin)
    // and SWDIO is on cfg.pin_swdio with IOMUX direct if possible.
    swd_bind_sclk(cfg.pin_swclk);
    err = swd_bind_swdio_iomux(cfg.pin_swdio);
    if (err != ESP_OK) {
        spi_device_release_bus(s_state.spi_dev);
        spi_bus_remove_device(s_state.spi_dev);
        spi_bus_free(SPI2_HOST);
        return err;
    }

    // Step 3: configure base SPI2 settings for SWD framing.
    // - LSB-first byte order (SWD is LSB on the wire)
    // - mode 0 (CPOL=0 CPHA=0); SWD samples on rising edge
    // - half-duplex 3-wire (sio=1)
    spi_ll_set_mosi_bitlen(s_state.hw, 8 - 1);
    spi_ll_set_miso_bitlen(s_state.hw, 8 - 1);
    s_state.hw->user.sio = 1;
    s_state.hw->user.doutdin = 0;
    // LSB out and in; SWD is LSB on the wire. ESP32-S3 SPI uses 2-bit fields
    // for bit-order with the LSB-first encoding being value 1.
    s_state.hw->ctrl.wr_bit_order = 1;
    s_state.hw->ctrl.rd_bit_order = 1;
    // Byte-order field is hardcoded little-endian on ESP32-S3 (no register).
    spi_ll_apply_config(s_state.hw);

    // Step 4: dedic_gpio bundle for the DIR strobe.
    err = swd_setup_dir_pin(cfg.pin_dir, &s_state.dir_bundle, &s_state.dir_mask_out);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "dedic_gpio bundle setup failed: %s", esp_err_to_name(err));
        spi_device_release_bus(s_state.spi_dev);
        spi_bus_remove_device(s_state.spi_dev);
        spi_bus_free(SPI2_HOST);
        return err;
    }

    // Step 5: nRST pin. Open-drain, idle high (released).
    if (cfg.pin_nrst >= 0) {
        gpio_config_t io = {
            .pin_bit_mask = 1ULL << cfg.pin_nrst,
            .mode = GPIO_MODE_OUTPUT_OD,
            .pull_up_en = GPIO_PULLUP_ENABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        gpio_config(&io);
        gpio_set_level(cfg.pin_nrst, 1);
    }

    s_state.cfg = cfg;
    s_state.initialised = true;
    s_state.transfers_total = 0;

    // Apply requested clock.
    err = swd_set_clock_hz(cfg.default_clock_hz);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "set_clock_hz(%" PRIu32 ") = %s; continuing", cfg.default_clock_hz, esp_err_to_name(err));
    }

    // Start with SWDIO direction = output (host drives during header phase).
    swd_dir_drive_out();

    ESP_LOGI(TAG, "init: SCLK=GPIO%d (matrix), SWDIO=GPIO%d (iomux=%s), DIR=GPIO%d (dedic), nRST=GPIO%d, clk=%" PRIu32 " Hz",
             cfg.pin_swclk, cfg.pin_swdio,
             cfg.pin_swdio == SPI2_IOMUX_PIN_NUM_MOSI ? "yes" : "no",
             cfg.pin_dir, cfg.pin_nrst, s_state.realised_clock_hz);
    return ESP_OK;
}

esp_err_t swd_deinit(void)
{
    if (!s_state.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_state.dir_bundle) {
        dedic_gpio_del_bundle(s_state.dir_bundle);
        s_state.dir_bundle = NULL;
    }
    spi_device_release_bus(s_state.spi_dev);
    spi_bus_remove_device(s_state.spi_dev);
    spi_bus_free(SPI2_HOST);
    memset(&s_state, 0, sizeof(s_state));
    return ESP_OK;
}

esp_err_t swd_set_clock_hz(uint32_t hz)
{
    if (!s_state.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (hz == 0 || hz > 80 * 1000 * 1000) {
        hz = 40 * 1000 * 1000;
    }
    // 80 MHz APB source. spi_ll_master_set_clock searches for the closest
    // divider <= requested rate.
    int realised = spi_ll_master_set_clock(s_state.hw, 80 * 1000 * 1000, (int)hz, 50);
    spi_ll_apply_config(s_state.hw);
    s_state.realised_clock_hz = (realised > 0) ? (uint32_t)realised : 0;
    return ESP_OK;
}

uint32_t swd_get_clock_hz(void)
{
    return s_state.realised_clock_hz;
}

esp_err_t swd_set_nrst(bool released)
{
    if (!s_state.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_state.cfg.pin_nrst < 0) {
        return ESP_ERR_NOT_SUPPORTED;
    }
    gpio_set_level(s_state.cfg.pin_nrst, released ? 1 : 0);
    return ESP_OK;
}

esp_err_t swd_line_reset(void)
{
    if (!s_state.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    swd_dir_drive_out();
    // 56 SWCLKs with SWDIO held high (spec says >= 50; round up to a byte multiple).
    // Two 28-bit chunks fit in data_buf[0,1] but we already split at the helper.
    swd_send_bits(0xFFFFFFFFu, 0xFFFFFFFFu, 56);
    // 16 idle cycles low.
    swd_send_bits(0, 0, 16);
    return ESP_OK;
}

swd_status_t swd_transfer(uint8_t header, const uint32_t *data_in, uint32_t *data_out)
{
    if (!s_state.initialised) {
        return SWD_STATUS_PROTOCOL;
    }
    bool is_read = (header >> 2) & 1;     // RnW

    // Phase 1: DIR=out, send 8-bit header.
    swd_dir_drive_out();
    swd_phase_send_header(header);

    // Phase 2: DIR=in, receive Trn(1) + ACK(3) + (Trn(1) if read).
    // The line is naturally in turnaround at this boundary; flipping DIR
    // synchronously hands it over to the target.
    swd_dir_drive_in();
    uint8_t ack = swd_phase_recv_ack(is_read ? 1 : 0);

    if (ack != SWD_STATUS_OK) {
        // Per ARM IHI 0031: on WAIT or FAULT the target releases the line
        // after ACK. Issue Trn cycle and return.
        // Drive idle clocks to complete the abort sequence.
        swd_dir_drive_out();
        swd_send_bits(0, 0, 8);
        s_state.transfers_total++;
        if (ack == SWD_STATUS_WAIT || ack == SWD_STATUS_FAULT) {
            return (swd_status_t)ack;
        }
        return SWD_STATUS_PROTOCOL;
    }

    if (is_read) {
        // Still DIR=in; target drives data + parity.
        uint32_t lo, hi;
        swd_recv_bits(&lo, &hi, 33);
        uint32_t v = lo;
        uint8_t parity_rx = (uint8_t)(hi & 1);
        if (data_out) {
            *data_out = v;
        }
        // Turn the line around: DIR=out, then 8 idle clocks.
        swd_dir_drive_out();
        swd_send_bits(0, 0, 8);
        s_state.transfers_total++;
        if (parity_rx != swd_parity32(v)) {
            return SWD_STATUS_PARITY_ERR;
        }
        return SWD_STATUS_OK;
    }

    // Write: DIR=out, send 32 data + 1 parity.
    swd_dir_drive_out();
    uint32_t v = data_in ? *data_in : 0;
    uint8_t par = swd_parity32(v);
    swd_send_bits(v, (uint32_t)par, 33);
    swd_send_bits(0, 0, 8);
    s_state.transfers_total++;
    return SWD_STATUS_OK;
}

bool swd_is_initialised(void)
{
    return s_state.initialised;
}

uint32_t swd_transfers_total(void)
{
    return s_state.transfers_total;
}
