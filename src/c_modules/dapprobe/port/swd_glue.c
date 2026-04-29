/* Annealage Pod: CMSIS-DAP SWD glue.
 *
 * Replaces vendor/cmsis-dap/Source/SW_DP.c. The vendored SW_DP.c is a
 * pure GPIO bit-bang implementation that loops over PIN_DELAY_SLOW per
 * SWCLK half-cycle; on the ESP32-S3 that caps SWD throughput at single-
 * digit MHz (Phase 0.3 spike, research/spi2-swd-benchmark.md). This
 * file provides the same public functions (SWD_Transfer, SWJ_Sequence,
 * SWD_Sequence) but routes them through WS-D's swd_transfer() which
 * uses the SPI2 + GDMA backend and the dedic_gpio DIR strobe.
 *
 * The dap_port_pin_* shims referenced from port/DAP_config.h live here
 * too, expressed in terms of swd.h primitives.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Derived in part from ARM-software/CMSIS-DAP `SW_DP.c` (Copyright (c)
 * 2013-2017 ARM Limited). The bit-level transfer state machine was
 * replaced; the function signatures preserved so DAP.c links unchanged.
 */

#include <stdint.h>
#include <string.h>

#include "DAP_config.h"
#include "DAP.h"

#include "swd.h"

/* ---------------------------------------------------------------------
 * dap_port_*  (called from DAP_config.h's PIN_* / PORT_* / LED_* / etc.)
 *
 * Most of these have no direct equivalent on the SPI-DMA SWD backend
 * (the engine never exposes single-pin toggles after init). We expose
 * the few that DAP_SWJ_Pins genuinely needs: nRST control via
 * swd_set_nrst, and a synthetic SWCLK/SWDIO state read so the SWJ_Pins
 * "current pin states" reply is correct.
 *
 * For PIN_SWDIO_OUT_ENABLE / DISABLE we rely on the engine's own DIR
 * strobe, so these become no-ops on this port; the engine flips DIR
 * inside swd_transfer() on each turnaround.
 * ------------------------------------------------------------------ */

static uint32_t s_pin_swclk_state = 1U;
static uint32_t s_pin_swdio_state = 1U;
static uint32_t s_pin_nreset_state = 1U;

uint32_t dap_port_pin_swclk_in(void)  { return s_pin_swclk_state; }
void     dap_port_pin_swclk_set(void) { s_pin_swclk_state = 1U; }
void     dap_port_pin_swclk_clr(void) { s_pin_swclk_state = 0U; }

uint32_t dap_port_pin_swdio_in(void)  { return s_pin_swdio_state; }
void     dap_port_pin_swdio_set(void) { s_pin_swdio_state = 1U; }
void     dap_port_pin_swdio_clr(void) { s_pin_swdio_state = 0U; }

void dap_port_pin_swdio_out(uint32_t bit) { s_pin_swdio_state = bit & 1U; }
void dap_port_pin_swdio_out_enable (void) { /* DIR strobe is engine-owned */ }
void dap_port_pin_swdio_out_disable(void) { /* DIR strobe is engine-owned */ }

uint32_t dap_port_pin_nreset_in(void) { return s_pin_nreset_state; }
void     dap_port_pin_nreset_out(uint32_t bit) {
    s_pin_nreset_state = bit & 1U;
    /* swd_set_nrst convention: true = released (high-Z), false = asserted (low). */
    (void)swd_set_nrst((bit & 1U) != 0U);
}

void dap_port_setup_swd(void) {
    /* Engine init is performed at module attach time (dap_core_init).
     * Reaching here means DAP_Connect was called; ensure the DIR strobe
     * is in the output direction by issuing a 0-clock idle through the
     * engine. For now this is a no-op because dap_core_init has already
     * brought the engine up. */
}

void dap_port_setup_jtag(void) { /* DAP_JTAG=0; never called */ }
void dap_port_off(void) { (void)swd_set_nrst(true); /* release nRST */ }

void dap_port_dap_setup(void) {
    /* DAP.c calls DAP_SETUP() once during DAP_Setup(). The engine is
     * brought up earlier from dap_core_init(); nothing to do here. */
}

uint8_t dap_port_reset_target(void) {
    /* No device-specific reset sequence implemented; return 0 to tell
     * the host to fall back to AIRCR.SYSRESETREQ via SWD. */
    return 0U;
}

/* LED hooks: dap_core.c plugs these to a notifier so MP can report on
 * the connection state. Phase 2: bookkeeping only, no GPIO.
 *
 * The real LED control sits in the WS-E `annealage_pod.*` MP package; we
 * provide a function-pointer level of indirection so this TU does not
 * link against MP. */
typedef void (*dap_port_led_cb_t)(uint32_t);
static dap_port_led_cb_t s_led_connected_cb;
static dap_port_led_cb_t s_led_running_cb;

void dap_port_led_connected(uint32_t bit) {
    if (s_led_connected_cb) { s_led_connected_cb(bit); }
}
void dap_port_led_running(uint32_t bit) {
    if (s_led_running_cb) { s_led_running_cb(bit); }
}
void dap_port_set_led_callbacks(dap_port_led_cb_t connected, dap_port_led_cb_t running) {
    s_led_connected_cb = connected;
    s_led_running_cb   = running;
}

