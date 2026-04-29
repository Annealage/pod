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

# esp_https_ota and esp_http_client are not in MicroPython's default
# IDF_COMPONENTS list (esp32_common.cmake lines 163-198). app_update is.
#
# Append to the main component's REQUIRES via the IDF build-system
# property that idf_component_register actually consults. The plain
# list(APPEND IDF_COMPONENTS ...) pattern does not propagate scope
# from the user module's micropython.cmake to esp32_common.cmake's
# idf_component_register call (verified empirically against
# project_description.json). Linking against idf::* aliases works
# but pulls IDF target INTERFACE_INCLUDE_DIRECTORIES into usermod's
# include set via usermod_gather_sources transitive recursion, which
# trips IDF's own include-dir validation.
#
# idf_component_optional_requires is the documented IDF mechanism
# for adding REQUIRES from outside idf_component_register; it is
# safe to call before the register because IDF defers the
# resolution.
idf_component_optional_requires(PUBLIC esp_https_ota esp_http_client)

target_link_libraries(usermod INTERFACE usermod_ops_ota)
