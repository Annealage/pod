"""Host-side unit tests for the on-pod SWD register/memory helpers
(annealage_pod.debug.ops). ops imports machine-bound submodules (swd_dap pulls
in machine/rp2), so those are stubbed in sys.modules before import; dbgsrv stays
real (its REGSEL_MAX/MAX_DATA/FLASH_TOP and _read_mem/_write_mem are exercised).

These cover the validation/guard branches that live on the pod and were
otherwise only reachable on hardware: the regsel range check, the running-core
gate, the length cap, the bad-hex guard, the flash/protect refusal, and the
{ok: False} error shape on a transfer failure. The guard branches return before
_ensure(), so most need no fake target; the success paths monkeypatch _ensure.
"""

import os
import sys
import types

import pytest

# Make the on-pod package importable and stub its machine-bound submodules.
_MPY = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mpy"))
if _MPY not in sys.path:
    sys.path.insert(0, _MPY)

for _name in ("annealage_pod.debug.swd_dap", "annealage_pod.debug.flash_nrf52",
              "annealage_pod.debug.netutil", "annealage_pod._rp2_pinmap"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

import annealage_pod.debug.ops as ops          # noqa: E402
import annealage_pod.debug.dbgsrv as dbgsrv     # noqa: E402


class _FakeCM:
    def __init__(self, halted=True, reg=0xCAFEBABE, dhcsr=0x00020000):
        self._halted = halted
        self._reg = reg
        self._dhcsr = dhcsr
        self.written = {}

    def is_halted(self):
        return self._halted

    def halt(self):
        self._halted = True

    def resume(self):
        self._halted = False

    def read_dhcsr(self):
        return self._dhcsr

    def read_core_reg(self, regsel):
        return self._reg

    def write_core_reg(self, regsel, value):
        self.written[regsel] = value


def _patch_session(monkeypatch, cm=None, ap=None):
    """Make _ensure() return a fake (dp, ap, cm, flash) without real SWD."""
    cm = cm or _FakeCM()
    monkeypatch.setattr(ops, "_ensure", lambda clkdiv=8: (object(), ap, cm, object()))
    return cm


# ── guard branches (return before _ensure; no target needed) ──────────────


class TestGuards:
    def test_read_reg_out_of_range(self):
        r = ops.read_reg(dbgsrv.REGSEL_MAX + 1)
        assert r["ok"] is False and "out of range" in r["err"]
        assert ops.read_reg(-1)["ok"] is False

    def test_write_reg_out_of_range(self):
        assert ops.write_reg(99, 0)["ok"] is False

    def test_read_mem_length_cap(self):
        r = ops.read_mem(0x20000000, dbgsrv.MAX_DATA + 1)
        assert r["ok"] is False and "out of range" in r["err"]
        assert ops.read_mem(0x20000000, -1)["ok"] is False

    def test_write_mem_bad_hex(self):
        r = ops.write_mem(0x20000000, "zz")
        assert r["ok"] is False and "bad hex" in r["err"]

    def test_write_mem_backstop_refuses_code_region(self):
        # No protect -> the Cortex-M code-region backstop refuses addr < SRAM base.
        r = ops.write_mem(0x100, "deadbeef")
        assert r["ok"] is False and "code/flash region" in r["err"]

    def test_write_mem_protect_refuses_overlap(self):
        r = ops.write_mem(0x10000000, "deadbeef",
                          protect=[[0x10000000, 0x10200000]])
        assert r["ok"] is False and "write-protected" in r["err"]

    def test_write_mem_oversize(self):
        r = ops.write_mem(0x20000000, "00" * (dbgsrv.MAX_DATA + 1))
        assert r["ok"] is False and "exceeds" in r["err"]


# ── success / state paths (monkeypatch _ensure) ───────────────────────────


class TestRegisters:
    def test_read_reg_running_core(self, monkeypatch):
        _patch_session(monkeypatch, cm=_FakeCM(halted=False))
        r = ops.read_reg(15)
        assert r["ok"] is False and "running" in r["err"]

    def test_read_reg_halted(self, monkeypatch):
        _patch_session(monkeypatch, cm=_FakeCM(halted=True, reg=0x1234))
        r = ops.read_reg(15)
        assert r == {"ok": True, "regsel": 15, "value": 0x1234}

    def test_write_reg_masks_value(self, monkeypatch):
        cm = _patch_session(monkeypatch, cm=_FakeCM(halted=True))
        r = ops.write_reg(13, 0x1_0000_0001)            # masked to 32 bits
        assert r["ok"] is True and r["value"] == 1
        assert cm.written[13] == 1

    def test_halt_resume(self, monkeypatch):
        cm = _patch_session(monkeypatch, cm=_FakeCM(halted=False))
        assert ops.halt()["halted"] is True and cm.is_halted() is True
        assert ops.resume()["halted"] is False and cm.is_halted() is False

    def test_transfer_error_is_result_not_raise(self, monkeypatch):
        def _boom(clkdiv=8):
            raise RuntimeError("SWD TransferError")
        monkeypatch.setattr(ops, "_ensure", _boom)
        r = ops.halt()
        assert r["ok"] is False and "TransferError" in r["err"]


class TestMemory:
    def test_read_mem_success(self, monkeypatch):
        _patch_session(monkeypatch)
        monkeypatch.setattr(dbgsrv, "_read_mem", lambda ap, addr, n: b"\xde\xad\xbe\xef")
        r = ops.read_mem(0x20000000, 4)
        assert r["ok"] is True and r["hex"] == "deadbeef" and r["length"] == 4

    def test_write_mem_success_in_ram(self, monkeypatch):
        _patch_session(monkeypatch)
        seen = {}
        monkeypatch.setattr(dbgsrv, "_write_mem",
                            lambda ap, addr, data: seen.update(addr=addr, data=bytes(data)))
        r = ops.write_mem(0x20000000, "deadbeef")
        assert r["ok"] is True and r["length"] == 4
        assert seen["addr"] == 0x20000000 and seen["data"] == b"\xde\xad\xbe\xef"
