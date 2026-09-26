"""Tier-1 validation: the generic CMSIS FLM runner against the live nRF DUT.

Validates the generic flash-algorithm runner mechanism (flm.FLMFlasher: load the
PIC Thumb blob into target SRAM, drive Init/EraseSector/ProgramPage through the
core registers + MEM-AP, read R0 status) on the nRF52840, using the host's
pack-resolved algorithm rather than a baked-in blob - the path
cmsis-flash-completion.md gap 2 calls out as never having run on silicon. This
exercises the runner contract itself, decoupled from any non-nRF silicon: the
only difference for another part is the algorithm dict, so a pass here proves
the mechanism that generalises to any chip with a CMSIS pack.

Method:
  1. host resolves the DUT's declared target_family from its CMSIS pack and
     installs it on the pod (ops.set_flm_algo), the same path every flash/erase
     uses in production;
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

# One self-contained on-pod program, run against whatever algorithm the host
# has already installed via ops.set_flm_algo (see main()). Printed dict is
# parsed by the host. It never streams flash back to the host; only the
# 256-byte readback is compared on the pod and a small result dict returned.
_POD_CODE = r'''
import annealage_pod.debug.ops as o

ADDR = {addr}
N = {n}

res = {{"ok": False, "addr": ADDR}}
try:
    dp, ap, cm = o._ensure()
    fr = o._require_flm()
    # reload(), not load(): _require_flm may return an FLMFlasher cached from
    # an earlier operation in this same pod session, whose _loaded=True no
    # longer holds - _flm_restore's sysreset (between that operation and this
    # one) reboots the DUT into its own firmware, which runs over and clobbers
    # the algorithm blob's SRAM. load() trusts the stale flag and skips
    # re-uploading; reload() always re-uploads.
    fr.reload()
    PAGE = fr.page_size

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

    o._flm_restore(cm)
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
    print("resolving the algorithm from the DUT's declared CMSIS pack...")
    pod.ensure_flm_algo()
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
