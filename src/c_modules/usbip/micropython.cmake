# mpy-pod usbip user C module.
#
# TCP/3240 USB/IP server. Two transports, selected by target:
#   - ESP32-S3 : FreeRTOS tasks + lwIP BSD sockets (usbip_server.c), with the
#     synthetic CMSIS-DAP virtual-device multiplexer.
#   - RP2350   : lwIP-RAW callback state machine (usbip_server_rp2.c), forwarding
#     the DUT only (busid 1); no synthetic device.
# The protocol codec (usbip_proto.c) and the device-record/registry types
# (virtual_device.c) are platform-neutral and shared by both.

add_library(usermod_usbip INTERFACE)

if(PICO_SDK_PATH)
    # RP2350 pod (lwIP-RAW transport, forwarder-only).
    target_sources(usermod_usbip INTERFACE
        ${CMAKE_CURRENT_LIST_DIR}/modusbip_rp2.c
        ${CMAKE_CURRENT_LIST_DIR}/usbip_server_rp2.c
        ${CMAKE_CURRENT_LIST_DIR}/usbip_proto.c
        ${CMAKE_CURRENT_LIST_DIR}/virtual_device.c
    )
else()
    # ESP32-S3 (FreeRTOS + BSD sockets transport).
    target_sources(usermod_usbip INTERFACE
        ${CMAKE_CURRENT_LIST_DIR}/modusbip.c
        ${CMAKE_CURRENT_LIST_DIR}/usbip_server.c
        ${CMAKE_CURRENT_LIST_DIR}/usbip_proto.c
        ${CMAKE_CURRENT_LIST_DIR}/virtual_device.c
    )
endif()

target_include_directories(usermod_usbip INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

target_link_libraries(usermod INTERFACE usermod_usbip)
