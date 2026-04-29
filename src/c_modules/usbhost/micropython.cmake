# mpy-pod usbhost user C module.
#
# Phase 2 WS-B: real USB host backend on top of the ESP-IDF `usb_host`
# component. See docs/design/usbhost.md §1 for the stack-selection
# rationale: TinyUSB host is not part of IDF v5.5.1's bundled
# components, so we use the underlying `usb_host` component directly,
# matching referencea/esp-usbip-bridge.
#
# usbhost_stub.c is retained on disk as the host-build fallback for
# the unit-test harness under test/unit/usbhost/. Only the firmware
# build links usbhost.c, which provides the real backend.

add_library(usermod_usbhost INTERFACE)

target_sources(usermod_usbhost INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/usbhost.c
)

target_include_directories(usermod_usbhost INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

target_link_libraries(usermod INTERFACE usermod_usbhost)
