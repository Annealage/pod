# Top-level CMake for spi2-swd-spike
# Validates SPI2 + GDMA half-duplex 3-wire as the SWD I/O backend.
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(spi2_swd_spike)
