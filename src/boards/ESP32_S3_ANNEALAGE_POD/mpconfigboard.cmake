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

# Freeze the annealage_pod Python package into the firmware image.
set(MICROPY_FROZEN_MANIFEST ${MICROPY_BOARD_DIR}/manifest.py)
