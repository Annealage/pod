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

# Phase 3 P3.0 pivot to TinyUSB host via andrewleech/micropython#7 is
# IN PROGRESS. The submodule pin and src/c_modules/usbhost/usbhost.c
# rewrite both still need work before MICROPY_HW_USB_HOST=1 here can
# build clean. Three blockers identified:
#   (1) CFG_TUSB_OS_INC_PATH had a trailing space in tinyusb_port/
#       tusb_config.h (fixed in our submodule pin: machine-usbhost-mpypod
#       commit c6d91f044).
#   (2) shared/tinyusb/mp_usbh.h uses machine_usbh_*_obj_t typedefs in
#       function prototypes BEFORE the typedefs themselves are declared,
#       which fails to compile against the IDF toolchain (fixed in our
#       submodule pin: machine-usbhost-mpypod commit acb3c4645).
#   (3) When MICROPY_HW_USB_HOST=1, the user-C-module sources
#       (src/c_modules/usbhost/usbhost.c) get compiled twice: once for
#       __idf_main and once for the micropython.elf executable. The
#       second compile fails because traceISR_EXIT_TO_SCHEDULER is not
#       visible at the parse time of the static-inline osal_semaphore_post
#       in espressif__tinyusb's osal_freertos.h. Cause not yet pinpointed;
#       likely an IDF-side include-path interaction with the user_c_modules
#       gather pass. Tracked for follow-up.
# Until (3) is resolved, leave MICROPY_HW_USB_HOST disabled so the rest
# of the firmware builds.
#
# To re-enable when (3) is fixed, uncomment the two lines below:
# set(MICROPY_HW_USB_HOST 1)
# list(APPEND MICROPY_DEF_BOARD MICROPY_HW_USB_HOST=1)

# Freeze the annealage_pod Python package into the firmware image.
set(MICROPY_FROZEN_MANIFEST ${MICROPY_BOARD_DIR}/manifest.py)
