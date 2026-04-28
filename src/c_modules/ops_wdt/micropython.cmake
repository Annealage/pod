# mpy-pod ops_wdt user C module.
#
# Wraps esp_task_wdt for MP main task subscription. WS-H scope
# (plan/phase-2-parallel-implementation.md). esp_task_wdt itself
# lives in the IDF esp_system component, which is already in the
# MicroPython esp32 port's IDF_COMPONENTS list, so no extra REQUIRES
# are needed here.

add_library(usermod_ops_wdt INTERFACE)

target_sources(usermod_ops_wdt INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modops_wdt.c
    ${CMAKE_CURRENT_LIST_DIR}/ops_wdt.c
)

target_include_directories(usermod_ops_wdt INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

target_link_libraries(usermod INTERFACE usermod_ops_wdt)
