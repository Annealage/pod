# Annealage Pod: uartcdc user C module.
#
# Synthetic USB CDC ACM serial device exposed via the USB/IP multiplexer.
# Selected when ANNEALAGE_POD_UART_BACKEND=usbip (the default). The uart_bridge
# TCP socket module is selected when ANNEALAGE_POD_UART_BACKEND=tcp.

add_library(usermod_uartcdc INTERFACE)

target_sources(usermod_uartcdc INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/moduartcdc.c
    ${CMAKE_CURRENT_LIST_DIR}/uart_cdc_device.c
)

target_include_directories(usermod_uartcdc INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

target_link_libraries(usermod INTERFACE usermod_uartcdc)
