# mpy-pod dapprobe user C module (Phase 2 WS-C).
#
# Vendored ARM-software CMSIS-DAP protocol layer (Apache-2.0) wired to:
#   - WS-D's swd.h SWD I/O engine (sources pulled in via io/io.cmake)
#   - WS-D's swo.h SWO trace pipeline (same)
#   - WS-A's USB/IP virtual-device registry (synthetic_device.c)
#
# The include search path puts dapprobe/port/ ahead of the vendored
# Config/ directory so DAP.c picks up our local DAP_config.h. SW_DP.c
# and SWO.c from the vendor tree are NOT compiled; their replacements
# are in port/swd_glue.c and port/swo_glue.c respectively.

add_library(usermod_dapprobe INTERFACE)

target_sources(usermod_dapprobe INTERFACE
    # Module entry / state
    ${CMAKE_CURRENT_LIST_DIR}/moddapprobe.c
    ${CMAKE_CURRENT_LIST_DIR}/dap_core.c
    ${CMAKE_CURRENT_LIST_DIR}/synthetic_device.c

    # Port glue (replaces vendor SW_DP.c and SWO.c)
    ${CMAKE_CURRENT_LIST_DIR}/port/swd_glue.c
    ${CMAKE_CURRENT_LIST_DIR}/port/swo_glue.c

    # Vendored CMSIS-DAP protocol layer (Apache-2.0)
    ${CMAKE_CURRENT_LIST_DIR}/vendor/cmsis-dap/Source/DAP.c
    ${CMAKE_CURRENT_LIST_DIR}/vendor/cmsis-dap/Source/DAP_vendor.c
)

target_include_directories(usermod_dapprobe INTERFACE
    # Order matters: port/ first so its DAP_config.h wins over the
    # vendored Config/DAP_config.h.
    ${CMAKE_CURRENT_LIST_DIR}/port
    ${CMAKE_CURRENT_LIST_DIR}/vendor/cmsis-dap/Include
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

# CRITICAL: pull in the WS-D SWD/SWO engine sources. Without this the
# swd_*/swo_* symbols referenced from port/swd_glue.c, port/swo_glue.c
# and dap_core.c go unresolved at link time. (The previous WS-C run
# missed this and the build broke.)
include(${CMAKE_CURRENT_LIST_DIR}/io/io.cmake)

target_link_libraries(usermod INTERFACE usermod_dapprobe)
