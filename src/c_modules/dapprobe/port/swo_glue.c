/* Annealage Pod: CMSIS-DAP SWO glue.
 *
 * Replaces vendor/cmsis-dap/Source/SWO.c. The vendored SWO.c targets the
 * CMSIS USART Driver (Driver_USART#); on the ESP32-S3 we use UART1 via
 * UHCI behind WS-D's swo.h API instead. The wire-level CMSIS-DAP
 * commands (SWO_Transport, SWO_Mode, SWO_Baudrate, SWO_Control,
 * SWO_Status, SWO_ExtendedStatus, SWO_Data) are reimplemented here in
 * terms of swo_init / swo_start / swo_stop / swo_read.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Derived in part from ARM-software CMSIS-DAP `SWO.c` (Copyright (c)
 * 2013-2021 ARM Limited). The state-machine semantics (transport,
 * mode, capture-active flag, overrun bookkeeping) are preserved; the
 * UART/Manchester I/O paths were replaced.
 *
 * Function signatures match DAP.h verbatim so DAP.c links unchanged.
 */

#include <stdint.h>
#include <string.h>
#include <sys/types.h>

#include "DAP_config.h"
#include "DAP.h"

#include "swo.h"

/* SWO transport encodings (CMSIS-DAP DAP_SWO_Transport):
 *  0 = none (capture disabled)
 *  1 = read trace data via DAP_SWO_Data command
 *  2 = read trace data via separate WinUSB endpoint (EP3 in v2)
 * Synthetic device exposes EP3, so the host typically sets transport=2. */
static uint8_t s_transport;

/* SWO mode (CMSIS-DAP DAP_SWO_Mode): DAP_SWO_OFF, DAP_SWO_UART, DAP_SWO_MANCHESTER. */
static uint8_t s_mode;

/* CMSIS-DAP SWO Status flags (DAP_SWO_CAPTURE_ACTIVE, DAP_SWO_BUFFER_OVERRUN, ...). */
static uint8_t s_status_flags;

/* Cached host-requested baud. Returned from SWO_Baudrate even when the
 * engine clamps to a different realised rate. */
static uint32_t s_requested_baud;

/* Last-known trace count snapshot for SWO_Status / SWO_ExtendedStatus. */
static uint32_t s_count_snapshot;

/* ------ SWO_Transport ----------------------------------------------- */
uint32_t SWO_Transport(const uint8_t *request, uint8_t *response) {
    const uint8_t transport = *request;
    if (transport <= 2U) {
        s_transport = transport;
        *response = DAP_OK;
    } else {
        *response = DAP_ERROR;
    }
    return ((1U << 16) | 1U);
}

/* ------ SWO_Mode ---------------------------------------------------- */
uint32_t SWO_Mode(const uint8_t *request, uint8_t *response) {
    const uint8_t mode = *request;
    *response = DAP_OK;

    if (mode == DAP_SWO_OFF) {
        (void)swo_stop();
        s_mode = DAP_SWO_OFF;
        s_status_flags &= (uint8_t)~DAP_SWO_CAPTURE_ACTIVE;
    } else if (mode == DAP_SWO_UART) {
#if (SWO_UART != 0)
        s_mode = DAP_SWO_UART;
        /* Capture starts with SWO_Control(1); SWO_Mode just selects
         * the encoding. The engine's swo_init brought up UART1+UHCI
         * already at boot time. */
#else
        *response = DAP_ERROR;
#endif
    } else if (mode == DAP_SWO_MANCHESTER) {
#if (SWO_MANCHESTER != 0)
        s_mode = DAP_SWO_MANCHESTER;
#else
        *response = DAP_ERROR;
#endif
    } else {
        *response = DAP_ERROR;
    }
    return ((1U << 16) | 1U);
}

/* ------ SWO_Baudrate ------------------------------------------------ */
uint32_t SWO_Baudrate(const uint8_t *request, uint8_t *response) {
    s_requested_baud = (uint32_t)(*(request+0) <<  0) |
                       (uint32_t)(*(request+1) <<  8) |
                       (uint32_t)(*(request+2) << 16) |
                       (uint32_t)(*(request+3) << 24);

    /* swo_start applies the new baud regardless of current state.
     * Realised baud is the same value (UART1 baud divider has fine
     * granularity at FullSpeed-equivalent rates), so we report back
     * what was requested. */
    uint32_t reported = 0U;
    if (s_mode == DAP_SWO_UART && s_requested_baud > 0U) {
        if (swo_start(s_requested_baud, SWO_MODE_UART) == 0) {
            reported = s_requested_baud;
        }
    } else {
        reported = s_requested_baud;
    }
    response[0] = (uint8_t)(reported >>  0);
    response[1] = (uint8_t)(reported >>  8);
    response[2] = (uint8_t)(reported >> 16);
    response[3] = (uint8_t)(reported >> 24);
    return ((4U << 16) | 4U);
}