/* ---------------------------------------------------------------------
 * SWJ_Sequence / SWD_Sequence / SWD_Transfer
 * ------------------------------------------------------------------ */

/* SWJ_Sequence: emit `count` bits LSB-first from `data` on the SWDIO
 * line with SWCLK toggling. Used by hosts to issue line resets, the
 * JTAG-to-SWD switch sequence, the dormant-to-SWD selection alert
 * (128 bits) and the SW-DP activation code (8 bits).
 *
 * `count == 0` means 256 bits per the CMSIS-DAP spec.
 */
void SWJ_Sequence(uint32_t count, const uint8_t *data) {
    if (!swd_is_initialised() || data == NULL) {
        return;
    }
    if (count == 0U) {
        count = 256U;
    }
    (void)swd_swj_send_bits(data, count);
}

/* SWD_Sequence: emit or capture a bit sequence on SWDIO. The vendored
 * DAP_SWD_Sequence handler iterates over the host-supplied list of
 * sub-sequences and calls this function once per sub-sequence with the
 * sequence info byte (count in bits 0..5, direction in bit 7). RP2040
 * multi-drop bring-up uses this to write the TARGETSEL register without
 * needing an ACK back from the DP.
 *
 *   info bit 7 = 1 -> capture from SWDIO into `swdi`
 *   info bit 7 = 0 -> drive `swdo` onto SWDIO
 *   info bits 0..5 = bit count; 0 means 64.
 */
void SWD_Sequence(uint32_t info, const uint8_t *swdo, uint8_t *swdi) {
    if (!swd_is_initialised()) {
        if (swdi != NULL) {
            uint32_t count = info & SWD_SEQUENCE_CLK;
            if (count == 0U) { count = 64U; }
            memset(swdi, 0, (size_t)((count + 7U) >> 3));
        }
        return;
    }
    uint32_t count = info & SWD_SEQUENCE_CLK;
    if (count == 0U) { count = 64U; }
    if ((info & SWD_SEQUENCE_DIN) != 0U) {
        (void)swd_seq_in_bits(swdi, count);
    } else {
        (void)swd_seq_out_bits(swdo, count);
    }
}

/* SWD_Transfer: the workhorse. Compose the 8-bit SWD packet header,
 * call swd_transfer, translate the engine's swd_status_t into the
 * CMSIS-DAP DAP_TRANSFER_* response code.
 *
 * `request` is the CMSIS-DAP request byte:
 *   bit0 = APnDP, bit1 = RnW, bit2 = A2, bit3 = A3
 *   bit4 = MATCH_VALUE, bit5 = MATCH_MASK, bit7 = TIMESTAMP
 *
 * MATCH_VALUE / MATCH_MASK / TIMESTAMP are handled in DAP.c around the
 * SWD_Transfer call; this function treats them as not-our-concern.
 */
uint8_t SWD_Transfer(uint32_t request, uint32_t *data) {
    if (!swd_is_initialised()) {
        return DAP_TRANSFER_ERROR;
    }

    const bool ap_n_dp = (request & DAP_TRANSFER_APnDP) != 0U;
    const bool r_n_w   = (request & DAP_TRANSFER_RnW) != 0U;
    /* CMSIS-DAP request byte places A2 in bit2, A3 in bit3.
     * swd_make_header expects a2_a3 packed as { A2 in bit0, A3 in bit1 }. */
    const uint8_t a2_a3 = (uint8_t)(((request >> 2) & 1U) | (((request >> 3) & 1U) << 1));
    const uint8_t header = swd_make_header(ap_n_dp, r_n_w, a2_a3);

    uint32_t value = 0U;
    swd_status_t st;
    if (r_n_w) {
        st = swd_transfer(header, NULL, &value);
        if (data != NULL) {
            *data = value;
        }
    } else {
        const uint32_t out = (data != NULL) ? *data : 0U;
        st = swd_transfer(header, &out, NULL);
    }

    /* Map engine status back to CMSIS-DAP DAP_TRANSFER_* codes.
     * The CMSIS-DAP DAP_TRANSFER_* values are not the same numeric set
     * as swd_status_t (the swd_status_t bit order matches the SWD ack
     * lines, while CMSIS-DAP uses bitfields). swd.h chose bit values
     * that already match DAP_TRANSFER_OK = 1, DAP_TRANSFER_WAIT = 2,
     * DAP_TRANSFER_FAULT = 4, DAP_TRANSFER_ERROR = 8 (parity), so we
     * forward directly except SWD_STATUS_PROTOCOL which maps to
     * DAP_TRANSFER_ERROR (the host treats both identically). */
    switch (st) {
        case SWD_STATUS_OK:         return DAP_TRANSFER_OK;
        case SWD_STATUS_WAIT:       return DAP_TRANSFER_WAIT;
        case SWD_STATUS_FAULT:      return DAP_TRANSFER_FAULT;
        case SWD_STATUS_PARITY_ERR: return DAP_TRANSFER_ERROR;
        case SWD_STATUS_PROTOCOL:   return DAP_TRANSFER_ERROR;
        default:                    return DAP_TRANSFER_ERROR;
    }
}
