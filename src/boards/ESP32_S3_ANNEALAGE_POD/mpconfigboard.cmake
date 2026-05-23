# Board variant definition for ESP32_S3_ANNEALAGE_POD.
#
# Pulls in the standard MicroPython esp32 sdkconfig fragments for an
# ESP32-S3 with octal PSRAM (N16R8), then overlays this board's
# sdkconfig.board for partition table, USB-host stack, task watchdog
# and bootloader rollback.

set(IDF_TARGET esp32s3)

set(SDKCONFIG_DEFAULTS
    boards/sdkconfig.base
    boards/sdkconfig.ble
    boards/sdkconfig.240mhz
    boards/sdkconfig.spiram_sx
    boards/sdkconfig.spiram_oct
    ${MICROPY_BOARD_DIR}/sdkconfig.board
)

# Phase 3 P3.0 pivot to TinyUSB host via andrewleech/micropython#7.
# Submodule pin machine-usbhost-mpypod carries the two unblocking
# fixes:
#   (1) CFG_TUSB_OS_INC_PATH trailing space in tinyusb_port/
#       tusb_config.h (commit c6d91f044).
#   (2) mp_usbh.h forward-decl ordering of machine_usbh_*_obj_t
#       typedefs (commit acb3c4645).
# R27: under TinyUSB host (replacing IDF host on this branch) the
# traceISR_EXIT_TO_SCHEDULER macro-visibility bug DOES reproduce in
# the micropython.elf secondary build (which omits -DESP_PLATFORM):
# osal_freertos.h's osal_semaphore_post() expands portYIELD_FROM_ISR()
# into traceISR_EXIT_TO_SCHEDULER() at parse time. usbhost.c
# pre-defines the macro to a no-op before any FreeRTOS include; see
# src/c_modules/slaveio/slaveio.c lines 48-59 for the same pattern.
# (On main with the IDF host backend the bug did not surface because
# usbhost.c did not include any TinyUSB headers; on this branch the
# inclusion of host/usbh.h pulls osal_freertos.h transitively.)
set(MICROPY_HW_USB_HOST 1)
list(APPEND MICROPY_DEF_BOARD MICROPY_HW_USB_HOST=1)

# Disable TinyUSB class drivers so they don't claim DUT interfaces.
# We forward raw URBs via usbhost.c; the class drivers (CDC/MSC/HID)
# are not used.  These definitions must reach ALL compilation units
# including machine_usb_host.c (extmod) and mp_usbh.c (shared/tinyusb),
# not just usbhost.c.  MICROPY_DEF_BOARD is passed to
# target_compile_definitions(${MICROPY_TARGET} PUBLIC ...) in
# esp32_common.cmake which covers the __idf_main component that
# compiles those files. Without these the QSTR extractor sees
# CFG_TUH_MSC=0 but the compiler sees CFG_TUH_MSC=2 (TinyUSB default),
# causing 'MP_QSTR_USBH_MSC undeclared' build errors.
list(APPEND MICROPY_DEF_BOARD CFG_TUH_CDC=0 CFG_TUH_MSC=0 CFG_TUH_HID=0)

# Enable the generic endpoint-transfer API (tuh_edpt_open / tuh_edpt_xfer).
# Without this, usbh_edpt_xfer_with_callback (usbh.c:1015 in this TinyUSB
# pin) silently drops the user's complete_cb (lines 1017-1018:
# `(void) complete_cb;`), so bulk/interrupt transfers complete in
# TinyUSB but never call back to our responder. URBs sit in our
# inflight list, kernel times out, cdc-acm cancels everything with
# -ECONNRESET, TTY is removed by kernel.
# This is gotcha #1 from r24-wip-history.md - non-negotiable for raw-URB
# forwarding via tuh_edpt_xfer.
list(APPEND MICROPY_DEF_BOARD CFG_TUH_API_EDPT_XFER=1)

# R27 fs-cp deadlock trace (Phase 1 step 3). Enables the per-URB
# sub:/cb:/synth:/watchdog: log lines in usbhost.c. Default off in
# production; uncomment the next line to enable the trace overhead.
list(APPEND MICROPY_DEF_BOARD R27_DEADLOCK_TRACE=1)

# Freeze the annealage_pod Python package into the firmware image.
set(MICROPY_FROZEN_MANIFEST ${MICROPY_BOARD_DIR}/manifest.py)
