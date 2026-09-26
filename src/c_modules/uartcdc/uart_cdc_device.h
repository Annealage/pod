/* Annealage Pod: synthetic CDC ACM virtual USB device.
 *
 * Implements virtual_device_t for the usbip multiplexer (WS-A). The
 * device exposes a USB CDC ACM serial port whose data endpoints bridge
 * to the ESP32-S3 UART driver, replacing the TCP socket uart_bridge
 * when built with ANNEALAGE_POD_UART_BACKEND=usbip (the default).
 *
 * The Linux cdc-acm driver enumerates the device as /dev/ttyACMx.
 * SET_LINE_CODING is forwarded to uart_param_config() so baud/parity
 * changes from the host take effect on the physical UART.
 *
 * SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Annealage-firmware-exception
 */

#ifndef MPY_POD_UART_CDC_DEVICE_H
#define MPY_POD_UART_CDC_DEVICE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "../usbip/virtual_device.h"

#ifdef __cplusplus
extern "C" {
#endif

/* USB identifiers. PID is one above the dapprobe (0xF00A) so both can
 * be imported simultaneously by the same usbip host. */
#define UART_CDC_VID  0xC251u
#define UART_CDC_PID  0xF00Bu

/* Raw descriptor sizes exposed for unit tests. */
#define UART_CDC_DEVICE_DESC_LEN  18u
/* 9 config + 8 IAD + 9 ctrl-itf + 5+5+4+5 functional + 7 ctrl-EP +
 * 9 data-itf + 7 out-EP + 7 in-EP = 75 */
#define UART_CDC_CONFIG_DESC_LEN  75u

/* EP addresses (with direction bit). */
#define UART_CDC_EP_NOTIFY_IN   0x81u  /* EP1 IN  interrupt, SERIAL_STATE */
#define UART_CDC_EP_DATA_OUT    0x02u  /* EP2 OUT bulk, host -> UART TX   */
#define UART_CDC_EP_DATA_IN     0x83u  /* EP3 IN  bulk, UART RX -> host   */

/* Register the synthetic device with the usbip virtual-device registry
 * and install the IDF UART driver.
 *
 * uart_num : IDF UART port number (0 .. UART_NUM_MAX-1).
 * tx_pin   : GPIO for TX; UART_PIN_NO_CHANGE (-1) keeps the current pin.
 * rx_pin   : GPIO for RX; UART_PIN_NO_CHANGE (-1) keeps the current pin.
 * baud     : Initial baud rate (e.g. 115200).
 *
 * Returns 0 on success, negative errno on error (-EEXIST if already
 * registered, -EIO if the UART driver fails to install). */
int uart_cdc_attach(int uart_num, int tx_pin, int rx_pin, int baud);

/* Uninstall the UART driver. The virtual-device registry slot stays
 * occupied until reboot (append-only in rev1). */
int uart_cdc_detach(void);

bool uart_cdc_is_attached(void);

void uart_cdc_set_verbose(bool enable);
bool uart_cdc_is_verbose(void);

/* Flush the UART RX FIFO and software buffer.  Call after a DUT reset to
 * clear break/framing-error bytes latched during the DUT TX reset transient. */
void uart_cdc_flush_rx(void);

/* Test hooks: raw descriptor blobs. */
const uint8_t *uart_cdc_get_device_desc(size_t *out_len);
const uint8_t *uart_cdc_get_config_desc(size_t *out_len);
const uint8_t *uart_cdc_get_string_desc(uint8_t index, size_t *out_len);

/* Test hook: returns the static virtual_device_t (populated after first
 * uart_cdc_attach() call or explicit uart_cdc_init_for_test()). */
virtual_device_t *uart_cdc_get_device(void);

#ifdef MPY_POD_HOST_TEST_BUILD
/* Test hook: reset all static state so uart_cdc_attach() can be called
 * again in a fresh test. Must be paired with
 * usbip_virtual_device_reset_for_test(). */
void uart_cdc_reset_for_test(void);
#endif

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_UART_CDC_DEVICE_H */
