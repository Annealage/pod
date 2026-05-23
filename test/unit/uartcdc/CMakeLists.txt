# Annealage Pod: unit tests for the uartcdc synthetic CDC device.
#
# Host-side CTest binary linking:
#   - uart_cdc_device.c (module under test)
#   - virtual_device.c  (host-portable usbip registry)
#   - support/uart_stubs.c    (IDF UART driver stubs)
#   - support/usbip_stubs.c   (usbip_server_register stub)
#
# The support/driver/uart.h shim intercepts #include "driver/uart.h".
# The support/esp_err.h shim intercepts #include "esp_err.h".
# No FreeRTOS, lwIP, or IDF dependencies.

cmake_minimum_required(VERSION 3.16)
project(uartcdc_unit_tests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED YES)

if(NOT CMAKE_C_FLAGS)
    set(CMAKE_C_FLAGS "-Wall -Wextra -Wno-unused-function -Wno-unused-parameter -Wno-unused-variable -O0 -g")
endif()

set(REPO_ROOT    "${CMAKE_CURRENT_SOURCE_DIR}/../../..")
set(CMODULES_DIR "${REPO_ROOT}/src/c_modules")
set(UARTCDC_DIR  "${CMODULES_DIR}/uartcdc")
set(USBIP_DIR    "${CMODULES_DIR}/usbip")
set(SUPPORT_DIR  "${CMAKE_CURRENT_SOURCE_DIR}/support")

enable_testing()

add_executable(test_uartcdc
    test_uart_cdc.c
    ${UARTCDC_DIR}/uart_cdc_device.c
    ${USBIP_DIR}/virtual_device.c
    ${SUPPORT_DIR}/uart_stubs.c
    ${SUPPORT_DIR}/usbip_stubs.c
)

target_include_directories(test_uartcdc PRIVATE
    # support/ first so driver/uart.h and esp_err.h shims take precedence
    ${SUPPORT_DIR}
    ${UARTCDC_DIR}
    ${USBIP_DIR}
    ${CMODULES_DIR}
)

target_compile_definitions(test_uartcdc PRIVATE
    MPY_POD_HOST_TEST_BUILD=1
)

add_test(NAME uartcdc_tests COMMAND test_uartcdc)
