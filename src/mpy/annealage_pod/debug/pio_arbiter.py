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


_RESERVED = {2: "cyw43"}     # block -> permanent owner (PIO2 = CYW43 Wi-Fi)
_claims = {}                 # block -> owner


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
