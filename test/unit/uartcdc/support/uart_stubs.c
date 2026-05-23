/* mpy-pod uartcdc unit-test stubs for the IDF UART driver.
 *
 * Captures writes, injects reads, and records uart_param_config calls
 * so tests can assert correct line coding application.
 */

#include "driver/uart.h"

#include <string.h>
#include <stddef.h>

/* ---- recorded state -------------------------------------------------- */

uart_config_t g_last_uart_config;
int           g_uart_install_count;
int           g_uart_delete_count;
int           g_uart_param_config_count;

/* ---- TX capture buffer (bytes "written" to UART) -------------------- */

#define TX_CAP 512
static uint8_t  s_tx_buf[TX_CAP];
static size_t   s_tx_len;

/* ---- RX inject buffer (bytes returned by uart_read_bytes) ----------- */

#define RX_CAP 512
static uint8_t  s_rx_buf[RX_CAP];
static size_t   s_rx_len;

/* ---- test control API ----------------------------------------------- */

void uart_stub_reset(void)
{
    memset(&g_last_uart_config, 0, sizeof(g_last_uart_config));
    g_uart_install_count      = 0;
    g_uart_delete_count       = 0;
    g_uart_param_config_count = 0;
    s_tx_len = 0;
    s_rx_len = 0;
}

/* Inject bytes to be returned by the next uart_read_bytes call(s). */
void uart_stub_inject_rx(const uint8_t *data, size_t len)
{
    if (len > RX_CAP - s_rx_len) { len = RX_CAP - s_rx_len; }
    memcpy(s_rx_buf + s_rx_len, data, len);
    s_rx_len += len;
}

/* Drain bytes that were written via uart_write_bytes. Returns actual count. */
size_t uart_stub_drain_tx(uint8_t *out, size_t cap)
{
    size_t n = (s_tx_len < cap) ? s_tx_len : cap;
    memcpy(out, s_tx_buf, n);
    if (n < s_tx_len) {
        memmove(s_tx_buf, s_tx_buf + n, s_tx_len - n);
    }
    s_tx_len -= n;
    return n;
}

/* ---- IDF UART stubs ------------------------------------------------- */

esp_err_t uart_driver_install(uart_port_t uart_num, int rx_buf_size,
                               int tx_buf_size, int queue_size,
                               QueueHandle_t *queue, int intr_flags)
{
    (void)uart_num; (void)rx_buf_size; (void)tx_buf_size;
    (void)queue_size; (void)queue; (void)intr_flags;
    g_uart_install_count++;
    return ESP_OK;
}

esp_err_t uart_driver_delete(uart_port_t uart_num)
{
    (void)uart_num;
    g_uart_delete_count++;
    return ESP_OK;
}

esp_err_t uart_param_config(uart_port_t uart_num, const uart_config_t *cfg)
{
    (void)uart_num;
    g_last_uart_config = *cfg;
    g_uart_param_config_count++;
    return ESP_OK;
}

esp_err_t uart_set_pin(uart_port_t uart_num, int tx, int rx, int rts, int cts)
{
    (void)uart_num; (void)tx; (void)rx; (void)rts; (void)cts;
    return ESP_OK;
}

int uart_write_bytes(uart_port_t uart_num, const void *src, size_t size)
{
    (void)uart_num;
    if (size > TX_CAP - s_tx_len) { size = TX_CAP - s_tx_len; }
    memcpy(s_tx_buf + s_tx_len, src, size);
    s_tx_len += size;
    return (int)size;
}

int uart_read_bytes(uart_port_t uart_num, void *buf, uint32_t length,
                    TickType_t ticks_to_wait)
{
    (void)uart_num; (void)ticks_to_wait;
    size_t n = (s_rx_len < (size_t)length) ? s_rx_len : (size_t)length;
    if (n == 0) { return 0; }
    memcpy(buf, s_rx_buf, n);
    if (n < s_rx_len) {
        memmove(s_rx_buf, s_rx_buf + n, s_rx_len - n);
    }
    s_rx_len -= n;
    return (int)n;
}
