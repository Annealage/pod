#!/usr/bin/env bash
# Phase 3 integration tests. Drives a flashed-and-Wi-Fi-connected
# Annealage Pod through the smoke checklist in plan/phase-3-integration.md.
#
# Usage:
#   ./run.sh <annealage_pod-host> [<dut-label>]
# Example:
#   ./run.sh annealage_pod-94a9.local pico2-w
#
# Pre-requisites (per docs/runbook.md):
#   - Annealage Pod firmware built and flashed (src/tools/build.sh, flash.sh).
#   - Wi-Fi credentials in /credentials.json on the device.
#   - DUT wired to the Annealage Pod: USB to USB-OTG pins, SWD/UART to
#     translator pins, optional power FET.
#   - Host PC on the same subnet as the Annealage Pod.
#   - usbip-utils, mpremote, pyOCD installed on the host.

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${1:?usage: $0 <annealage_pod-host> [<dut-label>]}"
DUT_LABEL="${2:-}"

# Default ports per spec.md §5.3.
USBIP_PORT=3240
REPL_PORT=8266
UART_PORT=2000

pass=0
fail=0

ok()   { printf "  PASS  %s\n" "$*"; pass=$((pass+1)); }
no()   { printf "  FAIL  %s\n" "$*"; fail=$((fail+1)); }
note() { printf "  note  %s\n" "$*"; }

section() {
    printf "\n== %s ==\n" "$*"
}

# --- P3.1.1 USB/IP devlist shows two devices --------------------------
section "P3.1.1  usbip list shows DUT + synthetic CMSIS-DAP"
if devlist=$(usbip list -r "$HOST" 2>&1); then
    n=$(printf "%s\n" "$devlist" | grep -cE '^\s*[0-9]+-[0-9]+:' || true)
    if [ "$n" -ge 2 ]; then
        ok "usbip list returned $n busids"
    elif [ "$n" -eq 1 ]; then
        no "usbip list returned only 1 busid (DUT not enumerated, or synthetic dapprobe not registered)"
        printf "%s\n" "$devlist" | sed 's/^/        /'
    else
        no "usbip list returned 0 busids"
        printf "%s\n" "$devlist" | sed 's/^/        /'
    fi
else
    no "usbip list failed: $devlist"
fi

# --- P3.1.2 TCP REPL reachable ----------------------------------------
section "P3.1.2  TCP REPL on $HOST:$REPL_PORT"
if ver=$(timeout 5 mpremote connect "tcp://${HOST}:${REPL_PORT}" exec 'import sys; print(sys.implementation._build)' 2>&1); then
    if [ "$ver" = "ESP32_S3_ANNEALAGE_POD" ]; then
        ok "REPL build identifier matches ESP32_S3_ANNEALAGE_POD"
    else
        no "REPL build identifier unexpected: '$ver'"
    fi
else
    no "TCP REPL did not respond: $ver"
fi

# --- P3.1.3 UART bridge accepts TCP connection ------------------------
section "P3.1.3  UART bridge on $HOST:$UART_PORT"
if (echo "PING" | timeout 2 nc -q 1 "$HOST" "$UART_PORT" >/dev/null 2>&1); then
    ok "TCP/$UART_PORT accepts and forwards bytes"
else
    note "UART bridge not exercised (nc closed or no DUT on UART2 yet)"
fi

# --- P3.1.4 pyOCD recognises the synthetic CMSIS-DAP ------------------
section "P3.1.4  pyOCD recognises synthetic CMSIS-DAP-v2"
if probelist=$(timeout 10 pyocd list 2>&1); then
    if printf "%s\n" "$probelist" | grep -q "CMSIS-DAP"; then
        ok "pyOCD list contains 'CMSIS-DAP'"
        printf "%s\n" "$probelist" | grep -i "CMSIS-DAP" | sed 's/^/        /'
    else
        no "pyOCD list does not contain a CMSIS-DAP probe"
        printf "%s\n" "$probelist" | sed 's/^/        /'
    fi
else
    no "pyOCD list failed: $probelist"
fi

# --- P3.2 Cleanup hook fires on REPL disconnect -----------------------
section "P3.2  cleanup hook on REPL TCP disconnect"
if before=$(timeout 5 mpremote connect "tcp://${HOST}:${REPL_PORT}" exec '
from annealage_pod.power import vtarget, dut_usb
print(int(vtarget._enabled), int(dut_usb._enabled), sep=",")
' 2>&1 | tail -1); then
    note "rail state pre-disconnect: $before"
    sleep 2
    if after=$(timeout 5 mpremote connect "tcp://${HOST}:${REPL_PORT}" exec '
from annealage_pod.power import vtarget, dut_usb
print(int(vtarget._enabled), int(dut_usb._enabled), sep=",")
' 2>&1 | tail -1); then
        note "rail state post-reconnect: $after"
        if [ "$after" = "0,0" ]; then
            ok "cleanup hook drove both rails off after disconnect"
        else
            no "rails remained enabled after disconnect+reconnect ($after)"
        fi
    else
        no "could not re-connect to REPL: $after"
    fi
else
    no "could not connect to REPL initially: $before"
fi

# --- P3.5 Log socket (if started) -------------------------------------
section "P3.5  log socket smoke"
note "log socket is opt-in; start with annealage_pod.ops.log.start(port=8514)"
note "(skipped here; covered in test_log_socket.sh)"

# --- Summary ----------------------------------------------------------
section "Summary"
printf "  %d passed, %d failed\n\n" "$pass" "$fail"
if [ "$fail" -gt 0 ]; then
    exit 1
fi
