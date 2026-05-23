#!/usr/bin/env python3
"""IN-direction PID save regression for the DMA-mode short-packet bug.

The fix (hcd/dwc2: save post-transfer PID in DMA-mode IN handler in
upstream PR #3637) makes handle_channel_in_dma() save hctsiz.pid into
edpt->next_pid after XFER_COMPLETE; the DMA-mode path used to skip
this even though the slave-mode path did it. After a short IN packet
ending a multi-packet transfer early, the next URB used the
pre-computed toggle instead of the post-transfer one, triggering
DATATOGGLE_ERR. Hardware then either retried (dropping the device's
first packet) or coalesced the duplicate (delivering corrupt bytes).

The cdc-acm symptom is sporadic byte loss or corruption on bulk-IN
right after the device emits a packet shorter than wMaxPacketSize.
We force this pattern by having the DUT emit a sequence of fixed-
size + short prints in raw-REPL exec, and verify the IN stream on
the host matches the expected interleave.

This test pins both the DMA path (R27 default) and the slave path
(the older default before R27); the assertion is identical.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bench import AttachedDUT, fail, mpremote, passed

# Each round emits a long line (~120 chars > FS bulk wMaxPacketSize of 64)
# then a short line (~3 chars < wMaxPacketSize). The short packet
# terminates the multi-packet IN transfer early - this is the trigger
# pattern for the bug.
ROUNDS = 100
LONG = "A" * 120
SHORT = "B"


def main():
    with AttachedDUT() as dut:
        script = (
            "import time\n"
            f"for i in range({ROUNDS}):\n"
            f"    print('{LONG}')\n"
            f"    print('{SHORT}')\n"
            "    time.sleep_ms(2)\n"
        )
        r = mpremote("exec", script, tty=dut.tty)
        if r.returncode != 0:
            fail(f"mpremote exec failed: {r.stderr.strip()}")

        # Parse alternating long/short pairs out of stdout.
        lines = [ln.rstrip("\r") for ln in r.stdout.splitlines() if ln]
        long_count = sum(1 for ln in lines if ln == LONG)
        short_count = sum(1 for ln in lines if ln == SHORT)
        garbled = [ln for ln in lines
                   if ln != LONG and ln != SHORT and ln]

        if long_count != ROUNDS:
            fail(
                f"missing long lines: expected {ROUNDS}, got {long_count}; "
                f"likely DATATOGGLE_ERR dropping first packet after short"
            )
        if short_count != ROUNDS:
            fail(
                f"missing short lines: expected {ROUNDS}, got {short_count}"
            )
        if garbled:
            fail(
                f"corrupted lines (PID toggle alias): {len(garbled)} "
                f"lines, first 3: {garbled[:3]!r}"
            )

    passed(f"{ROUNDS} long-then-short rounds verified clean")


if __name__ == "__main__":
    main()
