# Component wrapper that pulls swd.c / swo.c from src/c_modules/dapprobe/io/.
# Lets the test app build and link the io/ engine without duplicating sources.

set(IO_DIR "${CMAKE_CURRENT_LIST_DIR}/../../../../../src/c_modules/dapprobe/io")

idf_component_register(
    SRCS
        "${IO_DIR}/swd.c"
        "${IO_DIR}/swo.c"
    INCLUDE_DIRS
        "${IO_DIR}"
    REQUIRES
        esp_driver_spi
        esp_driver_gpio
        esp_driver_uart
        hal
        soc
        esp_rom
        esp_hw_support
        driver
)
