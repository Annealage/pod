# Annealage Pod: WS-B unit tests for the USB host backend's API
# contract.
#
# Host-side build. The real backend in usbhost.c depends on ESP-IDF,
# FreeRTOS and the IDF `usb_host` component, which are not available
# on the host. The tests therefore link the WS-A-installed stub
# (`usbhost_stub.c`) plus the host-portable headers, and validate the
# argument-validation paths and the documented stub semantics that
# the multiplexer relies on.
#
# What is covered:
#  - Stub returns 0 from usbhost_start() so boot is not blocked.
#  - Transfer functions return -ENOSYS rather than silently zeroing,
#    which the multiplexer's RET_SUBMIT path requires.
#  - usbhost_get_devices / _by_busid degrade safely (no devices).
#  - usbhost_is_interrupt_endpoint reports false in the absence of
#    a real device table.
#
# What is NOT covered here (deferred to integration / on-target tests):
#  - Live URB round-trip against a real DUT.
#  - Hot-plug attach / detach event handling.
#  - Composite-device interface claim ordering.
# These are gated on Phase 3 hardware enumeration.

cmake_minimum_required(VERSION 3.16)
project(usbhost_unit_tests C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED YES)

if(NOT CMAKE_C_FLAGS)
    set(CMAKE_C_FLAGS "-Wall -Wextra -Werror -O0 -g")
endif()

set(CMODULES_DIR  "${CMAKE_CURRENT_SOURCE_DIR}/../../../src/c_modules")
set(USBHOST_DIR   "${CMODULES_DIR}/usbhost")
set(USBIP_DIR     "${CMODULES_DIR}/usbip")

enable_testing()

add_executable(test_usbhost_stub
    test_usbhost_stub.c
    ${USBHOST_DIR}/usbhost_stub.c
)

target_include_directories(test_usbhost_stub PRIVATE
    ${USBHOST_DIR}
    ${USBIP_DIR}
    ${CMODULES_DIR}
)

target_compile_definitions(test_usbhost_stub PRIVATE
    MPY_POD_HOST_TEST_BUILD=1
)

add_test(NAME usbhost_stub_tests COMMAND test_usbhost_stub)
