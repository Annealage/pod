# mpy-pod dapprobe SWD/SWO engine (WS-D) cmake fragment.
#
# This file is consumed from src/c_modules/dapprobe/micropython.cmake
# (owned by WS-C). It adds the io/ sources to the dapprobe usermod
# library and exposes the io/ include directory.
#
# Usage from dapprobe/micropython.cmake:
#
#   include(${CMAKE_CURRENT_LIST_DIR}/io/io.cmake)
#
# The fragment assumes `usermod_dapprobe` is already an INTERFACE
# library defined in dapprobe/micropython.cmake.

target_sources(usermod_dapprobe INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/swd.c
    ${CMAKE_CURRENT_LIST_DIR}/swo.c
)

target_include_directories(usermod_dapprobe INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

# IDF components required by the SWD/SWO engine.
# These get propagated into the MicroPython firmware build via the
# usermod target. Listed here as documentation; the actual link
# requirements are picked up from the includes by the IDF build system.
#
# Required IDF components:
#   - esp_driver_spi  (spi_master.h, hal/spi_ll.h, soc/spi_periph.h)
#   - esp_driver_gpio (driver/dedic_gpio.h, driver/gpio.h)
#   - esp_driver_uart (driver/uart.h, driver/uhci.h)
#   - hal             (hal/spi_ll.h, hal/gpio_hal.h)
#   - soc             (soc/spi_pins.h, soc/gpio_sig_map.h, soc/io_mux_reg.h)
#   - esp_rom         (esp_rom_gpio.h)
