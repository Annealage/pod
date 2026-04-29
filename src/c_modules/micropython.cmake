# Top-level entry point for Annealage Pod user C modules.
#
# Each module declares its own INTERFACE library and links itself
# against the global `usermod` target. This file just lists them.

include(${CMAKE_CURRENT_LIST_DIR}/usbip/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/usbhost/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/dapprobe/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/uartbridge/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/slaveio/micropython.cmake)
# ops_ota C shim deferred for rev1: it depends on esp_https_ota and
# esp_http_client, which are not in MicroPython esp32 port's default
# IDF_COMPONENTS list. User-module cmakes run only during the build
# phase (gated by `if(NOT CMAKE_BUILD_EARLY_EXPANSION)` in
# esp32_common.cmake), so any IDF_COMPONENTS append from a user
# module arrives after idf_component_register has locked in REQUIRES
# from the discovery phase. The pure-MP fallback in
# src/mpy/annealage_pod/ops/ota.py (urequests + esp32.Partition) is what
# rev1 ships. Re-enable when an upstream MP hook lands or after a
# board-cmake-side patch.
# include(${CMAKE_CURRENT_LIST_DIR}/ops_ota/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/ops_wdt/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/ops_log/micropython.cmake)
