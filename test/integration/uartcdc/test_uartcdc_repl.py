#!/usr/bin/env python3
"""uartcdc end-to-end REPL connectivity test.

Exercises the full path:
  host -> usbip -> annealage_pod uartcdc C module -> UART2 GPIO17 -> DUT
  DUT -> GPIO18 -> annealage_pod uartcdc -> usbip -> host

Sequence:
  1. Attach uartcdc virtual device over usbip (fresh attach for clean state)
  2. Open CDC serial port at 115200
  3. Reset DUT via annealage_pod GPIO47 (port open before reset to capture trace)
  4. Assert full TRACE sequence arrives in order and '>>> ' prompt follows
  5. TX/RX round-trip: send '1+1\\r\\n', assert '2\\r\\n>>> ' in response
  6. mpremote exec "print(3*7)", assert '21' in output
  7. Detach (via context manager)
"""

import os
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bench import AttachedUartCDC, dut_reset_via_annealage_pod, fail, mpremote, passed

BOOT_TIMEOUT_S = 3.0
REPLY_TIMEOUT_S = 2.0
PROMPT = b">>> "

EXPECTED_TRACE = [
    # First line: UART is initialising during this very transmission so the
    # "TRACE:" prefix bytes may be corrupt. Match only the suffix.
    b"uart_ok",
    b"TRACE:tick_ok",
    b"TRACE:gc_init",
    b"TRACE:mp_init",
    b"TRACE:irq_init",
    b"TRACE:repl",
]


def read_until(ser, marker, timeout_s):
    """Read from ser until marker appears or timeout. Returns all bytes received."""
    buf = b""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            if marker in buf:
                return buf
        else:
            time.sleep(0.01)
    return buf


def main():
    with AttachedUartCDC() as dut:
        ser = serial.Serial(dut.cdc_path, 115200, timeout=0.1)
        try:
            dut_reset_via_annealage_pod()

            boot_buf = read_until(ser, PROMPT, BOOT_TIMEOUT_S)
            if PROMPT not in boot_buf:
                fail(
                    f"'>>> ' not received within {BOOT_TIMEOUT_S}s after reset.\n"
                    f"Received: {boot_buf!r}"
                )

            # Assert TRACE lines present and in order.
            pos = 0
            for trace in EXPECTED_TRACE:
                idx = boot_buf.find(trace, pos)
                if idx == -1:
                    fail(
                        f"TRACE line {trace!r} missing from boot output.\n"
                        f"Boot output: {boot_buf!r}"
                    )
                pos = idx + len(trace)

            # TX/RX round-trip via raw serial.
            ser.write(b"1+1\r\n")
            reply = read_until(ser, PROMPT, REPLY_TIMEOUT_S)
            if b"2" not in reply:
                fail(f"Expected '2' in REPL response, got: {reply!r}")
            if PROMPT not in reply:
                fail(f"No prompt after REPL response: {reply!r}")

        finally:
            ser.close()

        # mpremote exec: validates raw REPL and the full mpremote connection path.
        r = mpremote("exec", "print(3*7)", tty=dut.cdc_path)
        if r.returncode != 0:
            fail(f"mpremote exec failed (rc={r.returncode}): {r.stderr.strip()}")
        if "21" not in r.stdout:
            fail(f"Expected '21' in mpremote output, got: {r.stdout!r}")

    passed("boot trace ok, TX/RX round-trip ok, mpremote exec ok")


if __name__ == "__main__":
    main()
