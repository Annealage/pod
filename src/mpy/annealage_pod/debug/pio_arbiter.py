# PIO block-claim bookkeeping for the pod (Track 2).
#
# Three PIO blocks (0/1/2), 4 SMs and 32 instruction words each. On the RP2350
# Pico 2 W, CYW43 Wi-Fi runs on PIO2 (SM0) - it claims a free SM that can reach
# its high-numbered WL pins, which lands on PIO2, NOT PIO0 as on the RP2040.
# Touching PIO2 (building a state machine there) while Wi-Fi is live corrupts
# the CYW43 SM and hard-wedges the chip, so PIO2 is reserved and off-limits.
# The SWD debug stack uses PIO1; the logic analyser uses PIO0 (free). This
# records claims so a second consumer cannot silently stomp a live one.
#
# This is bookkeeping only - it does not tear down PIO programs itself. The
# owners do that in their own release() (SWDPio.release / LogicAnalyser.release).
# Block 2 (Wi-Fi) is never claimable.


class PioConflict(Exception):
    pass


# ── Authoritative PIO block map (single source of truth) ──────────────────────
# This is THE place the pod's PIO block allocation is recorded; other modules and
# docs reference `pio_arbiter.PIO_MAP` rather than restating it (a restated map is
# how the "PIO0 = CYW43" mistake drifted in and put the logic analyser on the live
# Wi-Fi block). Keep this table correct and let everything else point here.
#
# `reserved` means hardware-permanent and never claimable (CYW43 Wi-Fi). The other
# blocks list their assigned owner but are claimed at runtime via claim().
#
# Verified against live hardware by reading the PIO CTRL registers (SM-enable in
# bits[3:0]). Re-check on a new board/build before trusting the map:
#   import machine
#   for blk, base in ((0, 0x50200000), (1, 0x50300000), (2, 0x50400000)):
#       print('PIO%d CTRL=0x%08x' % (blk, machine.mem32[base]))
# On the RP2350 Pico 2 W this reads PIO2 CTRL=0x1 (SM0 live = CYW43), PIO0/PIO1=0x0.
PIO_MAP = {
    0: {"owner": "la",    "reserved": False, "sm": "0..3 (sm_id 0 default)",
        "note": "free block; logic analyser (LogicAnalyser)"},
    1: {"owner": "swd",   "reserved": False, "sm": "0 (sm_id 4)",
        "note": "SWD transport (swd_dap/swd_pio); owns GP14/GP15"},
    2: {"owner": "cyw43", "reserved": True,  "sm": "0 (sm_id 8)",
        "note": "CYW43 Wi-Fi SPI; OFF-LIMITS - building a SM here wedges the chip"},
}

# Permanently-reserved blocks, derived from PIO_MAP so the two can never disagree.
_RESERVED = {blk: m["owner"] for blk, m in PIO_MAP.items() if m["reserved"]}
_claims = {}                 # block -> owner (runtime claims)


def claim(owner, block):
    """Claim a PIO block for `owner`. Idempotent for the same owner; raises
    PioConflict if another owner (or a reserved block) holds it."""
    held = _claims.get(block)
    if held is None:
        held = _RESERVED.get(block)
    if held is not None and held != owner:
        raise PioConflict(
            "PIO%d held by %r; %r cannot claim it" % (block, held, owner))
    _claims[block] = owner
    return block


def release(owner):
    """Drop every block claimed by `owner`. Returns the freed block list."""
    freed = [b for b, o in _claims.items() if o == owner]
    for b in freed:
        del _claims[b]
    return freed


def holder(block):
    """Return the owner of a block (claimed or reserved), or None."""
    h = _claims.get(block)
    return h if h is not None else _RESERVED.get(block)


def status():
    """Snapshot of block -> owner (reserved + claimed)."""
    s = dict(_RESERVED)
    s.update(_claims)
    return s
