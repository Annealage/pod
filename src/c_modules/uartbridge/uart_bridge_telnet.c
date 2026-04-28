// Annealage Pod: telnet / RFC2217 decoder implementation.

#include "uart_bridge_telnet.h"

#include <string.h>

void uart_bridge_telnet_init(uart_bridge_telnet_t *t, bool active,
                              uart_bridge_telnet_sb_cb_t cb, void *user) {
    memset(t, 0, sizeof(*t));
    t->active = active;
    t->sb_cb = cb;
    t->sb_cb_user = user;
}

bool uart_bridge_telnet_parse_sb(const uint8_t *sb, size_t len,
                                  uart_bridge_telnet_sb_t *out) {
    out->baud = -1;
    out->data_bits = -1;
    out->parity = -1;
    out->stop_bits = -1;
    if (len < 2 || sb[0] != UART_BRIDGE_TELNET_OPT_COM_PORT) {
        return false;
    }
    bool any = false;
    switch (sb[1]) {
        case UART_BRIDGE_COM_PORT_SET_BAUDRATE:
            if (len >= 6) {
                uint32_t baud = ((uint32_t)sb[2] << 24) | ((uint32_t)sb[3] << 16)
                              | ((uint32_t)sb[4] << 8) | (uint32_t)sb[5];
                out->baud = (int)baud;
                any = true;
            }
            break;
        case UART_BRIDGE_COM_PORT_SET_DATASIZE:
            if (len >= 3 && sb[2] >= 5 && sb[2] <= 8) {
                out->data_bits = sb[2];
                any = true;
            }
            break;
        case UART_BRIDGE_COM_PORT_SET_PARITY:
            if (len >= 3) {
                // RFC2217: 1=NONE, 2=ODD, 3=EVEN, 4=MARK, 5=SPACE.
                if (sb[2] == 1) { out->parity = 0; any = true; }
                else if (sb[2] == 2) { out->parity = 3; any = true; }
                else if (sb[2] == 3) { out->parity = 2; any = true; }
            }
            break;
        case UART_BRIDGE_COM_PORT_SET_STOPSIZE:
            if (len >= 3 && (sb[2] == 1 || sb[2] == 2)) {
                out->stop_bits = sb[2];
                any = true;
            }
            break;
        default:
            break;
    }
    return any;
}

size_t uart_bridge_telnet_filter(uart_bridge_telnet_t *t,
                                  const uint8_t *in, size_t in_len,
                                  uint8_t *out, size_t out_cap) {
    if (!t->active) {
        size_t n = (in_len < out_cap) ? in_len : out_cap;
        memcpy(out, in, n);
        return n;
    }
    size_t op = 0;
    for (size_t i = 0; i < in_len; ++i) {
        uint8_t b = in[i];
        switch (t->state) {
            case 0:
                if (b == UART_BRIDGE_TELNET_IAC) { t->state = 1; }
                else if (op < out_cap) { out[op++] = b; }
                break;
            case 1:
                if (b == UART_BRIDGE_TELNET_IAC) {
                    if (op < out_cap) { out[op++] = 0xFF; }
                    t->state = 0;
                    break;
                }
                t->cmd = b;
                if (b == UART_BRIDGE_TELNET_SB) { t->state = 3; t->sb_len = 0; }
                else if (b == UART_BRIDGE_TELNET_DO || b == UART_BRIDGE_TELNET_DONT
                         || b == UART_BRIDGE_TELNET_WILL || b == UART_BRIDGE_TELNET_WONT) {
                    t->state = 2;
                } else {
                    t->state = 0;
                }
                break;
            case 2:
                t->state = 0;
                break;
            case 3:
                if (b == UART_BRIDGE_TELNET_IAC) { t->state = 4; }
                else if (t->sb_len < sizeof(t->sb_buf)) { t->sb_buf[t->sb_len++] = b; }
                break;
            case 4:
                if (b == UART_BRIDGE_TELNET_SE) {
                    if (t->sb_cb != NULL) {
                        uart_bridge_telnet_sb_t sb;
                        if (uart_bridge_telnet_parse_sb(t->sb_buf, t->sb_len, &sb)) {
                            t->sb_cb(&sb, t->sb_cb_user);
                        }
                    }
                    t->sb_len = 0;
                    t->state = 0;
                } else if (b == UART_BRIDGE_TELNET_IAC) {
                    if (t->sb_len < sizeof(t->sb_buf)) { t->sb_buf[t->sb_len++] = 0xFF; }
                    t->state = 3;
                } else {
                    t->sb_len = 0;
                    t->state = 0;
                }
                break;
            default:
                t->state = 0;
                break;
        }
    }
    return op;
}
