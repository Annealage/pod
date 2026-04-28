# mpy-pod uartbridge user C module.
#
# TCP <-> UART forwarder. Listens on a configurable TCP port (default
# 2000), forwards bytes byte-for-byte to/from a configurable UART
# (default UART2 per Appendix A pinmap). See docs/design/uartbridge.md.

add_library(usermod_uartbridge INTERFACE)

target_sources(usermod_uartbridge INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/moduartbridge.c
    ${CMAKE_CURRENT_LIST_DIR}/uart_bridge.c
    ${CMAKE_CURRENT_LIST_DIR}/uart_bridge_telnet.c
)

target_include_directories(usermod_uartbridge INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

# IDF components consumed: driver/uart, lwIP BSD sockets via the MP
# port's existing dependency on lwip, esp_log. Pulled in transitively
# through the MP esp32 port; no extra REQUIRES needed here.

target_link_libraries(usermod INTERFACE usermod_uartbridge)
