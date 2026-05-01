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
# The previously suspected blocker (3), where usbhost.c was reported
# to fail its second (micropython.elf) compile because
# traceISR_EXIT_TO_SCHEDULER was not visible at osal_semaphore_post
# parse time, did not reproduce against this submodule pin in P3.9.
# Both compiles of usbhost.c (one into __idf_main, one into
# micropython.elf) now build clean and the firmware boots, enumerates
# the Pico DUT and pyocd reset --target rp2040 completes via the
# attached synthetic CMSIS-DAP. If the macro-scope bug recurs (e.g.
# after an IDF or TinyUSB submodule bump), the slaveio.c precedent at
# src/c_modules/slaveio/slaveio.c lines 48-59 documents the pre-define
# workaround.
set(MICROPY_HW_USB_HOST 1)
list(APPEND MICROPY_DEF_BOARD MICROPY_HW_USB_HOST=1)

# Disable TinyUSB class drivers. We don't use machine.USBHost's bundled
# CDC/MSC/HID class drivers; our usbip backend forwards raw URBs. These
# defines must reach BOTH the QSTR extractor and the compilation units
# (machine_usb_host.c, mp_usbh.c) — without them the QSTR extractor
# sees CFG_TUH_MSC=0 (default in qstr preprocessing context) but the
# compiler sees CFG_TUH_MSC=2 (TinyUSB default), causing
# 'MP_QSTR_USBH_MSC undeclared' build errors. R24 work first.
list(APPEND MICROPY_DEF_BOARD CFG_TUH_CDC=0 CFG_TUH_MSC=0 CFG_TUH_HID=0)

# Freeze the annealage_pod Python package into the firmware image.
set(MICROPY_FROZEN_MANIFEST ${MICROPY_BOARD_DIR}/manifest.py)
