#!/usr/bin/env python3
"""uartcdc end-to-end REPL connectivity test.

Exercises the full path:
  host -> usbip -> annealage_pod uartcdc C module -> UART2 GPIO17 -> DUT
  DUT -> GPIO18 -> annealage_pod uartcdc -> usbip -> host

Sequence:
  1. Attach uartcdc virtual device over usbip (fresh attach for clean state)
  2. Open CDC serial port at 115200
  3. Reset DUT via annealage_pod GPIO47 (port open before reset to capture trace)
  4. Wait for '>>> ' prompt; assert TRACE sequence if debug build detected
  5. TX/RX round-trip: send '1+1\\r\\n', assert '2\\r\\n>>> ' in response
  6. mpremote exec "print(3*7)", assert '21' in output
  7. Detach (via context manager)

TRACE notes (dabao firmware):
  - TRACE output requires MICROPY_DEBUG_VERBOSE=1 at build time; release
    builds emit nothing.  The test auto-detects a debug build by checking
    whether any TRACE substring appears in the boot output.  Set
    EXPECT_TRACE=1 in the environment to force TRACE checking (fails if
    not found, useful in CI with a known debug build).
  - uart_ok and tick_ok are emitted once at cold boot (before the
    soft-reboot loop); gc_init..repl are emitted every session start.
    GPIO47 reset is a full chip reset so all six lines appear.

Burst TX note:
  - The Baochip-1x UART RX has no deep hardware FIFO; bytes that arrive
    while the UDMA ISR hasn't completed are dropped at the hardware level.
    DMA-driven RX is not yet available in the SDK.  Burst writes at
    115200 therefore risk drops; char-by-char with ~2 ms gaps is safe.
    mpremote raw REPL mode sends Ctrl-A + code + Ctrl-D as a single
    burst, so mpremote exec may occasionally misfire.  The test retries
    up to 3 times as a mitigation; revisit when DMA RX lands.
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

# uart_ok and tick_ok are cold-boot-only (before the soft-reboot loop).
# gc_init..repl repeat on every session start (inside the loop).
EXPECTED_TRACE = [
    b"uart_ok",
    b"TRACE:tick_ok",
    b"TRACE:gc_init",
    b"TRACE:mp_init",
    b"TRACE:irq_init",
    b"TRACE:repl",
]

EXPECT_TRACE = os.environ.get("EXPECT_TRACE", "").strip() not in ("", "0")


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


def check_trace(boot_buf):
    """Assert TRACE sequence present and in order. Returns True if checked."""
    has_any_trace = any(t in boot_buf for t in EXPECTED_TRACE)
    if not has_any_trace:
        if EXPECT_TRACE:
            fail(
                "EXPECT_TRACE=1 but no TRACE lines found in boot output.\n"
                "Is the DUT firmware built with MICROPY_DEBUG_VERBOSE=1?\n"
                f"Boot output: {boot_buf!r}"
            )
        print("note: no TRACE lines in boot output (release build or MICROPY_DEBUG_VERBOSE=0)")
        return False

    pos = 0
    for trace in EXPECTED_TRACE:
        idx = boot_buf.find(trace, pos)
        if idx == -1:
            fail(
                f"TRACE line {trace!r} missing from boot output.\n"
                f"Boot output: {boot_buf!r}"
            )
        pos = idx + len(trace)
    return True


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

            trace_checked = check_trace(boot_buf)

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
        # Retries up to 3 times due to known burst-TX byte-drop in dabao firmware.
        for attempt in range(3):
            r = mpremote("exec", "print(3*7)", tty=dut.cdc_path)
            if r.returncode == 0 and "21" in r.stdout:
                break
            if attempt < 2:
                print(f"mpremote exec attempt {attempt + 1} failed, retrying...")
                time.sleep(1.0)
        else:
            if r.returncode != 0:
                fail(f"mpremote exec failed after 3 attempts (rc={r.returncode}): {r.stderr.strip()}")
            fail(f"Expected '21' in mpremote output after 3 attempts, got: {r.stdout!r}")

    suffix = " (no TRACE - release build)" if not trace_checked else ""
    passed(f"prompt ok, TX/RX round-trip ok, mpremote exec ok{suffix}")


if __name__ == "__main__":
    main()
