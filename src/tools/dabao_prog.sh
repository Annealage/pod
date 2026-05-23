#!/usr/bin/env bash
# dabao_prog.sh
#
# Release the dabao from boot1 back into normal firmware mode:
#   GPIO48 (PROG) released to high-Z (pull-up deasserts PROG)
#   GPIO47 (RUN) pulsed LOW -> high-Z (pull-up releases reset)
#
# Exits 0 immediately after the GPIO sequence; does not wait for USB
# enumeration since user firmware may not enumerate (that is expected
# behaviour for pre-production builds).

set -uo pipefail

ANNEALAGE_POD_PORT="/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A45040839-if00"

if [[ ! -e "$ANNEALAGE_POD_PORT" ]]; then
    echo "ERROR: annealage_pod not found at $ANNEALAGE_POD_PORT" >&2
    exit 1
fi

echo "dabao_prog: releasing PROG (GPIO48=hi-Z), pulsing RESET (GPIO47 0->hi-Z) ..."

mpremote connect "$ANNEALAGE_POD_PORT" resume exec \
"import time
from machine import Pin
Pin(48, Pin.IN)
gpio47 = Pin(47, Pin.OUT, value=0)
time.sleep_ms(50)
gpio47.init(Pin.IN)
print('ok')
" 2>&1 | grep -v "^$" | sed 's/^/  mpremote: /'

echo "dabao_prog: done -- chip running user firmware"
