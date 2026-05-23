#!/usr/bin/env bash
# dabao_reset.sh
#
# Put the dabao into boot1 (bootloader) mode via the ESP32-S3 GPIO lines:
#   GPIO48 (PROG) LOW  = PROG asserted (bootloader on next boot)
#   GPIO47 (RUN)  LOW  = RESET held
#   GPIO47 (RUN)  hi-Z = RESET released -> chip starts in boot1
#
# Waits up to 5 s for the dabao to enumerate as 1d50:6196 on USB, then
# exits 0 on success or 1 on failure with a diagnostic message.
#
# GPIO47 (RUN) is pulsed LOW to assert reset then released to high-Z.
# GPIO48 (PROG) is held LOW during reset and released to high-Z after.
# PROG is level-sensitive: holding it LOW after boot prevents handoff to
# user firmware. boot1 stays up because bootwait is enabled in NVM.

set -uo pipefail

ANNEALAGE_POD_PORT="/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A45040839-if00"
DABAO_BY_ID_GLOB="/dev/serial/by-id/usb-Baochip_Baochip-1x_*"
WAIT_SECS=5
POLL_MS=100

if [[ ! -e "$ANNEALAGE_POD_PORT" ]]; then
    echo "ERROR: annealage_pod not found at $ANNEALAGE_POD_PORT" >&2
    exit 1
fi

echo "dabao_reset: asserting PROG (GPIO48=0), pulsing RESET (GPIO47 0->1) ..."

mpremote connect "$ANNEALAGE_POD_PORT" resume exec \
"import time
from machine import Pin
# Assert PROG then RESET, release RESET first, then PROG.
# PROG must be released (high-Z) before the chip completes boot or it
# continuously prevents handoff to user firmware -- PROG is level-sensitive,
# not edge-sampled.
gpio48 = Pin(48, Pin.OUT, value=0)
gpio47 = Pin(47, Pin.OUT, value=0)
time.sleep_ms(50)
gpio47.init(Pin.IN)  # release RESET (pull-up takes it high)
time.sleep_ms(50)
gpio48.init(Pin.IN)  # release PROG (pull-up takes it high)
print('ok')
" 2>&1 | grep -v "^$" | sed 's/^/  mpremote: /'

echo "dabao_reset: flushing annealage_pod UART2 RX (clear reset-transient latch) ..."

mpremote connect "$ANNEALAGE_POD_PORT" resume exec \
"import uartcdc
try:
    uartcdc.flush_rx()
    print('ok')
except AttributeError:
    print('flush_rx not available (old firmware)')
" 2>&1 | grep -v "^$" | sed 's/^/  mpremote: /'

echo "dabao_reset: waiting up to ${WAIT_SECS}s for 1d50:6196 ..."

DEADLINE=$(( $(date +%s) + WAIT_SECS ))
while [[ $(date +%s) -lt $DEADLINE ]]; do
    # shellcheck disable=SC2086
    matches=( $DABAO_BY_ID_GLOB )
    if [[ -e "${matches[0]}" ]]; then
        echo "dabao_reset: OK -- ${matches[0]}"
        exit 0
    fi
    sleep "0.${POLL_MS}"
done

echo "ERROR: dabao_reset: 1d50:6196 did not appear within ${WAIT_SECS}s" >&2
exit 1
