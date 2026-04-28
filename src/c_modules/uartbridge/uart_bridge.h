// Annealage Pod: UART bridge public API.
//
// Forwards bytes between an ESP32-S3 UART (default UART2 per Appendix A
// pinmap) and a TCP client. One TCP listener, one client at a time.
// Owns its FreeRTOS tasks, lifecycle managed via uart_bridge_start /
// uart_bridge_stop.
//
// All entry points return 0 on success and a negative ESP_ERR-style
// value on failure (see uart_bridge.c). This header is consumed by the
// MicroPython binding in moduartbridge.c and by the unit tests.

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Parity codes match ESP-IDF uart_parity_t encoding so the bridge can
// pass them straight into uart_param_config(). Kept as ints in the
// public API to avoid leaking IDF headers to MP-binding consumers.
#define UART_BRIDGE_PARITY_NONE 0
#define UART_BRIDGE_PARITY_EVEN 2
#define UART_BRIDGE_PARITY_ODD  3

typedef struct {
    int uart_num;        // 0..UART_NUM_MAX-1; default 2
    int tcp_port;        // default 2000
    int baud;            // 300..921600; default 115200
    int data_bits;       // 5..8; default 8
    int parity;          // UART_BRIDGE_PARITY_*; default NONE
    int stop_bits;       // 1 or 2; default 1
    bool flow_control;   // RTS/CTS hardware flow control; default false
    int tx_pin;          // GPIO; -1 keeps current/default
    int rx_pin;          // GPIO; -1 keeps current/default
    int rts_pin;         // GPIO; -1 if no flow control
    int cts_pin;         // GPIO; -1 if no flow control
    bool replace_client; // true: new connect closes old session
    bool telnet;         // RFC2217-ish IAC framing; default false
    int task_core;       // CPU core to pin tasks to; default 0 (PRO_CPU)
    int rx_buf_size;     // UART RX ringbuffer (bytes); 0 = default 2048
    int tx_buf_size;     // UART TX ringbuffer (bytes); 0 = default 2048
} uart_bridge_config_t;

// Initialise config struct with project defaults (UART2, port 2000,
// 115200 8N1, no flow control, raw mode, replace existing client).
void uart_bridge_config_default(uart_bridge_config_t *cfg);

// Start the bridge. Brings up the UART, opens the TCP listener, spawns
// the accept and (on demand) RX/TX tasks. Idempotent: a second call
// with the same config is a no-op; a call with a different config
// returns ESP_ERR_INVALID_STATE (caller must stop first).
esp_err_t uart_bridge_start(const uart_bridge_config_t *cfg);

// Stop the bridge. Closes the listener, terminates tasks, drops any
// connected client, uninstalls the UART driver. Safe to call when
// already stopped.
esp_err_t uart_bridge_stop(void);

// Snapshot of the running configuration. Returns ESP_ERR_INVALID_STATE
// when the bridge is stopped.
esp_err_t uart_bridge_get_config(uart_bridge_config_t *out);

// 0 if no client connected, 1 otherwise. Always succeeds.
int uart_bridge_client_count(void);

// Test hook: bypass the TCP listener and run a single bridging session
// over a caller-provided file descriptor. Used by unit tests to
// validate the byte-shovel state machine without going through accept.
// Blocks until the client_fd reads return 0/EOF or the bridge is
// stopped via uart_bridge_stop().
esp_err_t uart_bridge_run_session_fd(int client_fd);

// Sentinel UART number selecting the in-memory test loopback. When
// uart_bridge_start() is called with this value the bridge does not
// touch the IDF UART driver; instead bytes flow through the test
// inject/drain helpers below. Used by the protocol-level unit tests.
#define UART_BRIDGE_UART_TEST_LOOPBACK (-2)

// Test hook: inject bytes as if they had arrived on the UART RX FIFO.
// Wakes the UART-RX-to-TCP-TX task. Returns the number of bytes
// queued, or -1 if the bridge is not in test mode.
int uart_bridge_test_inject_rx(const uint8_t *buf, size_t len);

// Test hook: drain bytes the TCP-RX-to-UART-TX task wrote toward the
// (virtual) UART. Non-blocking. Returns the number of bytes copied
// into buf, or -1 if the bridge is not in test mode.
int uart_bridge_test_drain_tx(uint8_t *buf, size_t cap);

// Test hook: wait for the TX-side test FIFO to contain at least
// min_bytes, with a timeout in milliseconds. Returns 0 on success,
// ESP_ERR_TIMEOUT on timeout, ESP_ERR_INVALID_STATE if not in test
// mode.
esp_err_t uart_bridge_test_wait_tx(size_t min_bytes, uint32_t timeout_ms);

#ifdef __cplusplus
}
#endif
