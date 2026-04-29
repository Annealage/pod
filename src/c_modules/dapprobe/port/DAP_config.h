/* Annealage Pod: CMSIS-DAP port-customisation header.
 *
 * Replaces vendor/cmsis-dap/Config/DAP_config.h on the include path. The
 * include order in micropython.cmake puts dapprobe/port/ ahead of
 * vendor/cmsis-dap/Config/ so this file wins when DAP.c does
 * `#include "DAP_config.h"`. The vendor copy is kept under vendor/
 * unmodified for archive purposes.
 *
 * Hooks the CMSIS-DAP protocol layer to:
 *  - WS-D's swd.h / swo.h engine (via port/swd_glue.c and port/swo_glue.c
 *    which provide the function-level entry points; this header still
 *    provides the bit-bang PIN_* macros that DAP.c references directly
 *    in DAP_SWJ_Pins, but they call into shims in port/swd_glue.c too).
 *  - mpy-pod identity strings.
 *
 * Configuration:
 *   DAP_SWD             = 1   SWD supported
 *   DAP_JTAG            = 0   JTAG not supported (rev1 scope)
 *   SWO_UART            = 1   SWO UART trace supported
 *   SWO_MANCHESTER      = 0   Manchester SWO not supported
 *   DAP_UART            = 0   target UART bridged separately via uartbridge,
 *                             not through CMSIS-DAP UART commands
 *   TIMESTAMP_CLOCK     = 0   timestamps not supported in rev1
 *
 * Reasoning for the choices is in docs/design/cmsis-dap.md §3.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Derived from ARM-software CMSIS-DAP `Firmware/Config/DAP_config.h`
 * (Copyright (c) 2013-2021 ARM Limited. All rights reserved.). Layout
 * and macro names preserved per the upstream contract; bodies are
 * mpy-pod specific.
 */

#ifndef __DAP_CONFIG_H__
#define __DAP_CONFIG_H__

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "cmsis_compiler.h"

/* The vendored DAP.h gates its C-only PIN_DELAY_SLOW implementation on
 * `defined(__CC_ARM)`, falling back to ARM Thumb inline assembly. Neither
 * xtensa GCC (ESP32-S3 firmware build) nor host GCC (unit tests) accept
 * that assembly. Define __CC_ARM here so the C fallback is selected.
 * The macro has no other effect in DAP.c or DAP_vendor.c. */
#ifndef __CC_ARM
#define __CC_ARM 1
#endif

/* ---------------------------------------------------------------------
 * Debug Unit identity
 * ------------------------------------------------------------------ */

#define CPU_CLOCK               240000000U   /* ESP32-S3 max core clock; informational only */
#define IO_PORT_WRITE_CYCLES    2U

#define DAP_SWD                 1
#define DAP_JTAG                0
#define DAP_JTAG_DEV_CNT        8U

#define DAP_DEFAULT_PORT        1U           /* SWD */
#define DAP_DEFAULT_SWJ_CLOCK   1000000U     /* 1 MHz default; pyOCD overrides */

/* CMSIS-DAP-v2 over USB-Bulk on FullSpeed.
 * EP MaxPacketSize at the descriptor layer is 64 (see synthetic_device.c),
 * but the protocol-level packet size can exceed that (the host fragments).
 * Match windowsair's setting (512) for compatibility with typical hosts. */
#define DAP_PACKET_SIZE         512U
#define DAP_PACKET_COUNT        4U

#define SWO_UART                1
#define SWO_UART_DRIVER         1
#define SWO_UART_MAX_BAUDRATE   6000000U     /* 6 Mbps target per spec.md §4.7 */
#define SWO_MANCHESTER          0
#define SWO_BUFFER_SIZE         8388608U     /* 8 MB tier-2 PSRAM ring; must be 2^n */
#define SWO_STREAM              0

#define TIMESTAMP_CLOCK         0U

