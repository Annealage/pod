#!/usr/bin/env bash
# run.sh
#
# Build and run the host-side protocol-level unit tests for the
# uartbridge module. No IDF or FreeRTOS dependency: the decoder lives
# in uart_bridge_telnet.c which is platform-independent.

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${THIS_DIR}/../../.." && pwd)"
SRC_DIR="${REPO_ROOT}/src/c_modules/uartbridge"

OUT_DIR="${THIS_DIR}/build"
mkdir -p "${OUT_DIR}"

CC="${CC:-gcc}"
CFLAGS="${CFLAGS:--O2 -Wall -Wextra -Werror -std=c11}"

echo "==> compiling test_telnet_decoder"
"${CC}" ${CFLAGS} \
    -I "${SRC_DIR}" \
    "${THIS_DIR}/test_telnet_decoder.c" \
    "${SRC_DIR}/uart_bridge_telnet.c" \
    -o "${OUT_DIR}/test_telnet_decoder"

echo "==> running test_telnet_decoder"
"${OUT_DIR}/test_telnet_decoder"

echo "==> uartbridge unit tests OK"
