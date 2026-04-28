# Annealage Pod: WS-A unit tests for the USB/IP protocol parser
# and the virtual-device registry.
#
# Host-side build: link the protocol-layer translation units
# (usbip_proto.c, virtual_device.c) into a CTest binary that
# exercises wire-format encode/decode and registry behaviour
# without needing FreeRTOS, lwIP or the ESP target.

cmake_minimum_required(VERSION 3.16)
project(usbip_unit_tests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED YES)

if(NOT CMAKE_C_FLAGS)
    set(CMAKE_C_FLAGS "-Wall -Wextra -Werror -O0 -g")
endif()

# Locate the c_modules directory relative to this test directory.
set(CMODULES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../../src/c_modules")
set(USBIP_DIR    "${CMODULES_DIR}/usbip")

enable_testing()

# Tests build: protocol parser, virtual-device registry, plus the
# test runner itself. The TCP-server translation unit (usbip_server.c)
# is excluded from this build because it pulls in FreeRTOS and lwIP.
add_executable(test_usbip_proto
    test_usbip_proto.c
    ${USBIP_DIR}/usbip_proto.c
    ${USBIP_DIR}/virtual_device.c
)

target_include_directories(test_usbip_proto PRIVATE
    ${USBIP_DIR}
    ${CMODULES_DIR}
)

target_compile_definitions(test_usbip_proto PRIVATE
    MPY_POD_HOST_TEST_BUILD=1
)

add_test(NAME usbip_proto_tests COMMAND test_usbip_proto)
