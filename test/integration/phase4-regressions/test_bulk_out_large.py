#!/usr/bin/env python3
"""Bulk-OUT correctness regression for the multi-packet txfifo bug.

The fix (hcd/dwc2: fix txfifo full check in upstream PR #3637) re-
reads the FIFO/request-queue space inside the per-packet write loop
in handle_txfifo_empty(). Without it, cached values were stale after
the first packet write, so a multi-packet bulk-OUT could be issued
without enough space and XFER_COMPLETE never fired - the data on the
wire was corrupted.

The cdc-acm symptom is a small file copy (>1 FS bulk packet, so
>64 B) arriving garbled on the device side. We send a structured
payload via `mpremote fs cp`, run a Python script on the DUT that
reads it back and computes a CRC, and assert the CRC matches what
we sent.
"""

import os
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bench import AttachedDUT, fail, mpremote, passed

PAYLOAD_SIZE = 3700  # >50 FS bulk packets; enough to hit the bug
PAYLOAD_SEED = 0xCAFEBABE


def build_payload():
    """Deterministic non-trivial payload; raw bytes, no LF-CR
    confusion. Avoid backslash so it survives the cp pipeline."""
    import struct
    rng = struct.Struct("<I")
    out = bytearray()
    x = PAYLOAD_SEED
    while len(out) < PAYLOAD_SIZE:
        # LCG step; pick low bytes only to stay in printable-ASCII-ish
        # territory and avoid 0x0d / 0x0a edge cases through any
        # repl-mode pipeline.
        x = (1103515245 * x + 12345) & 0x7fffffff
        out += rng.pack(x)
    return bytes(out[:PAYLOAD_SIZE])


def main():
    payload = build_payload()
    expected_crc = zlib.crc32(payload) & 0xffffffff

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(payload)
        local_path = f.name

    try:
        with AttachedDUT() as dut:
            # Push the file over bulk-OUT through the bridge.
            r = mpremote(
                "fs", "cp", local_path, ":bulk_payload.bin",
                tty=dut.tty,
            )
            if r.returncode != 0:
                fail(f"mpremote fs cp failed: {r.stderr.strip()}")

            # Have the DUT compute CRC32 and stat the size.
            r = mpremote(
                "exec",
                "import binascii\n"
                "with open('bulk_payload.bin', 'rb') as f:\n"
                "    data = f.read()\n"
                "import zlib\n"
                "print('SIZE=%d CRC=%08x' % (len(data), zlib.crc32(data) & 0xffffffff))\n",
                tty=dut.tty,
            )
            if r.returncode != 0:
                fail(f"mpremote exec failed: {r.stderr.strip()}")

            line = next(
                (ln for ln in r.stdout.splitlines() if ln.startswith("SIZE=")),
                "",
            )
            if not line:
                fail(f"no SIZE= line in output:\n{r.stdout}")

            kv = dict(p.split("=") for p in line.split())
            size = int(kv["SIZE"])
            got_crc = int(kv["CRC"], 16)

            if size != PAYLOAD_SIZE:
                fail(f"size mismatch: sent {PAYLOAD_SIZE}, got {size}")
            if got_crc != expected_crc:
                fail(
                    f"crc mismatch: sent {expected_crc:08x}, "
                    f"got {got_crc:08x} - bulk-OUT corruption"
                )

            # Tidy up.
            mpremote("fs", "rm", ":bulk_payload.bin", tty=dut.tty)
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass

    passed(f"{PAYLOAD_SIZE} B round-tripped clean, crc {expected_crc:08x}")


if __name__ == "__main__":
    main()
