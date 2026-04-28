#!/usr/bin/env bash
# monitor.sh
#
# Attach idf.py monitor to the connected esp32-s3 board (or to a label
# supplied on the command line).
#
# Usage:
#   src/tools/monitor.sh                # auto-detect single esp32*
#   src/tools/monitor.sh esp32-s3       # explicit mpy-dev label

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${THIS_DIR}/../.." && pwd)"

MP_ESP32_PORT="${REPO_ROOT}/src/micropython/ports/esp32"
BOARD_DIR="${REPO_ROOT}/src/boards/ESP32_S3_ANNEALAGE_POD"
USER_C_MODULES="${REPO_ROOT}/src/c_modules/micropython.cmake"
BUILD_DIR="${MP_ESP32_PORT}/build-ESP32_S3_ANNEALAGE_POD"

LABEL="${1:-}"
if [ -z "${LABEL}" ]; then
    if ! command -v mpy-dev >/dev/null 2>&1; then
        echo "monitor.sh: mpy-dev tool not on PATH; cannot auto-detect target" >&2
        exit 1
    fi
    mapfile -t ESP_LABELS < <(mpy-dev list 2>/dev/null \
        | awk '/\[connected\]/ && $1 ~ /^esp32/ {print $1}')
    if [ "${#ESP_LABELS[@]}" -ne 1 ]; then
        echo "monitor.sh: expected exactly one connected esp32* device, found ${#ESP_LABELS[@]}" >&2
        exit 1
    fi
    LABEL="${ESP_LABELS[0]}"
fi

PORT="$(mpy-dev tty "${LABEL}")"

# shellcheck disable=SC1091
source "${THIS_DIR}/setup-idf.sh"

idf.py -C "${MP_ESP32_PORT}" \
    -B "${BUILD_DIR}" \
    -D MICROPY_BOARD=ESP32_S3_ANNEALAGE_POD \
    -D MICROPY_BOARD_DIR="${BOARD_DIR}" \
    -D USER_C_MODULES="${USER_C_MODULES}" \
    -p "${PORT}" \
    monitor
