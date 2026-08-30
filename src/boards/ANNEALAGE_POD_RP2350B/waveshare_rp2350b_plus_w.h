/*
 * Copyright (c) 2020 Raspberry Pi (Trading) Ltd.
 *
 * SPDX-License-Identifier: BSD-3-Clause
 */

// -----------------------------------------------------
// NOTE: THIS HEADER IS ALSO INCLUDED BY ASSEMBLER SO
//       SHOULD ONLY CONSIST OF PREPROCESSOR DIRECTIVES
// -----------------------------------------------------

// pico-sdk board definition for the Waveshare RP2350B-Plus-W.
//
// The upstream pico-sdk has no header for this board, so it is carried here.
// Pin assignments are taken from the vendor schematic
// (files.waveshare.com/wiki/RP2350B-Plus-W/RP2350B-Plus-W.pdf), not from
// Waveshare's own demo header, which sets PICO_VSYS_PIN to 43; the schematic
// routes VSYS_SENSE to GPIO46 (ADC6) and GPIO43 is a plain bottom pad.
//
// Board summary: RP2350B (QFN-80, GPIO0-47), 16 MB W25Q128 QSPI flash,
// unpopulated QSPI-PSRAM footprint on XIP CS1 = GPIO47, Raspberry Pi RM2
// (CYW43439) radio on GPIO36-39, USB-C, no battery charger, no user button.

#ifndef _BOARDS_WAVESHARE_RP2350B_PLUS_W_H
#define _BOARDS_WAVESHARE_RP2350B_PLUS_W_H

pico_board_cmake_set(PICO_PLATFORM, rp2350)
pico_board_cmake_set(PICO_CYW43_SUPPORTED, 1)

// For board detection
#define WAVESHARE_RP2350B_PLUS_W

// --- RP2350 VARIANT ---
// QFN-80 part: 48 GPIOs. This also turns on PICO_PIO_USE_GPIO_BASE, which the
// CYW43 PIO SPI driver needs to reach the radio pins at GPIO36-39.
#define PICO_RP2350A 0

// --- UART ---
#ifndef PICO_DEFAULT_UART
#define PICO_DEFAULT_UART 0
#endif
#ifndef PICO_DEFAULT_UART_TX_PIN
#define PICO_DEFAULT_UART_TX_PIN 0
#endif
#ifndef PICO_DEFAULT_UART_RX_PIN
#define PICO_DEFAULT_UART_RX_PIN 1
#endif

// --- LED ---
// Two LEDs: LED2 is on RP2350B GPIO23, LED1 is on the RM2's WL_GPIO0 (see the
// CYW43 section). PICO_DEFAULT_LED_PIN names the MCU-side one.
#ifndef PICO_DEFAULT_LED_PIN
#define PICO_DEFAULT_LED_PIN 23
#endif
// no PICO_DEFAULT_WS2812_PIN - the board has no addressable LED

// --- I2C ---
#ifndef PICO_DEFAULT_I2C
#define PICO_DEFAULT_I2C 0
#endif
#ifndef PICO_DEFAULT_I2C_SDA_PIN
#define PICO_DEFAULT_I2C_SDA_PIN 4
#endif
#ifndef PICO_DEFAULT_I2C_SCL_PIN
#define PICO_DEFAULT_I2C_SCL_PIN 5
#endif

// --- SPI ---
#ifndef PICO_DEFAULT_SPI
#define PICO_DEFAULT_SPI 0
#endif
#ifndef PICO_DEFAULT_SPI_SCK_PIN
#define PICO_DEFAULT_SPI_SCK_PIN 18
#endif
#ifndef PICO_DEFAULT_SPI_TX_PIN
#define PICO_DEFAULT_SPI_TX_PIN 19
#endif
#ifndef PICO_DEFAULT_SPI_RX_PIN
#define PICO_DEFAULT_SPI_RX_PIN 16
#endif
#ifndef PICO_DEFAULT_SPI_CSN_PIN
#define PICO_DEFAULT_SPI_CSN_PIN 17
#endif

// --- FLASH ---
// U2 = W25Q128JVSIQ, 16 MB, on QSPI CS0.
#define PICO_BOOT_STAGE2_CHOOSE_W25Q080 1

#ifndef PICO_FLASH_SPI_CLKDIV
#define PICO_FLASH_SPI_CLKDIV 2
#endif

