# Annealage Pod DUT control stubs.
#
# Phase 1: signatures only. Phase 2 implements the four reset paths
# from spec.md §3.4: swd, nrst, power, relay.


def reset(mode="swd", relay=None):
    """Reset the DUT.

    mode: one of 'swd', 'nrst', 'power', 'relay'.
    relay: integer 1..7 when mode == 'relay'.
    """
    raise NotImplementedError("dut.reset() not implemented in Phase 1")
