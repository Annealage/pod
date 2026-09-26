# Annealage Pod usbhost user C module: TinyUSB raw-URB host backend.
#
# usbhost_rp2.c runs cooperatively on the mp_usbh tuh_task pump (depth 1,
# no RTOS) behind usbhost.h.
#
# CFG_TUH_CDC/MSC/HID=0 and CFG_TUH_API_EDPT_XFER=1 are defined per-board via
# MICROPY_DEF_BOARD in the board cmake so they reach every USB compilation
# unit (machine_usb_host.c, mp_usbh.c, this module, tinyusb).

add_library(usermod_usbhost INTERFACE)

target_sources(usermod_usbhost INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/usbhost_rp2.c
    ${CMAKE_CURRENT_LIST_DIR}/modusbhost_rp2.c
)

target_include_directories(usermod_usbhost INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

target_link_libraries(usermod INTERFACE usermod_usbhost)
