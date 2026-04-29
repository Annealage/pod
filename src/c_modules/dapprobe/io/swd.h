// Annealage Pod: SWD I/O engine public API.
//
// WS-D scope per plan/phase-2-parallel-implementation.md. Owns the SPI2 +
// GDMA SWD backend on ESP32-S3, and the dedic_gpio-driven DIR strobe for
// the SWDIO direction-controlled translator.
//
// This header is the API surface that WS-C (CMSIS-DAP protocol layer) calls
// into. It is intentionally narrow; the only callers are inside dapprobe/.
//
// Pinout (spec-appendix-A-pinmap.md §A.5.1):
//   GPIO10 SWCLK     SPI2 CLK (GPIO matrix; native iomux pin is 12)
//   GPIO11 SWDIO     SPI2 D    (IO_MUX direct; SPI2_IOMUX_PIN_NUM_MOSI)
//   GPIO12 SWDIO_DIR translator DIR; dedic_gpio bundle channel 0
//   GPIO14 nRST      open-drain reset (managed via swd_set_nrst())
//
// Backend: direct HAL/register programming of SPI2 (windowsair pattern,
// ported to ESP32-S3 register layout). Rationale and trade-offs in
// docs/design/swd-swo-engine.md.

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// SWD ack codes (CMSIS-DAP convention).
typedef enum {
    SWD_STATUS_OK         = 0x01,
    SWD_STATUS_WAIT       = 0x02,
    SWD_STATUS_FAULT      = 0x04,
    SWD_STATUS_PARITY_ERR = 0x08, // host-side parity check failure on read
    SWD_STATUS_PROTOCOL   = 0x10, // protocol error (no ack / bad ack)
} swd_status_t;

// SWD transfer header bits, packed per the SWD-DP spec.
//   bit0 = always 1 (start)
//   bit1 = APnDP
//   bit2 = RnW
//   bit3 = A2
//   bit4 = A3
//   bit5 = parity over bits 1..4
//   bit6 = always 0 (stop)
//   bit7 = always 1 (park)
// The caller is expected to compose this byte; the engine sends it on the wire
// as-is and decodes the ACK separately.
//
// Convenience composer:
static inline uint8_t swd_make_header(bool ap_n_dp, bool r_n_w, uint8_t a2_a3) {
    uint8_t b0 = 1;                              // start
    uint8_t b1 = ap_n_dp ? 1 : 0;                // APnDP
    uint8_t b2 = r_n_w  ? 1 : 0;                 // RnW
    uint8_t b3 = (a2_a3 >> 0) & 1;               // A2
    uint8_t b4 = (a2_a3 >> 1) & 1;               // A3
    uint8_t parity = (b1 ^ b2 ^ b3 ^ b4) & 1;
    return (uint8_t)(b0 | (b1 << 1) | (b2 << 2) | (b3 << 3) | (b4 << 4) | (parity << 5) | (0 << 6) | (1 << 7));
}

// Configuration for swd_init().
typedef struct {
    int pin_swclk;        // SCLK GPIO number, default 10
    int pin_swdio;        // SWDIO GPIO number, default 11; must be SPI2 native IOMUX MOSI for max clock
    int pin_dir;          // SWDIO_DIR GPIO number, default 12
    int pin_nrst;         // nRST GPIO number, default 14; -1 disables
    uint32_t default_clock_hz;  // initial SCLK; clamped to internal range. 0 means use default.
    bool dir_active_high; // DIR=1 means "S3 drives line out" (translator A->B); rev-board specific
} swd_config_t;

// Get a populated config struct with the spec defaults filled in.
swd_config_t swd_default_config(void);

// Initialise the SWD engine.
//
// Allocates SPI2 + GDMA, sets up the dedic_gpio bundle for DIR, configures
// the SWDIO MOSI pin via IO_MUX direct, routes SWCLK via the GPIO matrix.
// Idempotent: calling again returns ESP_ERR_INVALID_STATE.
esp_err_t swd_init(const swd_config_t *config);

// De-init (release SPI2, free dedic_gpio bundle, reset pins).
esp_err_t swd_deinit(void);

// Set the SWD bus clock. The actual frequency is rounded down to the nearest
// achievable divider of the 80 MHz APB clock. 0 selects "as fast as possible".
esp_err_t swd_set_clock_hz(uint32_t hz);

// Read the realised SWD bus clock (0 if not initialised).
uint32_t swd_get_clock_hz(void);

// Drive the optional nRST line. true = released (high-Z), false = asserted (low).
// When pin_nrst < 0 in init, this is a no-op returning ESP_ERR_NOT_SUPPORTED.
esp_err_t swd_set_nrst(bool released);

// Send a SWD line-reset sequence: at least 50 SWCLKs with SWDIO held high,
// followed by 16 idle cycles low (per ARM debug spec).
esp_err_t swd_line_reset(void);

// SWJ-stream primitive: drive `count` bits on SWDIO, LSB-first, sourced from
// `data`. SWCLK toggles for each bit. DIR is driven to "host drives line" for
// the duration. Used by the CMSIS-DAP DAP_SWJ_Sequence handler to emit
// dormant-to-SWD selection alerts, activation codes, JTAG-to-SWD switch
// patterns, line resets, etc. Length is in bits; spec caps at 256.
esp_err_t swd_swj_send_bits(const uint8_t *data, uint32_t bit_count);

// SWD-stream primitive (output): drive `count` bits on SWDIO, LSB-first, from
// `data`. DIR is driven to "host drives line" for the duration. Length capped
// at 64 bits per call (matches the CMSIS-DAP DAP_SWD_Sequence sub-sequence
// limit; longer sequences are decomposed by the caller).
esp_err_t swd_seq_out_bits(const uint8_t *data, uint32_t bit_count);

// SWD-stream primitive (input): clock `count` bits on SWCLK while sampling
// SWDIO, LSB-first. DIR is driven to "target drives line" for the duration.
// Bits are written to `data` LSB-first; trailing bits in the last byte are
// cleared. Length capped at 64 bits per call.
esp_err_t swd_seq_in_bits(uint8_t *data, uint32_t bit_count);

// SWD turnaround helper: drive `count` extra SWCLK cycles with the line in
// the current direction (no data driven by either side). Used between an
// output sub-sequence and an input sub-sequence to cover the bus turnaround
// the target expects.
esp_err_t swd_seq_idle(uint32_t bit_count, bool driving);

// Issue a single SWD transfer.
//
// header: the 8-bit SWD packet header (start/APnDP/RnW/addr/parity/stop/park).
// data_in: for write transfers, points to the 32-bit value to send. Ignored on read.
// data_out: for read transfers, receives the 32-bit value. Ignored on write.
//
// On read with SWD_STATUS_OK, a host-side parity check is performed and
// SWD_STATUS_PARITY_ERR is returned if it fails. data_out is still populated.
swd_status_t swd_transfer(uint8_t header, const uint32_t *data_in, uint32_t *data_out);

// Engine state queries.
bool swd_is_initialised(void);
uint32_t swd_transfers_total(void);     // monotonic counter, useful for tests/perf

// Enable per-transfer ESP_LOGW tracing for the next `count` SWD transfers.
// Each traced transfer logs the header, ACK raw word, ACK code, and (for
// reads) the captured data word and parity bits. Use this to diagnose
// wire-level protocol mismatches; default is 0 (no tracing).
void swd_set_trace(uint32_t count);

#ifdef __cplusplus
}
#endif
