# mpy-pod usbhost user C module: TinyUSB raw-URB host backend.
#
# Two implementations, selected by target:
#   - ESP32-S3 : usbhost.c (FreeRTOS pump/watchdog tasks, IDF heap/log, the full
#     async + cancel + recovery machinery).
#   - RP2350   : usbhost_rp2.c (cooperative single-thread on the mp_usbh tuh_task
#     pump; depth=1; no FreeRTOS).
# Both sit behind usbhost.h.
#
# CFG_TUH_CDC/MSC/HID=0 and CFG_TUH_API_EDPT_XFER=1 are defined per-board via
# MICROPY_DEF_BOARD (ESP32_S3_ANNEALAGE_POD / ANNEALAGE_POD_RP2350 cmake) so they
# reach every USB compilation unit (machine_usb_host.c, mp_usbh.c, this module,
# tinyusb).

add_library(usermod_usbhost INTERFACE)

if(PICO_SDK_PATH)
    target_sources(usermod_usbhost INTERFACE
        ${CMAKE_CURRENT_LIST_DIR}/usbhost_rp2.c
        ${CMAKE_CURRENT_LIST_DIR}/modusbhost_rp2.c
    )
else()
    target_sources(usermod_usbhost INTERFACE
        ${CMAKE_CURRENT_LIST_DIR}/usbhost.c
        ${CMAKE_CURRENT_LIST_DIR}/modusbhost.c
    )
endif()

target_include_directories(usermod_usbhost INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

target_link_libraries(usermod INTERFACE usermod_usbhost)
