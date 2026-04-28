#!/usr/bin/env bash
# Annealage Pod: WS-A unit tests for the USB/IP protocol parser
# and virtual-device registry.
#
# Idempotent: configures + builds + runs ctest under build/ in this
# directory. No host dependencies beyond cmake, make, and a C
# compiler.

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${THIS_DIR}/build"

mkdir -p "${BUILD_DIR}"
cmake -S "${THIS_DIR}" -B "${BUILD_DIR}" -Wno-dev >/dev/null
cmake --build "${BUILD_DIR}" --parallel
ctest --test-dir "${BUILD_DIR}" --output-on-failure
