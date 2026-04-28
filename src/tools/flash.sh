#!/usr/bin/env bash
# flash.sh
#
# Flash the Annealage Pod firmware to the connected esp32-s3 board
# via the CH340N USB-UART bridge. Refuses to operate if more than one
# esp32* board is registered with mpy-dev unless a specific label is
# given as the first argument.
#
# Usage:
#   src/tools/flash.sh                # auto-detect, requires single esp32*
#   src/tools/flash.sh esp32-s3       # explicit mpy-dev label
#
# Smoke-flash run log:
#   2026-04-29: first successful flash on esp32-s3 (CH340N serial
#   5A46090178). MP 1.29.0-preview boots on UART0 at 115200 8N1, octal
#   PSRAM detected, gc.mem_free() ~8.3 MiB. Frozen `annealage_pod.boot.up()`
#   printed "annealage_pod skeleton up" on the console; all four C user
#   modules imported and emitted their ESP_LOGI start lines.

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${THIS_DIR}/../.." && pwd)"

MP_ESP32_PORT="${REPO_ROOT}/src/micropython/ports/esp32"
BOARD_DIR="${REPO_ROOT}/src/boards/ESP32_S3_ANNEALAGE_POD"
USER_C_MODULES="${REPO_ROOT}/src/c_modules/micropython.cmake"
BUILD_DIR="${MP_ESP32_PORT}/build-ESP32_S3_ANNEALAGE_POD"

# Resolve target label.
LABEL="${1:-}"
if [ -z "${LABEL}" ]; then
    if ! command -v mpy-dev >/dev/null 2>&1; then
        echo "flash.sh: mpy-dev tool not on PATH; cannot auto-detect target" >&2
        exit 1
    fi
    # Pick the only connected esp32* label, refuse if there are 0 or >1.
    mapfile -t ESP_LABELS < <(mpy-dev list 2>/dev/null \
        | awk '/\[connected\]/ && $1 ~ /^esp32/ {print $1}')
    if [ "${#ESP_LABELS[@]}" -eq 0 ]; then
        echo "flash.sh: no connected esp32* device found via mpy-dev" >&2
        exit 1
    elif [ "${#ESP_LABELS[@]}" -gt 1 ]; then
        echo "flash.sh: multiple connected esp32* devices found:" >&2
        printf '  %s\n' "${ESP_LABELS[@]}" >&2
        echo "Pass an explicit label: src/tools/flash.sh <label>" >&2
        exit 1
    fi
    LABEL="${ESP_LABELS[0]}"
fi

PORT="$(mpy-dev tty "${LABEL}")"
if [ ! -e "${PORT}" ]; then
    echo "flash.sh: mpy-dev returned ${PORT} but the device node is missing" >&2
    exit 1
fi

echo "==> flashing label=${LABEL} port=${PORT}"

if [ ! -f "${BUILD_DIR}/firmware.bin" ]; then
    echo "flash.sh: ${BUILD_DIR}/firmware.bin not found; running build.sh first"
    "${THIS_DIR}/build.sh"
fi

# shellcheck disable=SC1091
source "${THIS_DIR}/setup-idf.sh"

idf.py -C "${MP_ESP32_PORT}" \
    -B "${BUILD_DIR}" \
    -D MICROPY_BOARD=ESP32_S3_ANNEALAGE_POD \
    -D MICROPY_BOARD_DIR="${BOARD_DIR}" \
    -D USER_C_MODULES="${USER_C_MODULES}" \
    -p "${PORT}" \
    flash

echo
echo "Flash complete. Use src/tools/monitor.sh to attach the serial monitor."
