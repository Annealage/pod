#!/usr/bin/env python3
"""Run the device->host (DATA IN) leg of serial_test against a CDC TTY.

Background: serial_test.py also has DATA ECHO and DATA OUT tests, but
RP2's stdin does not implement readinto (echo fails), and Pico's stdin
ringbuffer caps the OUT direction at a non-USB-related throughput. Only
DATA IN measures the USB/IP pipe end-to-end.

This wrapper uses pyboard.Pyboard for raw-REPL script entry (robust over
USB/IP latency) then runs serial_test.read_test() against the same
serial connection.

Usage:
  cdc_throughput.py <tty>

Example:
  cdc_throughput.py /dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_a5a2229740635c53-if00
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "src",
                                 "micropython", "tests"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "src",
                                 "micropython", "tools"))

import serial  # type: ignore
import pyboard  # type: ignore
import serial_test


def send_script_via_pyboard(ser, script):
    """Replacement for serial_test.send_script that uses pyboard's robust
    raw-REPL entry. Leaves the device executing `script` and returns
    once the device has sent the leading OK from the raw-REPL exec."""
    pyb = pyboard.Pyboard.__new__(pyboard.Pyboard)
    pyb.serial = ser
    pyb.in_raw_repl = False
    pyb.use_raw_paste = True
    pyb.enter_raw_repl(soft_reset=False)
    pyb.exec_raw_no_follow(script)


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    tty = sys.argv[1]

    ser = serial.Serial(tty, baudrate=115200, timeout=1)

    serial_test.send_script = send_script_via_pyboard
    serial_test.test_passed = True

    print("DATA IN throughput test, tty=%s" % tty)
    rows = []
    bufsize = 256
    nbuf = 128
    while bufsize <= 16384:
        rate = serial_test.read_test(ser, ser, bufsize, nbuf)
        rows.append((bufsize, nbuf, rate))
        if rate:
            nbuf = max(min(128, int(rate * 1.0 / bufsize)), 1)
        bufsize *= 2

    ser.close()

    print()
    print("=== summary (bufsize, nbuf, bytes/sec, kib/sec) ===")
    for bufsize, nbuf, rate in rows:
        print("  bufsize=%-6d nbuf=%-4d rate=%-10.0f kib_s=%.1f"
              % (bufsize, nbuf, rate, rate / 1024))

    if not serial_test.test_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
