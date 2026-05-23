#!/usr/bin/env bash
# Run all uartcdc integration tests.
# Override defaults via env: ANNEALAGE_POD_HOST (mDNS name), USBIPD_IP (fallback), ANNEALAGE_POD_TTY
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "=== test_uartcdc_repl ==="
python3 test_uartcdc_repl.py
echo
echo "All uartcdc integration tests passed."
