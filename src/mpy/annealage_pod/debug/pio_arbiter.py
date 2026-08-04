# PIO block-claim bookkeeping for the pod (Track 2).
#
# Three PIO blocks (0/1/2), 4 SMs and 32 instruction words each. On the RP2350
# Pico 2 W, CYW43 Wi-Fi runs on PIO2 (SM0) - it claims a free SM that can reach
# its high-numbered WL pins, which lands on PIO2, NOT PIO0 as on the RP2040.
# Touching PIO2 (building a state machine there) while Wi-Fi is live corrupts
# the CYW43 SM and hard-wedges the chip, so PIO2 is reserved and off-limits.
# The SWD debug stack uses PIO1; PIO0 (free) is shared by the SPI target and the
# logic analyser on distinct state machines. This records per-(block, SM) claims
# so a second consumer cannot silently stomp a live one, while still letting two
# consumers coexist on one block when they use different SMs.
#
# This is bookkeeping only - it does not tear down PIO programs itself. The owners
# do that in their own release()/teardown (SWDPio.release, LogicAnalyser.release,
# SpiTarget.deinit), each removing only its OWN program so a co-tenant on the same
# block survives. Block 2 (Wi-Fi) is never claimable.


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
    0: {"owner": "la/spi", "reserved": False,
        "sm": "0..3; SPI target (sm 0 default) + logic analyser (sm 1) coexist",
        "note": "free block; SPI target and logic analyser claim per-SM and run together"},
    1: {"owner": "swd",   "reserved": False, "sm": "0 (sm_id 4)",
        "note": "SWD transport (swd_dap/swd_pio); owns GP14/GP15"},
    2: {"owner": "cyw43", "reserved": True,  "sm": "0 (sm_id 8)",
        "note": "CYW43 Wi-Fi SPI; OFF-LIMITS - building a SM here wedges the chip"},
}

# Permanently-reserved blocks, derived from PIO_MAP so the two can never disagree.
# A reserved block is off-limits on EVERY state machine (CYW43 owns PIO2 wholesale).
_RESERVED = {blk: m["owner"] for blk, m in PIO_MAP.items() if m["reserved"]}
_claims = {}                 # (block, sm) -> owner (runtime per-SM claims)


def claim(owner, block, sm=0):
    """Claim state machine `sm` of PIO `block` for `owner`.

    Per-SM: two owners can share a block on different SMs (the SPI target on PIO0
    sm 0 and the logic analyser on PIO0 sm 1 coexist). Idempotent for the same
    (owner, block, sm). Raises PioConflict if that SM is held by another owner, or
    if the block is reserved (CYW43's PIO2 is off-limits on every SM).
    """
    reserved = _RESERVED.get(block)
    if reserved is not None and reserved != owner:
        raise PioConflict(
            "PIO%d is reserved for %r; %r cannot claim it"
            % (block, reserved, owner))
    held = _claims.get((block, sm))
    if held is not None and held != owner:
        raise PioConflict(
            "PIO%d sm%d held by %r; %r cannot claim it"
            % (block, sm, held, owner))
    _claims[(block, sm)] = owner
    return block


def release(owner):
    """Drop every (block, sm) claimed by `owner`. Returns the freed keys."""
    freed = [key for key, o in _claims.items() if o == owner]
    for key in freed:
        del _claims[key]
    return freed


def holder(block, sm=None):
    """Who holds a block / SM. A reserved block returns its owner (all SMs). Else
    with `sm` given, that SM's owner or None; with `sm` omitted, a {sm: owner} map
    of the block's claimed SMs (empty if none)."""
    reserved = _RESERVED.get(block)
    if reserved is not None:
        return reserved
    if sm is None:
        return {s: o for (b, s), o in _claims.items() if b == block}
    return _claims.get((block, sm))


def status():
    """Snapshot: {block: {"reserved": owner}} for reserved blocks and
    {block: {sm: owner, ...}} for claimed SMs."""
    out = {blk: {"reserved": o} for blk, o in _RESERVED.items()}
    for (blk, sm), o in _claims.items():
        out.setdefault(blk, {})[sm] = o
    return out
