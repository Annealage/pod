# mpy-pod usbip user C module skeleton.
#
# Phase 1: stubs only; just enough surface area for `import usbip;
# usbip.start()` to log a line. Phase 2 brings up the real TCP/3240
# USB/IP server.

add_library(usermod_usbip INTERFACE)

target_sources(usermod_usbip INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modusbip.c
    ${CMAKE_CURRENT_LIST_DIR}/usbip_server.c
)

target_include_directories(usermod_usbip INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

target_link_libraries(usermod INTERFACE usermod_usbip)
