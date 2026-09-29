"""Host-side tests for the on-pod Cortex-M debug helpers
(annealage_pod.debug.swd_dap.CortexM).

swd_dap imports swd_pio (rp2 PIO) for its ACK constants only, so a stub carrying
those is swapped into sys.modules while the real swd_dap is loaded under a
private name; the shared swd_dap stub the ops/flm tests use is left alone.
"""

import importlib.util
import os
import sys
import types

import pytest

_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "mpy", "annealage_pod", "debug",
    "swd_dap.py"))
_MPY = os.path.dirname(os.path.dirname(os.path.dirname(_PATH)))
if _MPY not in sys.path:
    sys.path.insert(0, _MPY)


@pytest.fixture
def swd_dap(monkeypatch):
    import annealage_pod.debug  # noqa: F401 - parent package for the relative import
    pio = types.ModuleType("annealage_pod.debug.swd_pio")
    pio.SWD_OK, pio.SWD_WAIT, pio.SWD_FAULT = 1, 2, 4
    monkeypatch.setitem(sys.modules, "annealage_pod.debug.swd_pio", pio)
    spec = importlib.util.spec_from_file_location(
        "annealage_pod.debug._swd_dap_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _CortexM7AP:
    """DHCSR as a Cortex-M7 behaves: C_MASKINTS only changes in a write that
    leaves the core halted; a write that also clears C_HALT keeps the old
    value (the architecture calls that combination UNPREDICTABLE)."""

    def __init__(self, d, halted, maskints):
        self.d = d
        self.halted = halted
        self.maskints = maskints
        self.writes = []

    def read32(self, addr):
        assert addr == self.d.DHCSR
        return (self.d.C_DEBUGEN | (self.d.C_HALT if self.halted else 0)
                | (self.d.C_MASKINTS if self.maskints else 0)
                | (self.d.S_HALT if self.halted else 0))

    def write32(self, addr, value):
        assert addr == self.d.DHCSR and value >> 16 == 0xA05F
        self.writes.append(value)
        halt = bool(value & self.d.C_HALT)
        if halt:
            self.maskints = bool(value & self.d.C_MASKINTS)
        self.halted = halt


def test_resume_after_an_flm_call_unmasks_interrupts(swd_dap):
    # The FLM runner leaves the core halted on its BKPT with C_MASKINTS set.
    ap = _CortexM7AP(swd_dap, halted=True, maskints=True)
    swd_dap.CortexM(ap).resume()
    assert not ap.halted
    assert not ap.maskints, "target left running with interrupts masked"


def test_resume_recovers_a_core_already_running_masked(swd_dap):
    ap = _CortexM7AP(swd_dap, halted=False, maskints=True)
    swd_dap.CortexM(ap).resume()
    assert not ap.halted and not ap.maskints


def test_plain_resume_is_a_single_write(swd_dap):
    ap = _CortexM7AP(swd_dap, halted=True, maskints=False)
    swd_dap.CortexM(ap).resume()
    assert ap.writes == [swd_dap.DBGKEY | swd_dap.C_DEBUGEN]
    assert not ap.halted


class _ResettingDP:
    """A debug port that NAKs every access for `down` reconnect attempts, as
    an i.MX RT105x does while a system reset holds its SW-DP."""

    def __init__(self, d, down):
        self.d = d
        self.down = down
        self.connects = 0
        self.ap_writes = []

    def connect(self):
        self.connects += 1
        if self.connects <= self.down:
            raise self.d.TransferError("read_dp 0x0: ack=7", 7)

    def write_ap(self, reg, value, apsel=0):
        self.ap_writes.append((reg, value))

    def read_ap(self, reg, apsel=0):
        return 0


@pytest.fixture
def clock(swd_dap, monkeypatch):
    now = [0]
    monkeypatch.setattr(swd_dap, "time", types.SimpleNamespace(
        ticks_ms=lambda: now[0], ticks_diff=lambda a, b: a - b,
        sleep_ms=lambda ms: now.__setitem__(0, now[0] + ms)))
    return now


def test_reconnect_waits_out_a_reset_and_rewrites_csw(swd_dap, clock):
    dp = _ResettingDP(swd_dap, down=3)
    ap = swd_dap.MEMAP(dp)
    ap._csw = swd_dap.CSW_WORD      # what was set before the reset
    ap.reconnect()
    assert dp.connects == 4
    # The AP came back with its reset CSW, so the cached value must not
    # suppress rewriting it.
    assert dp.ap_writes[0] == (swd_dap.AP_CSW, swd_dap.CSW_WORD)


def test_reconnect_gives_up_after_its_timeout(swd_dap, clock):
    ap = swd_dap.MEMAP(_ResettingDP(swd_dap, down=10 ** 6))
    with pytest.raises(swd_dap.TransferError):
        ap.reconnect(timeout_ms=50)
    assert clock[0] <= 60
