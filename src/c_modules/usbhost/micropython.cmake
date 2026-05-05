# mpy-pod usbhost user C module (R27: TinyUSB host backend).
#
# Builds usbhost.c against TinyUSB host primitives (tuh_edpt_xfer,
# tuh_control_xfer, etc.) via the mp_usbh.h init wrapper from
# andrewleech/micropython#7.
#
# Lineage: this cmake configuration was originally landed on the
# parked `r24-wip` branch and is brought to this branch
# (r27-tinyusb-migration) alongside the new usbhost.c. See
# test/integration/phase3/r27-tinyusb-resume-plan.md for context.
#
# Hot-plug integration: mp_usbh.c defines __attribute__((weak))
# tuh_mount_hook / tuh_umount_hook no-ops and calls them from its
# tuh_mount_cb / tuh_umount_cb implementations. usbhost.c provides
# strong-linkage overrides of those hooks to run USBIP slot bookkeeping
# alongside the machine.USBHost Python API bookkeeping in mp_usbh.c.
#
# CFG_TUH_CDC/MSC/HID=0 and CFG_TUH_API_EDPT_XFER=1: defined in
# src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.cmake via MICROPY_DEF_BOARD
# so they reach all compilation units (machine_usb_host.c, mp_usbh.c,
# usbhost.c).

add_library(usermod_usbhost INTERFACE)

target_sources(usermod_usbhost INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/usbhost.c
)

target_include_directories(usermod_usbhost INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

# NOTE: CFG_TUH_CDC=0, CFG_TUH_MSC=0, CFG_TUH_HID=0 and
# CFG_TUH_API_EDPT_XFER=1 are defined in
# src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.cmake via MICROPY_DEF_BOARD.
# That list is passed to target_compile_definitions(${MICROPY_TARGET} PUBLIC)
# in esp32_common.cmake and reaches all compilation units including the
# __idf_main component (machine_usb_host.c, mp_usbh.c) and usbhost.c itself.

target_link_libraries(usermod INTERFACE usermod_usbhost)
