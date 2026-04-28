#!/usr/bin/env bash
# build.sh
#
# Build the Annealage Pod firmware for the ESP32_S3_ANNEALAGE_POD board
# variant. Idempotent: re-invoking does an incremental MicroPython /
# IDF build under src/micropython/ports/esp32/build-ESP32_S3_ANNEALAGE_POD/.
#
# Output: src/micropython/ports/esp32/build-ESP32_S3_ANNEALAGE_POD/firmware.bin

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${THIS_DIR}/../.." && pwd)"

MP_ESP32_PORT="${REPO_ROOT}/src/micropython/ports/esp32"
BOARD_DIR="${REPO_ROOT}/src/boards/ESP32_S3_ANNEALAGE_POD"
USER_C_MODULES="${REPO_ROOT}/src/c_modules/micropython.cmake"

# shellcheck disable=SC1091
source "${THIS_DIR}/setup-idf.sh"

# Ensure mpy-cross is built (needed for frozen modules).
if [ ! -x "${REPO_ROOT}/src/micropython/mpy-cross/build/mpy-cross" ]; then
    echo "==> building mpy-cross"
    make -C "${REPO_ROOT}/src/micropython/mpy-cross"
fi

# Pull the esp32 port's required submodules. This is idempotent and
# only does network traffic the first time.
echo "==> updating ESP32 port submodules"
make -C "${MP_ESP32_PORT}" \
    BOARD_DIR="${BOARD_DIR}" \
    USER_C_MODULES="${USER_C_MODULES}" \
    submodules

# Build.
echo "==> building firmware"
make -C "${MP_ESP32_PORT}" \
    BOARD_DIR="${BOARD_DIR}" \
    USER_C_MODULES="${USER_C_MODULES}"

echo
echo "Build complete. Firmware: ${MP_ESP32_PORT}/build-ESP32_S3_ANNEALAGE_POD/firmware.bin"
