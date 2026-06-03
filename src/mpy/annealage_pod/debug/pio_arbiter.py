# PIO block-claim bookkeeping for the pod (Track 2).
#
# Three PIO blocks (0/1/2), 4 SMs and 32 instruction words each. PIO0 is
# permanently CYW43 Wi-Fi. The SWD debug stack (PIO1) and the logic analyser
# (PIO2) each claim a block; this records claims so a second consumer cannot
# silently stomp a live one, and so the "swap SWD out for the analyser" policy
# is explicit and inspectable.
#
# This is bookkeeping only - it does not tear down PIO programs itself. The
# owners do that in their own release() (SWDPio.release / LogicAnalyser.release),
# and `ops` drives the swap. Block 0 (Wi-Fi) is never claimable.


class PioConflict(Exception):
    pass


_RESERVED = {0: "cyw43"}     # block -> permanent owner
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