/* ------ SWO_Control ------------------------------------------------- */
uint32_t SWO_Control(const uint8_t *request, uint8_t *response) {
    const uint8_t active = *request & 1U;
    *response = DAP_OK;

    if (active) {
        if (s_mode == DAP_SWO_UART && s_requested_baud > 0U) {
            if (swo_start(s_requested_baud, SWO_MODE_UART) != 0) {
                *response = DAP_ERROR;
            } else {
                s_status_flags |= DAP_SWO_CAPTURE_ACTIVE;
            }
        } else {
            *response = DAP_ERROR;
        }
    } else {
        (void)swo_stop();
        s_status_flags &= (uint8_t)~DAP_SWO_CAPTURE_ACTIVE;
    }
    return ((1U << 16) | 1U);
}

/* ------ SWO_Status -------------------------------------------------- */
uint32_t SWO_Status(uint8_t *response) {
    /* Latch the overrun flag if the engine recorded an overflow since
     * the last status read. CMSIS-DAP semantics: SWO_Status clears the
     * overrun flag on read. */
    if (swo_overrun_latched()) {
        s_status_flags |= DAP_SWO_BUFFER_OVERRUN;
        swo_overrun_clear_latched();
    }

    s_count_snapshot = (uint32_t)swo_bytes_buffered();

    response[0] = s_status_flags;
    response[1] = (uint8_t)(s_count_snapshot >>  0);
    response[2] = (uint8_t)(s_count_snapshot >>  8);
    response[3] = (uint8_t)(s_count_snapshot >> 16);
    response[4] = (uint8_t)(s_count_snapshot >> 24);

    /* Clear the overrun bit after reporting it; CMSIS-DAP says
     * "DAP_SWO_BUFFER_OVERRUN is cleared on the next SWO_Status query
     * if no further overrun has occurred". */
    s_status_flags &= (uint8_t)~DAP_SWO_BUFFER_OVERRUN;
    return 5U;
}

/* ------ SWO_ExtendedStatus ----------------------------------------- */
uint32_t SWO_ExtendedStatus(const uint8_t *request, uint8_t *response) {
    const uint8_t control = *request;

    if (swo_overrun_latched()) {
        s_status_flags |= DAP_SWO_BUFFER_OVERRUN;
        swo_overrun_clear_latched();
    }

    uint8_t flags = s_status_flags;
    s_status_flags &= (uint8_t)~DAP_SWO_BUFFER_OVERRUN;

    s_count_snapshot = (uint32_t)swo_bytes_buffered();

    /* Layout: status(1) + [count(4)] + [index(4)] + [tdtr(4)] depending
     * on `control` bits (bit0 = include count, bit1 = include index,
     * bit2 = include TDTR). We always include count, never index/TDTR
     * because TIMESTAMP_CLOCK=0 in this port. */
    size_t n = 0U;
    response[n++] = flags;
    if (control & (1U << 0)) {
        response[n++] = (uint8_t)(s_count_snapshot >>  0);
        response[n++] = (uint8_t)(s_count_snapshot >>  8);
        response[n++] = (uint8_t)(s_count_snapshot >> 16);
        response[n++] = (uint8_t)(s_count_snapshot >> 24);
    }
    if (control & (1U << 1)) {
        response[n++] = 0U; response[n++] = 0U; response[n++] = 0U; response[n++] = 0U;
    }
    if (control & (1U << 2)) {
        response[n++] = 0U; response[n++] = 0U; response[n++] = 0U; response[n++] = 0U;
    }
    return ((1U << 16) | (uint32_t)n);
}

/* ------ SWO_Data ---------------------------------------------------- */
uint32_t SWO_Data(const uint8_t *request, uint8_t *response) {
    /* Host requested up to wLength bytes; we drain the tier-2 ring. */
    uint32_t want = (uint32_t)(*(request+0) <<  0) | (uint32_t)(*(request+1) <<  8);

    if (swo_overrun_latched()) {
        s_status_flags |= DAP_SWO_BUFFER_OVERRUN;
        swo_overrun_clear_latched();
    }

    uint8_t flags = s_status_flags;
    s_status_flags &= (uint8_t)~DAP_SWO_BUFFER_OVERRUN;

    /* Cap at what fits in the response buffer. The CMSIS-DAP spec caps
     * the total response at DAP_PACKET_SIZE - 5 (status + count + data),
     * but the call site in DAP.c does not enforce that, so we trust
     * `want` from the host within reason. */
    uint8_t *data = response + 5;
    if (want > (DAP_PACKET_SIZE - 5U)) {
        want = (uint32_t)(DAP_PACKET_SIZE - 5U);
    }
    ssize_t n = 0;
    if (want > 0U) {
        n = swo_read(data, (size_t)want);
        if (n < 0) { n = 0; }
    }

    response[0] = flags;
    response[1] = (uint8_t)((uint32_t)n >>  0);
    response[2] = (uint8_t)((uint32_t)n >>  8);
    response[3] = (uint8_t)((uint32_t)n >> 16);
    response[4] = (uint8_t)((uint32_t)n >> 24);
    return ((2U << 16) | (5U + (uint32_t)n));
}

/* ---- helpers exposed back to dap_core --------------------------------- */
uint8_t  swo_glue_get_transport(void) { return s_transport; }
uint8_t  swo_glue_get_mode(void)      { return s_mode; }
uint32_t swo_glue_get_baud(void)      { return s_requested_baud; }

void swo_glue_reset(void) {
    s_transport = 0U;
    s_mode = DAP_SWO_OFF;
    s_status_flags = 0U;
    s_requested_baud = 0U;
    s_count_snapshot = 0U;
}
