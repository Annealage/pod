# mpy-pod slaveio user C module.
#
# Hardware-driven I2C-slave / SPI-slave register-table responder. See
# docs/design/slaveio.md for the design notes (peripheral choice,
# ISR contract, notify-callback dispatch path, mutual exclusion).
#
# Files:
#   modslaveio.c        : MicroPython binding, callback registry,
#                         dispatch wiring.
#   slaveio.c           : top-level personality control, IDF i2c-slave
#                         V2 + SPI-slave driver glue, FreeRTOS pump
#                         tasks.
#   slaveio_regtable.c  : pure-C register-table state machine. No IDF
#                         or FreeRTOS dependency; exercised by the
#                         host-side unit tests under test/unit/slaveio/.

add_library(usermod_slaveio INTERFACE)

target_sources(usermod_slaveio INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modslaveio.c
    ${CMAKE_CURRENT_LIST_DIR}/slaveio.c
    ${CMAKE_CURRENT_LIST_DIR}/slaveio_regtable.c
)

target_include_directories(usermod_slaveio INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

# IDF components consumed: esp_driver_i2c (i2c-slave V2), esp_driver_spi
# (spi-slave), esp_log, freertos. Pulled transitively through the MP
# esp32 port's `driver` aggregate component.

target_link_libraries(usermod INTERFACE usermod_slaveio)
