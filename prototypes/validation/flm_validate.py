"""Tier-1 validation: the generic CMSIS FLM runner against the live nRF DUT.

Validates the generic flash-algorithm runner mechanism (flm.FLMFlasher: load the
PIC Thumb blob into target SRAM, drive Init/EraseSector/ProgramPage through the
core registers + MEM-AP, read R0 status) on the nRF52840. This exercises the
runner contract itself, decoupled from any non-nRF silicon: the only difference
for another part is the algorithm dict, so a pass here proves the mechanism that
generalises to any chip with a CMSIS pack.

Method (all on the pod, one exec round-trip):
  1. ensure the SWD session, build FLMFlasher over the cached MEM-AP / CortexM
     with the nRF52840 algo;
  2. pick a high scratch flash page (0xF8000, well clear of low/used flash) and
     save its current 4 KB contents via the MEM-AP;
  3. erase the page, program a known 256-byte pattern through the FLM runner
     (erase=False, verify=False - the script does its own readback);
  4. read the 256 bytes back via the MEM-AP and diff against the pattern;
  5. restore the page: erase, then re-program the saved original if it held any
     non-erased byte (re-runnable, leaves the DUT flash as found).

Run (after the firmware is deployed; the orchestrator runs this, not the agent):
  python3 prototypes/validation/flm_validate.py [pod-label]

Prints PASS/FAIL with the on-pod result dict (status codes, mismatch offset).
"""

from _podboot import pod_from_label

SCRATCH_ADDR = 0xF8000        # high scratch page; 4 KB-aligned, clear of low flash
PATTERN_LEN = 256

# One self-contained on-pod program. Printed dict is parsed by the host. It
# never streams flash back to the host; only the 256-byte readback is compared
# on the pod and a small result dict is returned.
_POD_CODE = r'''
import annealage_pod.debug.ops as o
from annealage_pod.debug import flm
from annealage_pod.debug import flm_nrf52840

ADDR = {addr}
N = {n}
PAGE = flm_nrf52840.FLASH_ALGO["page_size"]

res = {{"ok": False, "addr": ADDR}}
try:
    dp, ap, cm, fl = o._ensure()
    fr = flm.FLMFlasher(ap, cm, flm_nrf52840.FLASH_ALGO)

    # Save the current page so the script is non-destructive / re-runnable.
    saved = ap.read_block32(ADDR, PAGE // 4)
    saved_dirty = any(w != 0xFFFFFFFF for w in saved)

    # Build a deterministic pattern (0,1,2,...) of N bytes.
    pattern = bytes((i & 0xFF) for i in range(N))

    # Erase the page and program the pattern through the FLM runner.
    fr.load()
    fr.init(1)              # op 1 = erase
    fr.erase_sector(ADDR)
    fr.uninit(1)
    fr.init(2)              # op 2 = program
    fr.program_page(ADDR, pattern)
    fr.uninit(2)

    # Read the pattern back over the MEM-AP and diff.
    rb = b"".join(int(w).to_bytes(4, "little")
                  for w in ap.read_block32(ADDR, (N + 3) // 4))[:N]
    mismatch = -1
    for i in range(N):
        if rb[i] != pattern[i]:
            mismatch = i
            break

    # Restore the original page (erase always; re-program only if it had data).
    fr.init(1)
    fr.erase_sector(ADDR)
    fr.uninit(1)
    if saved_dirty:
        fr.init(2)
        orig = b"".join(int(w).to_bytes(4, "little") for w in saved)
        off = 0
        while off < len(orig):
            fr.program_page(ADDR + off, orig[off:off + PAGE])
            off += PAGE
        fr.uninit(2)

    cm.resume()
    res = {{"ok": mismatch < 0, "addr": ADDR, "n": N, "mismatch": mismatch,
            "saved_dirty": saved_dirty,
            "rb_head": rb[:8].hex(), "want_head": pattern[:8].hex()}}
except Exception as e:
    res = {{"ok": False, "addr": ADDR, "err": repr(e)}}
print(res)
'''


def main():
    label, pod = pod_from_label()
    print("FLM runner validation against pod %r (nRF DUT), scratch 0x%05X"
          % (label, SCRATCH_ADDR))
    code = _POD_CODE.format(addr=SCRATCH_ADDR, n=PATTERN_LEN)
    from pod.client import _last_dict
    out = pod.exec(code)
    res = _last_dict(out)
    print("on-pod result:", res)
    if res.get("ok"):
        print("PASS: FLM runner programmed and read back %d bytes at 0x%05X "
              "(want %s, got %s)"
              % (res.get("n"), SCRATCH_ADDR, res.get("want_head"),
                 res.get("rb_head")))
        return 0
    print("FAIL: %s" % (res.get("err")
                        or ("mismatch at byte %d" % res.get("mismatch", -1))))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
