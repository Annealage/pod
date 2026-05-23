#!/usr/bin/env python3
"""Control-transfer timeout regression for the tuh_control_xfer wedge.

The fix (host: honour tuh_xfer_t.timeout_ms in tuh_control_xfer in
upstream PR #3637) makes the synchronous tuh_control_xfer path
respect xfer->timeout_ms instead of busy-looping forever. Without
it, any device that NAKs a control transfer indefinitely (e.g.
some cdc-acm devices on CLEAR_FEATURE(ENDPOINT_HALT)) wedges the
caller permanently.

This is the hardest of the four to drive from userspace because the
trigger condition is a device that NAKs a *specific* control request
indefinitely, which the standard MicroPython firmware doesn't do.
The usbip protocol exposes control transfers (we can compose a
CMD_SUBMIT for any setup packet), but a typical DUT just answers,
not NAK-loops.

Two ways to validate this fix end-to-end:

  1. Manual: connect a known-misbehaving cdc-acm device (some
     SiLabs / Prolific clones NAK CLEAR_FEATURE indefinitely),
     attach, run mpremote, kill the host side mid-call, reattach.
     The first attempt's setup is what triggers the wedge.

  2. Synthetic: build a MicroPython firmware variant with a control
     handler that NAKs a specific vendor request indefinitely, then
     drive that request from the host. (Not in this tree.)

The script below does the closest userspace approximation: attach,
hammer cdc-acm with rapid open/close cycles which exercise
CLEAR_FEATURE(ENDPOINT_HALT) on the bulk endpoints. Without the
timeout fix, one of these calls is statistically likely to wedge
within 10 rounds on a marginal device. With the fix, all rounds
complete within their per-attempt timeout.

This is a smoke test, not a deterministic repro. The deterministic
repro requires the synthetic firmware (TODO: track in
test/integration/phase4-regressions/synth-naker-firmware.md).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bench import AttachedDUT, fail, mpremote, passed

ROUNDS = 20
PER_ROUND_TIMEOUT_S = 5


def main():
    fails = []
    for i in range(ROUNDS):
        try:
            with AttachedDUT() as dut:
                t0 = time.monotonic()
                r = mpremote(
                    "eval", "1+1",
                    tty=dut.tty,
                    timeout=PER_ROUND_TIMEOUT_S,
                )
                dt = time.monotonic() - t0
                if r.returncode != 0:
                    fails.append((i, "eval rc!=0", dt, r.stderr.strip()))
                elif "2" not in r.stdout:
                    fails.append((i, "wrong output", dt, r.stdout.strip()))
                elif dt > PER_ROUND_TIMEOUT_S * 0.8:
                    # Took most of the per-round budget - control xfer
                    # probably nearly wedged.
                    fails.append(
                        (i, "too slow", dt, f"{dt:.1f}s")
                    )
        except Exception as e:
            fails.append((i, "exception", -1.0, str(e)))

    if fails:
        for i, kind, dt, detail in fails:
            print(
                f"  round {i}: {kind} (took {dt:.1f}s): {detail}",
                file=sys.stderr,
            )
        fail(
            f"{len(fails)}/{ROUNDS} attach+eval rounds tripped a "
            "control-xfer wedge - timeout_ms not honoured?"
        )

    passed(f"{ROUNDS} attach+eval rounds completed without wedge")


if __name__ == "__main__":
    main()
