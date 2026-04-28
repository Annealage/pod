# mpy-pod uartbridge user C module skeleton.
#
# Phase 1: stub only. Phase 2 wires UART2 to a TCP listener.

add_library(usermod_uartbridge INTERFACE)

target_sources(usermod_uartbridge INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/moduartbridge.c
    ${CMAKE_CURRENT_LIST_DIR}/uart_bridge.c
)

target_include_directories(usermod_uartbridge INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

target_link_libraries(usermod INTERFACE usermod_uartbridge)
