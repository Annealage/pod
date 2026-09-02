# Unit tests for annealage_pod.dut.reset() across the four configured paths.

import pytest

from annealage_pod.esp32 import dut, power, relays


def test_reset_unknown_mode_raises():
    with pytest.raises(ValueError):
        dut.reset(mode="floppy")


def test_reset_swd_logs_when_dapprobe_missing(capsys):
    # On the Unix port the dapprobe C module is not present, so the
    # SWD path should log a TODO and return False rather than raise.
    rc = dut.reset(mode="swd")
    out = capsys.readouterr().out
    assert rc is False
    assert "dapprobe" in out


def test_reset_nrst_returns_true_on_unix():
    assert dut.reset(mode="nrst") is True


def test_reset_power_cycles_vtarget():
    before = power.vtarget.cycle_count()
    rc = dut.reset(mode="power", off_ms=1)
    after = power.vtarget.cycle_count()
    assert rc is True
    assert after == before + 1


def test_reset_relay_requires_relay_arg():
    with pytest.raises(ValueError):
        dut.reset(mode="relay")


def test_reset_relay_invalid_number():
    with pytest.raises(ValueError):
        dut.reset(mode="relay", relay=99)


def test_reset_relay_pulses_relay():
    bank = relays.relays
    bank.batch(open=bank.numbers())
    rc = dut.reset(mode="relay", relay=2, pulse_ms=1)
    assert rc is True
    # After a single 1 ms pulse the relay returns to off.
    assert bank.get(2) is False
