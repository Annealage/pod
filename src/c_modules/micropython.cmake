# Top-level entry point for Annealage Pod user C modules.
#
# Each module declares its own INTERFACE library and links itself
# against the global `usermod` target. This file just lists them.

include(${CMAKE_CURRENT_LIST_DIR}/usbip/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/usbhost/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/dapprobe/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/uartbridge/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/slaveio/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/ops_ota/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/ops_wdt/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/ops_log/micropython.cmake)