#define DAP_UART                0
#define DAP_UART_DRIVER         1
#define DAP_UART_RX_BUFFER_SIZE 1024U
#define DAP_UART_TX_BUFFER_SIZE 1024U
#define DAP_UART_USB_COM_PORT   0

#define TARGET_FIXED            0
#define TARGET_DEVICE_VENDOR    ""
#define TARGET_DEVICE_NAME      ""
#define TARGET_BOARD_VENDOR     ""
#define TARGET_BOARD_NAME       ""

/* ---------------------------------------------------------------------
 * Identity strings returned by DAP_Info
 * ------------------------------------------------------------------ */

/* Returned strings are NUL-terminated and the length includes the NUL.
 * These are read from rom-resident constants populated by dap_core.c at
 * boot (the serial number is the lower 12 hex digits of the ESP32-S3
 * efuse MAC). */
extern const char *dap_port_vendor_string;
extern const char *dap_port_product_string;
extern const char *dap_port_serial_string;
extern const char *dap_port_fw_version_string;

__STATIC_INLINE uint8_t DAP_GetVendorString(char *str) {
    if (dap_port_vendor_string == NULL) { return 0U; }
    size_t n = strlen(dap_port_vendor_string);
    if (n >= 60U) { n = 59U; }
    memcpy(str, dap_port_vendor_string, n);
    str[n] = '\0';
    return (uint8_t)(n + 1U);
}

__STATIC_INLINE uint8_t DAP_GetProductString(char *str) {
    if (dap_port_product_string == NULL) { return 0U; }
    size_t n = strlen(dap_port_product_string);
    if (n >= 60U) { n = 59U; }
    memcpy(str, dap_port_product_string, n);
    str[n] = '\0';
    return (uint8_t)(n + 1U);
}

__STATIC_INLINE uint8_t DAP_GetSerNumString(char *str) {
    if (dap_port_serial_string == NULL) { return 0U; }
    size_t n = strlen(dap_port_serial_string);
    if (n >= 60U) { n = 59U; }
    memcpy(str, dap_port_serial_string, n);
    str[n] = '\0';
    return (uint8_t)(n + 1U);
}

__STATIC_INLINE uint8_t DAP_GetTargetDeviceVendorString(char *str) { (void)str; return 0U; }
__STATIC_INLINE uint8_t DAP_GetTargetDeviceNameString  (char *str) { (void)str; return 0U; }
__STATIC_INLINE uint8_t DAP_GetTargetBoardVendorString (char *str) { (void)str; return 0U; }
__STATIC_INLINE uint8_t DAP_GetTargetBoardNameString   (char *str) { (void)str; return 0U; }

__STATIC_INLINE uint8_t DAP_GetProductFirmwareVersionString(char *str) {
    if (dap_port_fw_version_string == NULL) { return 0U; }
    size_t n = strlen(dap_port_fw_version_string);
    if (n >= 60U) { n = 59U; }
    memcpy(str, dap_port_fw_version_string, n);
    str[n] = '\0';
    return (uint8_t)(n + 1U);
}

/* ---------------------------------------------------------------------
 * I/O pin macros
 *
 * DAP.c references these directly only in DAP_SWJ_Pins (manual SWCLK /
 * SWDIO drive for line-reset and similar) and indirectly through SW_DP.c
 * (which we replace). For SWJ_Pins we call into shims in swd_glue.c so
 * the engine's bookkeeping stays consistent.
 *
 * The SW_DP.c-replacement path uses the engine's swd_transfer() directly
 * and never touches these macros, so the bit-bang implementations below
 * are intentionally minimal: they exist so DAP.c links and so a host
 * doing line-reset via SWJ_Pins works correctly.
 * ------------------------------------------------------------------ */

