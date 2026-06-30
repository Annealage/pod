"""Tier-1 validation: halt + core-register read + RAM word read/write round-trip.

Drives the on-pod single-shot SWD peek/poke surface through the host Pod client
(the same path the MCP dut_* tools use). Halts the nRF, reads the core registers
(pc/sp/lr/xpsr), reads a RAM word, writes then reads back a known value at a
RAM scratch word, confirms the round-trip, restores the original word, and
resumes. Non-destructive: the scratch word is saved and restored, so the DUT's
RAM is left as found and the script is re-runnable.

The orchestrator may also do this directly over MCP (dut_halt / dut_read_reg /
dut_read_mem / dut_write_mem / dut_resume); this script is the standalone form.

Run (after the firmware is deployed):
  python3 prototypes/validation/peekpoke_validate.py [pod-label]

Prints PASS/FAIL with the observed register and memory values.
"""

import binascii

from _podboot import pod_from_label

# Scratch RAM word. 0x20000000 is the Cortex-M SRAM base; the word is saved and
# restored, so touching live firmware RAM is harmless (the core is halted across
# the write/read/restore and the original value is put back before resume).
SCRATCH_ADDR = 0x20000000
TEST_VALUE = 0xA5A5_5A5A


def _word_from_hex(hexstr):
    return int.from_bytes(binascii.unhexlify(hexstr)[:4], "little")


def main():
    label, pod = pod_from_label()
    print("peek/poke validation against pod %r (nRF DUT)" % label)
    failures = []

    h = pod.halt_dut()
    print("halt:", h)
    if not h.get("ok"):
        print("FAIL: could not halt the core: %s" % h.get("err"))
        return 1
    try:
        # Core registers (require a halted core).
        regs = {}
        for name in ("pc", "sp", "lr", "xpsr"):
            r = pod.read_reg(name)
            if not r.get("ok"):
                failures.append("read_reg %s: %s" % (name, r.get("err")))
            else:
                regs[name] = r["value"]
        print("regs:", {k: "0x%08x" % v for k, v in regs.items()})
        # PC should be in the code region (< SRAM base) for halted firmware; a
        # plausibility check, not a hard gate (a halt in a RAM function is legal).
        if "pc" in regs and regs["pc"] == 0:
            failures.append("pc read back as 0 (suspect)")

        # RAM word read.
        before = pod.read_mem(SCRATCH_ADDR, 4)
        if not before.get("ok"):
            failures.append("read_mem before: %s" % before.get("err"))
            orig = None
        else:
            orig = _word_from_hex(before["hex"])
            print("scratch 0x%08x before = 0x%08x" % (SCRATCH_ADDR, orig))

        # RAM word write + read-back.
        data = TEST_VALUE.to_bytes(4, "little")
        w = pod.write_mem(SCRATCH_ADDR, data)
        if not w.get("ok"):
            failures.append("write_mem: %s" % w.get("err"))
        else:
            rb = pod.read_mem(SCRATCH_ADDR, 4)
            got = _word_from_hex(rb["hex"]) if rb.get("ok") else None
            print("scratch 0x%08x after write = %s"
                  % (SCRATCH_ADDR, None if got is None else "0x%08x" % got))
            if got != TEST_VALUE:
                failures.append("round-trip mismatch: wrote 0x%08x got %s"
                                % (TEST_VALUE, got))

        # Restore the original word so the DUT RAM is left as found.
        if orig is not None:
            pod.write_mem(SCRATCH_ADDR, orig.to_bytes(4, "little"))
    finally:
        res = pod.resume_dut()
        print("resume:", res)

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("PASS: halt, core-reg reads, and a RAM word write/read round-trip all OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
