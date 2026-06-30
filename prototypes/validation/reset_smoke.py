"""Tier-2 regression guard: ops.reset(mode='sysreset') leaves the DUT running.

The reset path (AIRCR SYSRESETREQ via cm.sysreset(), then resume) already exists
and is hardware-validated, and this work batch added no new reset code; so this
is a smoke / regression guard rather than a first-validation. It issues a
sysreset through the host Pod client and confirms the core is executing
afterwards.

Forward progress is checked via the DWT cycle counter (CYCCNT), not a PC sample:
a firmware that idles in a tight loop sits at a fixed PC, so "PC changed between
two halts" gives false negatives. A CYCCNT that advances while the core is not
halted is an unambiguous running signal.

Run (after the firmware is deployed):
  python3 prototypes/validation/reset_smoke.py [pod-label]

Prints PASS/FAIL with DHCSR and the cycle-count delta. The DUT is left running.
"""

import time

from _podboot import pod_from_label

DHCSR = 0xE000EDF0
DEMCR = 0xE000EDFC
DWT_CTRL = 0xE0001000
DWT_CYCCNT = 0xE0001004
S_HALT = 1 << 17
DEMCR_TRCENA = 1 << 24
DWT_CYCCNTENA = 1 << 0


def _u32(pod, addr):
    r = pod.read_mem(addr, 4)
    if not r.get("ok"):
        return None
    return int.from_bytes(bytes.fromhex(r["hex"]), "little")


def _w32(pod, addr, val):
    pod.write_mem(addr, val.to_bytes(4, "little").hex())


def main():
    label, pod = pod_from_label()
    print("reset smoke (sysreset) against pod %r (nRF DUT)" % label)

    res = pod.reset_dut(mode="sysreset")
    print("reset_dut(sysreset):", res)
    if not res.get("ok"):
        print("FAIL: reset returned not-ok: %s" % res.get("err"))
        return 1

    # Let the core leave the reset vector and start executing.
    time.sleep(0.3)

    dhcsr = _u32(pod, DHCSR)
    if dhcsr is None:
        print("FAIL: could not read DHCSR after reset (core unresponsive)")
        return 1
    halted = bool(dhcsr & S_HALT)

    # Enable the cycle counter and confirm it advances (forward progress).
    demcr = _u32(pod, DEMCR)
    _w32(pod, DEMCR, demcr | DEMCR_TRCENA)
    ctrl = _u32(pod, DWT_CTRL)
    _w32(pod, DWT_CTRL, ctrl | DWT_CYCCNTENA)
    c1 = _u32(pod, DWT_CYCCNT)
    time.sleep(0.05)
    c2 = _u32(pod, DWT_CYCCNT)
    delta = None if (c1 is None or c2 is None) else (c2 - c1) & 0xFFFFFFFF
    print("DHCSR=0x%08x (halted=%s)  CYCCNT delta=%s"
          % (dhcsr, halted, delta))

    if halted:
        print("FAIL: core halted after sysreset (expected running)")
        return 1
    if not delta:
        print("FAIL: cycle counter did not advance; core not executing")
        return 1

    print("PASS: sysreset completed and the core is executing "
          "(%d cycles in ~50 ms)" % delta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
