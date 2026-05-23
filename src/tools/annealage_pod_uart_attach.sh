#!/usr/bin/env bash
# annealage_pod_uart_attach.sh <annealage_pod-host>
#
# Attaches the annealage_pod's synthetic CDC UART device (VID c251 PID f00b)
# via USB/IP and prints the resulting /dev/ttyACM* path.
# The busid is discovered dynamically from `usbip list` so it survives
# usbip server restarts that renumber virtual devices.
#
# The annealage_pod exposes the DUT hardware UART (UART2) as a synthetic USB
# CDC ACM device served by the usbip server on port 3240.  Attaching it
# here makes it appear as a local ttyACM node and lets any serial tool
# (minicom, screen, pyserial) talk to the DUT UART directly.
#
# Usage:
#   sudo annealage_pod_uart_attach.sh 192.168.1.42
#   sudo annealage_pod_uart_attach.sh annealage_pod-aabbcc.local
#
# Requires: usbip (linux-tools or usbip-utils), usbip kernel modules loaded.
# Must be run as root (usbip attach requires it).

set -uo pipefail

HOST="${1:-}"
VID_PID="c251:f00b"  # UART_CDC_VID / UART_CDC_PID from uart_cdc_device.h
WAIT_SECS=5

if [[ -z "$HOST" ]]; then
    echo "Usage: $0 <annealage_pod-host>" >&2
    exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: must be run as root (usbip attach requires root)" >&2
    exit 1
fi

# Ensure the vhci-hcd kernel module is loaded.
if ! lsmod | grep -q vhci_hcd; then
    modprobe vhci-hcd || { echo "ERROR: failed to load vhci-hcd" >&2; exit 1; }
fi

# Snapshot ttyACM devices that exist before attach.
before=( /dev/ttyACM* )

echo "annealage_pod_uart: listing devices on $HOST ..."
LIST=$(usbip list -r "$HOST" 2>&1)
BUSID=$(echo "$LIST" | grep -B1 "$VID_PID" | grep -oE '[0-9]+-[0-9]+' | head -1)
if [[ -z "$BUSID" ]]; then
    echo "ERROR: CDC UART device ($VID_PID) not found on $HOST" >&2
    echo "       Is the annealage_pod running and uartcdc attached?" >&2
    exit 1
fi

echo "annealage_pod_uart: attaching busid $BUSID from $HOST ..."
usbip attach -r "$HOST" -b "$BUSID"

# Wait for a new ttyACM to appear.
DEADLINE=$(( $(date +%s) + WAIT_SECS ))
while [[ $(date +%s) -lt $DEADLINE ]]; do
    for dev in /dev/ttyACM*; do
        [[ -e "$dev" ]] || continue
        already=false
        for b in "${before[@]}"; do
            [[ "$b" == "$dev" ]] && already=true && break
        done
        if ! $already; then
            echo "annealage_pod_uart: OK -- $dev"
            exit 0
        fi
    done
    sleep 0.2
done

echo "ERROR: no new ttyACM appeared within ${WAIT_SECS}s after attach" >&2
exit 1
