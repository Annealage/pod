// Annealage Pod: SWO trace capture pipeline public API.
//
// WS-D scope per plan/phase-2-parallel-implementation.md.
//
// Pipeline (spec.md §4.7):
//   UART1 RX  -> UHCI DMA  -> tier-1 DRAM ring (64 KB)
//                                |
//                                v
//                         tier-2 PSRAM ring (8 MB) (drained by APP_CPU task)
//                                |
//                                v
//                        swo_read() consumer (CMSIS-DAP SWO endpoint)
//
// UHCI cannot DMA directly into PSRAM, so the two-tier ring is mandatory.
// On overflow we set DAP_SWO_BUFFER_OVERRUN (bookkept by the WS-C protocol
// layer; we expose the counter via swo_overruns_total()) and keep streaming.
//
// SWO uses NRZ UART encoding (Manchester not implemented; per the survey,
// no real-world SWO host requires Manchester).

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    SWO_MODE_OFF = 0,
    SWO_MODE_UART = 2,           // matches CMSIS-DAP SWO mode encoding
} swo_mode_t;

typedef struct {
    int pin_swo_rx;              // GPIO for UART1 RX (default 13 per Appendix A)
    int uart_port;               // UART_NUM_1 by default
    size_t tier1_dram_bytes;     // tier-1 DRAM ring size; default 64 KB
    size_t tier2_psram_bytes;    // tier-2 PSRAM ring size; default 8 MB
    int drain_task_core;         // 1 (APP_CPU) per architecture.md
    int drain_task_priority;     // 7 per architecture.md
} swo_config_t;

swo_config_t swo_default_config(void);

// One-time pipeline allocation. Call once at boot. After init the engine is
// idle; call swo_start(baud, mode) to begin capture.
esp_err_t swo_init(uint32_t baud, swo_mode_t mode);

// Convenience wrapper to start capture at a new baud after init.
esp_err_t swo_start(uint32_t baud, swo_mode_t mode);

// Stop capture; PSRAM ring contents are preserved until next swo_start().
esp_err_t swo_stop(void);

// Tear down the pipeline; releases UHCI, UART driver, and the rings.
esp_err_t swo_deinit(void);

// Drain up to max_len bytes from the tier-2 PSRAM ring into buf. Returns the
// number of bytes copied. Returns 0 (not -1) when no bytes are available;
// returns -1 only on real error (uninitialised, etc).
ssize_t swo_read(uint8_t *buf, size_t max_len);

// Counters / observability.
uint32_t swo_overruns_total(void);    // monotonic count of tier-1 -> tier-2 overflows
size_t swo_bytes_buffered(void);      // tier-1 + tier-2 unread bytes
size_t swo_bytes_received(void);      // total bytes successfully captured (for tests)

// Reset the overrun flag without clearing the counter. CMSIS-DAP semantics
// expect SWO_Status to clear the latched overrun on read.
void swo_overrun_clear_latched(void);
bool swo_overrun_latched(void);

#ifdef __cplusplus
}
#endif
