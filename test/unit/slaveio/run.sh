#!/usr/bin/env bash
# run.sh
#
# Build and run host-side unit tests for the slaveio register-table
# state machine. No IDF or FreeRTOS dependency; the regtable module is
# pure C and exercises the byte-level protocol that backs both the
# I2C-slave and SPI-slave personalities.

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${THIS_DIR}/../../.." && pwd)"
SRC_DIR="${REPO_ROOT}/src/c_modules/slaveio"

OUT_DIR="${THIS_DIR}/build"
mkdir -p "${OUT_DIR}"

CC="${CC:-gcc}"
CFLAGS="${CFLAGS:--O2 -Wall -Wextra -Werror -std=c11}"

echo "==> compiling test_regtable"
"${CC}" ${CFLAGS} \
    -I "${SRC_DIR}" \
    "${THIS_DIR}/test_regtable.c" \
    "${SRC_DIR}/slaveio_regtable.c" \
    -o "${OUT_DIR}/test_regtable"

echo "==> running test_regtable"
"${OUT_DIR}/test_regtable"

echo "==> slaveio unit tests OK"
