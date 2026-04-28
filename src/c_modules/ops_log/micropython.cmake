# mpy-pod ops_log user C module.
#
# Listens on TCP/<port> and fans out ESP_LOGx + stdout to UART0
# (preserved) and the connected client. WS-H scope
# (plan/phase-2-parallel-implementation.md).
#
# IDF deps: log (esp_log_set_vprintf), lwip (BSD sockets),
# freertos. All three are already in the MicroPython esp32 port's
# IDF_COMPONENTS list, so no extra REQUIRES are needed here.

add_library(usermod_ops_log INTERFACE)

target_sources(usermod_ops_log INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modops_log.c
    ${CMAKE_CURRENT_LIST_DIR}/ops_log.c
)

target_include_directories(usermod_ops_log INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

target_link_libraries(usermod INTERFACE usermod_ops_log)
