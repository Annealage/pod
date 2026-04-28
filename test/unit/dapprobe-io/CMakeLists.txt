# Unit test app for the dapprobe io/ engine (WS-D).
#
# This is a standalone idf.py project that links the SWD and SWO
# sources from src/c_modules/dapprobe/io/ and runs a loopback self-test.
# It is intended to be built and flashed independently of the main
# MicroPython firmware to validate the io/ engine in isolation.
#
# Build:
#   cd test/unit/dapprobe-io
#   idf.py set-target esp32s3
#   idf.py build
#
# Flash + run (requires the SWDIO pin shorted to itself; in 3-wire
# half-duplex loopback a single pin is sufficient since MOSI and MISO
# share the same wire by definition):
#   idf.py -p $(mpy-dev tty esp32-s3) flash monitor

cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# Add the dapprobe/io/ directory as a component search path so we can
# pick up swd.{c,h} and swo.{c,h} without copying.
list(APPEND EXTRA_COMPONENT_DIRS
    ${CMAKE_CURRENT_LIST_DIR}/components
)

project(dapprobe_io_test)
