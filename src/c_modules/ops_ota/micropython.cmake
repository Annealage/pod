# mpy-pod ops_ota user C module.
#
# Wraps esp_https_ota for synchronous OTA updates from MicroPython.
# WS-H scope (plan/phase-2-parallel-implementation.md). The MP-side
# wrapper in src/mpy/annealage_pod/ops/ota.py imports this module and
# falls back to esp32.Partition on the Unix port (where this module
# does not exist).

add_library(usermod_ops_ota INTERFACE)

target_sources(usermod_ops_ota INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modops_ota.c
    ${CMAKE_CURRENT_LIST_DIR}/ops_ota.c
)

target_include_directories(usermod_ops_ota INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

# esp_https_ota and esp_http_client are not in the MicroPython esp32
# port's default IDF_COMPONENTS list (esp32_common.cmake). Append
# them here so the IDF main component pulls in their public headers
# and link archives. The append happens before
# idf_component_register() runs (usermod.cmake is included earlier
# in esp32_common.cmake).
list(APPEND IDF_COMPONENTS esp_https_ota esp_http_client app_update)

target_link_libraries(usermod INTERFACE usermod_ops_ota)
