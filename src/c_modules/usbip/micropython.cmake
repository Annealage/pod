# Annealage Pod usbip user C module.
#
# TCP/3240 USB/IP server on lwIP-RAW callbacks (usbip_server_rp2.c),
# forwarding the DUT only (busid 1). usbip_proto.c is the wire codec and
# usbip_device.h the device-record types it encodes.

add_library(usermod_usbip INTERFACE)

target_sources(usermod_usbip INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modusbip_rp2.c
    ${CMAKE_CURRENT_LIST_DIR}/usbip_server_rp2.c
    ${CMAKE_CURRENT_LIST_DIR}/usbip_proto.c
)

target_include_directories(usermod_usbip INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

target_link_libraries(usermod INTERFACE usermod_usbip)
