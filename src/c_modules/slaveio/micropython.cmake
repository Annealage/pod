# mpy-pod slaveio user C module skeleton.
#
# Phase 1: stub only. Phase 2 wires the I2C-slave / SPI-slave hardware
# personalities (mutually exclusive on shared translator pins).

add_library(usermod_slaveio INTERFACE)

target_sources(usermod_slaveio INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modslaveio.c
    ${CMAKE_CURRENT_LIST_DIR}/slaveio.c
)

target_include_directories(usermod_slaveio INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

target_link_libraries(usermod INTERFACE usermod_slaveio)