pico_board_cmake_set_default(PICO_FLASH_SIZE_BYTES, (16 * 1024 * 1024))
#ifndef PICO_FLASH_SIZE_BYTES
#define PICO_FLASH_SIZE_BYTES (16 * 1024 * 1024)
#endif

// --- PSRAM ---
// U1 is an unpopulated 8-pin QSPI-PSRAM footprint sharing the flash bus, with
// its own chip select on GPIO47 (the XIP CS1 alt-function pin). Only the CS
// differs from the flash; R8 (10K pull-up) and C4 are already fitted.
// Consumed by MICROPY_HW_PSRAM_CS_PIN in mpconfigboard.h.
#ifndef WAVESHARE_RP2350B_PLUS_W_PSRAM_CS_PIN
#define WAVESHARE_RP2350B_PLUS_W_PSRAM_CS_PIN 47
#endif

// --- VSYS / VBUS ---
// VSYS is sensed on GPIO46 (ADC6) through a 20K/10K divider (ratio 3), gated by
// Q1. VBUS presence is not wired to an RP2350 GPIO at all - it goes to the RM2's
// GPIO2, so it is read through the CYW43 (CYW43_WL_GPIO_VBUS_PIN below).
#ifndef PICO_VSYS_PIN
#define PICO_VSYS_PIN 46
#endif

pico_board_cmake_set_default(PICO_RP2350_A2_SUPPORTED, 1)
#ifndef PICO_RP2350_A2_SUPPORTED
#define PICO_RP2350_A2_SUPPORTED 1
#endif

// --- CYW43 (Raspberry Pi RM2 module, CYW43439) ---
// The radio sits on GPIO36-39, not on the Pico W's GPIO23-29. All three PIO-
// driven pins (DATA_OUT, DATA_IN, CLOCK) are >= 16, which satisfies the CYW43
// driver's single-GPIO-base requirement: the SDK claims a PIO and sets its
// GPIO base to 16 so the 16-47 window covers them.

// gpio pin to power up the cyw43 chip (drives both WLON and BTON)
#ifndef CYW43_DEFAULT_PIN_WL_REG_ON
#define CYW43_DEFAULT_PIN_WL_REG_ON 36u
#endif

// gpio pin for spi data out to the cyw43 chip
#ifndef CYW43_DEFAULT_PIN_WL_DATA_OUT
#define CYW43_DEFAULT_PIN_WL_DATA_OUT 37u
#endif

// gpio pin for spi data in from the cyw43 chip (half-duplex, shared with DATA_OUT)
#ifndef CYW43_DEFAULT_PIN_WL_DATA_IN
#define CYW43_DEFAULT_PIN_WL_DATA_IN 37u
#endif

// gpio (irq) pin for the irq line from the cyw43 chip (shared, via R21)
#ifndef CYW43_DEFAULT_PIN_WL_HOST_WAKE
#define CYW43_DEFAULT_PIN_WL_HOST_WAKE 37u
#endif

// gpio pin for the spi clock line to the cyw43 chip
#ifndef CYW43_DEFAULT_PIN_WL_CLOCK
#define CYW43_DEFAULT_PIN_WL_CLOCK 39u
#endif

// gpio pin for the spi chip select to the cyw43 chip
#ifndef CYW43_DEFAULT_PIN_WL_CS
#define CYW43_DEFAULT_PIN_WL_CS 38u
#endif

#ifndef CYW43_WL_GPIO_COUNT
#define CYW43_WL_GPIO_COUNT 3
#endif

// LED1 hangs off the RM2's GPIO0.
#ifndef CYW43_WL_GPIO_LED_PIN
#define CYW43_WL_GPIO_LED_PIN 0
#endif

// VBUS presence is read on the CYW43's GPIO2 rather than an RP2350 pin.
#ifndef CYW43_WL_GPIO_VBUS_PIN
#define CYW43_WL_GPIO_VBUS_PIN 2
#endif

// The CYW43 shares the VSYS sense arrangement, so VSYS reads must be bracketed
// by cyw43_thread_enter / cyw43_thread_exit.
#ifndef CYW43_USES_VSYS_PIN
#define CYW43_USES_VSYS_PIN 1
#endif

// The board regulates 3V3 with an ME6217C33M5G LDO, so there is no SMPS
// power-save mode pin to drive (the RM2's GPIO1 is left unconnected). Do not
// define PICO_SMPS_MODE_PIN.

#endif
