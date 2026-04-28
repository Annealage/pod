// Annealage Pod: telnet / RFC2217 decoder for the UART bridge.
//
// Pure logic, no IDF or FreeRTOS dependencies. Used by the bridge's
// tcp_rx task and exercised directly by the host-side unit tests.

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define UART_BRIDGE_TELNET_IAC  0xFF
#define UART_BRIDGE_TELNET_DONT 0xFE
#define UART_BRIDGE_TELNET_DO   0xFD
#define UART_BRIDGE_TELNET_WONT 0xFC
#define UART_BRIDGE_TELNET_WILL 0xFB
#define UART_BRIDGE_TELNET_SB   0xFA
#define UART_BRIDGE_TELNET_SE   0xF0

#define UART_BRIDGE_TELNET_OPT_COM_PORT 44

#define UART_BRIDGE_COM_PORT_SET_BAUDRATE 1
#define UART_BRIDGE_COM_PORT_SET_DATASIZE 2
#define UART_BRIDGE_COM_PORT_SET_PARITY   3
#define UART_BRIDGE_COM_PORT_SET_STOPSIZE 4

// Subnegotiation summary handed to the host callback when a SB..SE
// frame completes. Fields not set by the frame keep the value -1 to
// distinguish "not present" from "present, value zero".
typedef struct {
    int baud;       // valid when >= 0
    int data_bits;  // valid when in 5..8
    int parity;     // 0=NONE, 2=EVEN, 3=ODD; -1 when not set
    int stop_bits;  // 1 or 2; -1 when not set
} uart_bridge_telnet_sb_t;

typedef void (*uart_bridge_telnet_sb_cb_t)(const uart_bridge_telnet_sb_t *sb,
                                            void *user);

typedef struct {
    bool active;
    int state;        // 0 normal, 1 IAC, 2 cmd-arg, 3 SB, 4 SB+IAC
    uint8_t cmd;
    uint8_t sb_buf[16];
    size_t sb_len;
    uart_bridge_telnet_sb_cb_t sb_cb;
    void *sb_cb_user;
} uart_bridge_telnet_t;

void uart_bridge_telnet_init(uart_bridge_telnet_t *t, bool active,
                              uart_bridge_telnet_sb_cb_t cb, void *user);

// Decode `in_len` raw client bytes; emit the bytes that should be
// forwarded to the UART into `out` (at most `out_cap`). Returns the
// number of bytes written to `out`. When `active` is false, copies
// straight through.
size_t uart_bridge_telnet_filter(uart_bridge_telnet_t *t,
                                  const uint8_t *in, size_t in_len,
                                  uint8_t *out, size_t out_cap);

// Parse a SB..SE payload (without the leading IAC SB and trailing IAC
// SE bytes). Fills *out with the parsed parameters. Returns true if
// the payload is a recognised COM_PORT_OPTION frame, false otherwise.
bool uart_bridge_telnet_parse_sb(const uint8_t *sb, size_t len,
                                  uart_bridge_telnet_sb_t *out);

#ifdef __cplusplus
}
#endif
