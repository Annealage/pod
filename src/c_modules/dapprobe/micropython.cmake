# mpy-pod dapprobe user C module skeleton.
#
# Phase 1: stub only. Phase 2 brings up CMSIS-DAP-v2 over the SPI2 +
# GDMA SWD backend and the UART1 + UHCI SWO pipeline.

add_library(usermod_dapprobe INTERFACE)

target_sources(usermod_dapprobe INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/moddapprobe.c
    ${CMAKE_CURRENT_LIST_DIR}/dap_core.c
)

target_include_directories(usermod_dapprobe INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

target_link_libraries(usermod INTERFACE usermod_dapprobe)