extern uint32_t dap_port_pin_swclk_in (void);
extern void     dap_port_pin_swclk_set(void);
extern void     dap_port_pin_swclk_clr(void);
extern uint32_t dap_port_pin_swdio_in (void);
extern void     dap_port_pin_swdio_set(void);
extern void     dap_port_pin_swdio_clr(void);
extern void     dap_port_pin_swdio_out(uint32_t bit);
extern void     dap_port_pin_swdio_out_enable (void);
extern void     dap_port_pin_swdio_out_disable(void);
extern uint32_t dap_port_pin_nreset_in (void);
extern void     dap_port_pin_nreset_out(uint32_t bit);
extern void     dap_port_setup_swd     (void);
extern void     dap_port_setup_jtag    (void);
extern void     dap_port_off           (void);
extern void     dap_port_dap_setup     (void);
extern uint8_t  dap_port_reset_target  (void);
extern void     dap_port_led_connected (uint32_t bit);
extern void     dap_port_led_running   (uint32_t bit);

__STATIC_INLINE void PORT_JTAG_SETUP(void) { dap_port_setup_jtag(); }
__STATIC_INLINE void PORT_SWD_SETUP (void) { dap_port_setup_swd();  }
__STATIC_INLINE void PORT_OFF       (void) { dap_port_off();        }

__STATIC_FORCEINLINE uint32_t PIN_SWCLK_TCK_IN (void) { return dap_port_pin_swclk_in(); }
__STATIC_FORCEINLINE void     PIN_SWCLK_TCK_SET(void) { dap_port_pin_swclk_set(); }
__STATIC_FORCEINLINE void     PIN_SWCLK_TCK_CLR(void) { dap_port_pin_swclk_clr(); }

__STATIC_FORCEINLINE uint32_t PIN_SWDIO_TMS_IN (void) { return dap_port_pin_swdio_in(); }
__STATIC_FORCEINLINE void     PIN_SWDIO_TMS_SET(void) { dap_port_pin_swdio_set(); }
__STATIC_FORCEINLINE void     PIN_SWDIO_TMS_CLR(void) { dap_port_pin_swdio_clr(); }

__STATIC_FORCEINLINE uint32_t PIN_SWDIO_IN (void)              { return dap_port_pin_swdio_in(); }
__STATIC_FORCEINLINE void     PIN_SWDIO_OUT(uint32_t bit)      { dap_port_pin_swdio_out(bit); }
__STATIC_FORCEINLINE void     PIN_SWDIO_OUT_ENABLE (void)      { dap_port_pin_swdio_out_enable(); }
__STATIC_FORCEINLINE void     PIN_SWDIO_OUT_DISABLE(void)      { dap_port_pin_swdio_out_disable(); }

/* JTAG pins are unused (DAP_JTAG=0). DAP.c still references PIN_TDI_*,
 * PIN_TDO_IN, PIN_nTRST_* in DAP_SWJ_Pins; provide harmless stubs. */
__STATIC_FORCEINLINE uint32_t PIN_TDI_IN  (void)         { return 0U; }
__STATIC_FORCEINLINE void     PIN_TDI_OUT (uint32_t bit) { (void)bit; }
__STATIC_FORCEINLINE uint32_t PIN_TDO_IN  (void)         { return 0U; }
__STATIC_FORCEINLINE uint32_t PIN_nTRST_IN(void)         { return 1U; }
__STATIC_FORCEINLINE void     PIN_nTRST_OUT(uint32_t bit){ (void)bit; }

__STATIC_FORCEINLINE uint32_t PIN_nRESET_IN (void)         { return dap_port_pin_nreset_in(); }
__STATIC_FORCEINLINE void     PIN_nRESET_OUT(uint32_t bit) { dap_port_pin_nreset_out(bit); }

__STATIC_INLINE void LED_CONNECTED_OUT(uint32_t bit) { dap_port_led_connected(bit); }
__STATIC_INLINE void LED_RUNNING_OUT  (uint32_t bit) { dap_port_led_running(bit); }

__STATIC_INLINE uint32_t TIMESTAMP_GET(void) { return 0U; }

__STATIC_INLINE void DAP_SETUP(void)                { dap_port_dap_setup(); }
__STATIC_INLINE uint8_t RESET_TARGET(void)          { return dap_port_reset_target(); }

#endif /* __DAP_CONFIG_H__ */
