// Annealage Pod: TCP log socket C shim public API.
//
// Listens on a configurable TCP port; on accept, every byte that
// would normally go to the UART0 console (via esp_log_set_vprintf
// and stdout fan-out) is also written to the connected client.
//
// See spec.md §6.1 and docs/design/ops.md for the fan-out model.

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Default TCP log port. Spec §5.3 lists 514 in the optional table;
// using TCP/514 here since UDP/514 is the standard syslog port and
// TCP/514 keeps the line-buffered semantics callers want for
// interactive log streaming.
#define OPS_LOG_DEFAULT_PORT 514

// Start the TCP log listener on `port`. Idempotent: a second call
// with the same port is a no-op; a call with a different port
// returns ESP_ERR_INVALID_STATE.
//
// Once started, ESP_LOGx output and any stdout writes that pass
// through the fan-out vprintf are mirrored to the connected client
// (if any). UART0 output is preserved unconditionally.
//
// `task_core` selects the CPU core to pin the accept task to;
// pass -1 for "no affinity" (FreeRTOS picks).
esp_err_t ops_log_start(uint16_t port, int task_core);

// Stop the listener, drop the active client, and restore the
// previous esp_log vprintf hook. Safe to call when already stopped.
esp_err_t ops_log_stop(void);

// 0 if no client connected, 1 otherwise. Always succeeds.
int ops_log_client_count(void);

// Test hook: feed bytes through the fan-out path as if ESP_LOG had
// emitted them. Used by unit tests to verify the fan-out without
// going through esp_log_set_vprintf.
void ops_log_test_fanout(const char *buf, size_t len);

// Test hook: drain bytes the fan-out wrote to the (virtual) client
// FIFO. Returns the number of bytes copied into `out`. Available
// only when the listener is running in test mode (port == 0).
size_t ops_log_test_drain(uint8_t *out, size_t cap);

#ifdef __cplusplus
}
#endif
