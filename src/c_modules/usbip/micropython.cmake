# mpy-pod usbip user C module.
#
# Phase 2 (WS-A): TCP/3240 listener, USB/IP protocol multiplexer with
# real-USB (via usbhost stub) and synthetic (virtual_device_t) backends.

add_library(usermod_usbip INTERFACE)

target_sources(usermod_usbip INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modusbip.c
    ${CMAKE_CURRENT_LIST_DIR}/usbip_server.c
    ${CMAKE_CURRENT_LIST_DIR}/usbip_proto.c
    ${CMAKE_CURRENT_LIST_DIR}/virtual_device.c
)

target_include_directories(usermod_usbip INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

# IDF lwIP, FreeRTOS, esp_event and esp_log headers are made
# available to user C modules by the MicroPython esp32 port build,
# so an explicit target_link_libraries against idf::lwip etc. is
# unnecessary and triggers the IDF component-resolver to walk the
# full optional-component graph (including paths that may not exist
# in this IDF tree). Keep the link declaration on `usermod` only.

target_link_libraries(usermod INTERFACE usermod_usbip)
