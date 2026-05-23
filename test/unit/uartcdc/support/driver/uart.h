/* mpy-pod uartcdc unit-test shim for IDF driver/uart.h.
 *
 * Provides just enough type and macro definitions to compile
 * uart_cdc_device.c on the host without the ESP-IDF tree. Function
 * bodies live in support/uart_stubs.c.
 */

#ifndef MPY_POD_TEST_DRIVER_UART_H
#define MPY_POD_TEST_DRIVER_UART_H

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef int uart_port_t;
typedef uint32_t TickType_t;
typedef void *QueueHandle_t;

typedef enum {
    UART_DATA_5_BITS = 0,
    UART_DATA_6_BITS = 1,
    UART_DATA_7_BITS = 2,
    UART_DATA_8_BITS = 3,
} uart_word_length_t;

typedef enum {
    UART_STOP_BITS_1   = 1,
    UART_STOP_BITS_1_5 = 2,
    UART_STOP_BITS_2   = 3,
} uart_stop_bits_t;

typedef enum {
    UART_PARITY_DISABLE = 0,
    UART_PARITY_EVEN    = 2,
    UART_PARITY_ODD     = 3,
} uart_parity_t;

typedef enum {
    UART_HW_FLOWCTRL_DISABLE = 0,
    UART_HW_FLOWCTRL_RTS     = 1,
    UART_HW_FLOWCTRL_CTS     = 2,
    UART_HW_FLOWCTRL_CTS_RTS = 3,
} uart_hw_flowcontrol_t;

typedef enum {
    UART_SCLK_DEFAULT = 0,
} uart_sclk_t;

typedef struct {
    int                   baud_rate;
    uart_word_length_t    data_bits;
    uart_parity_t         parity;
    uart_stop_bits_t      stop_bits;
    uart_hw_flowcontrol_t flow_ctrl;
    int                   rx_flow_ctrl_thresh;
    uart_sclk_t           source_clk;
} uart_config_t;

#define UART_PIN_NO_CHANGE (-1)

esp_err_t uart_driver_install(uart_port_t uart_num, int rx_buf_size,
                               int tx_buf_size, int queue_size,
                               QueueHandle_t *uart_queue, int intr_alloc_flags);
esp_err_t uart_driver_delete(uart_port_t uart_num);
esp_err_t uart_param_config(uart_port_t uart_num, const uart_config_t *uart_config);
esp_err_t uart_set_pin(uart_port_t uart_num, int tx_io_num, int rx_io_num,
                        int rts_io_num, int cts_io_num);
int uart_write_bytes(uart_port_t uart_num, const void *src, size_t size);
int uart_read_bytes(uart_port_t uart_num, void *buf, uint32_t length,
                    TickType_t ticks_to_wait);

#endif /* MPY_POD_TEST_DRIVER_UART_H */
