# mpy-pod usbhost user C module.
#
# Phase 2 WS-A: stub-only translation unit. WS-B will replace
# usbhost_stub.c with a real TinyUSB host integration; until then
# every usbhost_* call returns -ENOSYS so the multiplexer surfaces a
# transparent error to the host instead of silently completing.

add_library(usermod_usbhost INTERFACE)

target_sources(usermod_usbhost INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/usbhost_stub.c
)

target_include_directories(usermod_usbhost INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

target_link_libraries(usermod INTERFACE usermod_usbhost)
