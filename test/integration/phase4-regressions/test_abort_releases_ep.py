#!/usr/bin/env python3
"""EP claim release regression for the abort_xfer callback bug.

The fix (hcd/dwc2: fire xfer_complete callback after
hcd_edpt_abort_xfer in upstream PR #3637) makes the channel-halted
IRQ deliver an xfer_complete event with XFER_RESULT_FAILED when the
upper layer aborts the channel. Without it, the in-flight URB never
gets given back, the EP claim stays held in TinyUSB, and a
subsequent CMD_SUBMIT on the same EP comes back as "tuh_*_xfer
rejected" forever - the usbip detach -> reattach cycle wedges.

We exercise it by: attach, kick off a long-running mpremote that
holds a bulk-IN URB in flight, detach mid-flight, immediately
re-attach, and verify the next mpremote round-trips cleanly. If
the EP claim leaked, the second round-trip's first bulk-IN URB
will return "rejected" and mpremote times out.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bench import (
    AttachedDUT,
    fail,
    mpremote,
    passed,
    usbip_attach,
    usbip_detach_all,
    wait_for_ttyACM,
)


def main():
    # First attach: kick off something that will hold a bulk-IN URB.
    usbip_attach()
    try:
        tty = wait_for_ttyACM()

        # Background mpremote that will block in a long read; the
        # DUT prints slowly and we yank the cable mid-read.
        proc = subprocess.Popen(
            ["mpremote", "connect", tty, "resume", "exec",
             "import time\n"
             "for i in range(200):\n"
             "    print(i, 'x' * 60)\n"
             "    time.sleep_ms(50)\n"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Let the bulk-IN pipe fill with at least one URB.
        time.sleep(0.5)

        # Yank the cable. The fix is what makes the EP claim release.
        usbip_detach_all()

        # Wait for the abandoned mpremote to actually die.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # Brief pause so the annealage_pod's tcp_err_cb completes EP cleanup.
        time.sleep(0.2)
    except Exception:
        usbip_detach_all()
        raise

    # Second attach: if the EP claim leaked, the first IN URB
    # rejects and mpremote eval times out.
    with AttachedDUT() as dut:
        r = mpremote("eval", "1+1", tty=dut.tty, timeout=10)
        if r.returncode != 0:
            fail(
                "second-attach eval failed - likely EP claim leaked "
                f"from first attach: rc={r.returncode}\n"
                f"stderr: {r.stderr.strip()}"
            )
        if "2" not in r.stdout:
            fail(f"unexpected eval output: {r.stdout!r}")

    passed("abort + reattach round-tripped clean (no EP claim leak)")


if __name__ == "__main__":
    main()
