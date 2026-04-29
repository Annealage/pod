# Annealage Pod: WS-C unit tests for the dapprobe protocol layer.
#
# Host-side CTest binary that links:
#   - the vendored CMSIS-DAP `DAP.c` and `DAP_vendor.c`
#   - the port glue (swd_glue.c, swo_glue.c)
#   - dap_core.c, synthetic_device.c, moddapprobe.c is excluded (MP runtime)
#   - virtual_device.c (host-portable, from the usbip module)
#   - support/io_stubs.c stubbing WS-D's swd.h / swo.h
#   - support/usbip_stubs.c forwarding to the host virtual_device registry
#
# No FreeRTOS, lwIP, or IDF dependencies.

cmake_minimum_required(VERSION 3.16)
project(dapprobe_unit_tests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED YES)

if(NOT CMAKE_C_FLAGS)
    set(CMAKE_C_FLAGS "-Wall -Wextra -Wno-unused-function -Wno-unused-parameter -Wno-unused-variable -Wno-unused-const-variable -O0 -g")
endif()

set(REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../..")
set(CMODULES_DIR  "${REPO_ROOT}/src/c_modules")
set(DAPPROBE_DIR  "${CMODULES_DIR}/dapprobe")
set(USBIP_DIR     "${CMODULES_DIR}/usbip")
set(SUPPORT_DIR   "${CMAKE_CURRENT_SOURCE_DIR}/support")

enable_testing()

add_executable(test_dapprobe
    test_dapprobe.c
    # Module sources under test
    ${DAPPROBE_DIR}/dap_core.c
    ${DAPPROBE_DIR}/synthetic_device.c
    ${DAPPROBE_DIR}/port/swd_glue.c
    ${DAPPROBE_DIR}/port/swo_glue.c
    ${DAPPROBE_DIR}/vendor/cmsis-dap/Source/DAP.c
    ${DAPPROBE_DIR}/vendor/cmsis-dap/Source/DAP_vendor.c
    # Host-portable usbip pieces (only the registry is exercised here)
    ${USBIP_DIR}/virtual_device.c
    # Support / stubs
    ${SUPPORT_DIR}/io_stubs.c
    ${SUPPORT_DIR}/usbip_stubs.c
)

target_include_directories(test_dapprobe PRIVATE
    ${DAPPROBE_DIR}
    ${DAPPROBE_DIR}/port
    ${DAPPROBE_DIR}/vendor/cmsis-dap/Include
    ${DAPPROBE_DIR}/io
    ${USBIP_DIR}
    ${CMODULES_DIR}
    ${SUPPORT_DIR}
)

# The host build is not under IDF, so silence ESP_PLATFORM and provide
# our own esp_err.h shim from support/.
target_compile_definitions(test_dapprobe PRIVATE
    MPY_POD_HOST_TEST_BUILD=1
    # Take the C-only branch of DAP.h's PIN_DELAY_SLOW; the alternative
    # branch is hand-rolled ARM Thumb assembly.
    __CC_ARM=1
)

add_test(NAME dapprobe_tests COMMAND test_dapprobe)
