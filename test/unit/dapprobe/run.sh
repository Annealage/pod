#!/usr/bin/env bash
# Annealage Pod: WS-C unit tests for the dapprobe protocol layer.
#
# Idempotent: configures, builds, and runs ctest under build/.

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${THIS_DIR}/build"

mkdir -p "${BUILD_DIR}"
cmake -S "${THIS_DIR}" -B "${BUILD_DIR}" -Wno-dev >/dev/null
cmake --build "${BUILD_DIR}" --parallel
ctest --test-dir "${BUILD_DIR}" --output-on-failure
