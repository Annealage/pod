"""Unit tests for the PIO arbiter's per-(block, SM) claim model (task #6, Phase A).

Pure-Python bookkeeping (no rp2/machine), so it runs on the Unix port. Covers the
concurrency contract: two owners coexist on one block via distinct SMs, same-SM
claims conflict, reserved blocks (CYW43's PIO2) are off-limits on every SM, and
release() frees only the caller's SMs.
"""

import pytest

from annealage_pod.debug import pio_arbiter


@pytest.fixture(autouse=True)
def _clean_arbiter():
    """Reset the module-global claim table around each test."""
    pio_arbiter._claims.clear()
    yield
    pio_arbiter._claims.clear()


def test_two_owners_coexist_on_distinct_sms():
    pio_arbiter.claim("spi", 0, 0)
    pio_arbiter.claim("la", 0, 1)
    assert pio_arbiter.holder(0, 0) == "spi"
    assert pio_arbiter.holder(0, 1) == "la"
    assert pio_arbiter.holder(0) == {0: "spi", 1: "la"}


def test_same_sm_conflicts():
    pio_arbiter.claim("spi", 0, 0)
    with pytest.raises(pio_arbiter.PioConflict):
        pio_arbiter.claim("la", 0, 0)


def test_same_owner_same_sm_is_idempotent():
    pio_arbiter.claim("spi", 0, 0)
    pio_arbiter.claim("spi", 0, 0)   # no raise
    assert pio_arbiter.holder(0, 0) == "spi"


def test_reserved_block_off_limits_on_every_sm():
    for sm in range(4):
        with pytest.raises(pio_arbiter.PioConflict):
            pio_arbiter.claim("x", 2, sm)
    # A reserved block reports its owner for any SM query.
    assert pio_arbiter.holder(2) == "cyw43"
    assert pio_arbiter.holder(2, 3) == "cyw43"


def test_release_frees_only_that_owner():
    pio_arbiter.claim("spi", 0, 0)
    pio_arbiter.claim("la", 0, 1)
    freed = pio_arbiter.release("spi")
    assert freed == [(0, 0)]
    assert pio_arbiter.holder(0, 0) is None
    assert pio_arbiter.holder(0, 1) == "la"
    # The freed SM can now be reclaimed by another owner.
    pio_arbiter.claim("other", 0, 0)
    assert pio_arbiter.holder(0, 0) == "other"


def test_one_owner_can_hold_multiple_sms():
    pio_arbiter.claim("la", 0, 1)
    pio_arbiter.claim("la", 0, 2)
    assert pio_arbiter.holder(0) == {1: "la", 2: "la"}
    pio_arbiter.release("la")
    assert pio_arbiter.holder(0) == {}


def test_status_shape():
    pio_arbiter.claim("swd", 1, 0)
    pio_arbiter.claim("spi", 0, 0)
    st = pio_arbiter.status()
    assert st[2] == {"reserved": "cyw43"}
    assert st[1] == {0: "swd"}
    assert st[0] == {0: "spi"}
