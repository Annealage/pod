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

for _name in ("annealage_pod.debug.swd_dap",
              "annealage_pod.debug.netutil", "annealage_pod._rp2_pinmap"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

# flm imports the DHCSR bit constants from swd_dap, so the stub carries them
# (values mirror the real swd_dap); test_flm_pod shares this stub, and setting
# them here as well keeps the two files independent of import order.
_swd_dap_stub = sys.modules["annealage_pod.debug.swd_dap"]
for _const, _val in (("DHCSR", 0xE000EDF0), ("DBGKEY", 0xA05F << 16),
                     ("C_DEBUGEN", 1 << 0), ("C_HALT", 1 << 1),
                     ("C_MASKINTS", 1 << 3), ("S_HALT", 1 << 17)):
    setattr(_swd_dap_stub, _const, _val)

# ops imports swd_pio directly for DEFAULT_CLKDIV (used in its entry-point
# default args, evaluated at import), so the stub must carry that attribute; the
# real swd_pio pulls in rp2. Value mirrors swd_pio.DEFAULT_CLKDIV.
_swd_pio_stub = sys.modules.setdefault(
    "annealage_pod.debug.swd_pio", types.ModuleType("annealage_pod.debug.swd_pio"))
if not hasattr(_swd_pio_stub, "DEFAULT_CLKDIV"):
    _swd_pio_stub.DEFAULT_CLKDIV = 16

import annealage_pod.debug.ops as ops          # noqa: E402
import annealage_pod.debug.dbgsrv as dbgsrv     # noqa: E402


@pytest.fixture
def fake_clock(monkeypatch):
    """holders.py calls time.ticks_ms()/ticks_diff(), MicroPython-only names
    CPython's time module lacks. Stands in a controllable monotonic pair so
    the SWD guard tests below can advance "now" deterministically, and clears
    both holders state and the guard's own sticky-window record before/after
    so a guard claim in one test cannot leak into the next.
    """
    now = types.SimpleNamespace(ms=0)
    monkeypatch.setattr(
        ops.holders, "time",
        types.SimpleNamespace(ticks_ms=lambda: now.ms,
                              ticks_diff=lambda a, b: a - b))
    ops.holders.clear()
    ops._last = None
    yield now
    ops.holders.clear()
    ops._last = None


class _FakeCM:
    def __init__(self, halted=True, reg=0xCAFEBABE, dhcsr=0x00020000):
        self._halted = halted
        self._reg = reg
        self._dhcsr = dhcsr
        self.written = {}
        self.sysreset_called = False

    def is_halted(self):
        return self._halted

    def halt(self):
        self._halted = True

    def resume(self):
        self._halted = False

    def sysreset(self):
        self.sysreset_called = True
        self._halted = False

    def read_dhcsr(self):
        return self._dhcsr

    def read_core_reg(self, regsel):
        return self._reg

    def write_core_reg(self, regsel, value):
        self.written[regsel] = value


def _patch_session(monkeypatch, cm=None, ap=None):
    """Make _ensure() return a fake (dp, ap, cm) without real SWD."""
    cm = cm or _FakeCM()
    monkeypatch.setattr(ops, "_ensure", lambda clkdiv=8: (object(), ap, cm))
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


class _FakeSWDPio:
    def __init__(self, clkdiv):
        self.clkdiv = clkdiv
        self.released = False

    def release(self):
        self.released = True


class _FakeDP:
    """Fake DebugPort recording its build clkdiv + connect count."""
    built = []            # every instance, in build order

    def __init__(self, swdio=None, swclk=None, sm_id=None, clkdiv=None):
        self.swd = _FakeSWDPio(clkdiv)
        self.connects = 0
        _FakeDP.built.append(self)

    def connect(self):
        self.connects += 1
        return 0x2BA01477


class TestEnsureClkdiv:
    """ops._ensure must honour a changed clkdiv on a warm stack by rebuilding
    the SWD transport (the #2 caching bug), and reuse it when unchanged."""

    def _wire_fakes(self, monkeypatch):
        import annealage_pod.debug.pio_arbiter as pio_arbiter
        _FakeDP.built = []
        # Fresh, torn-down session each time.
        for g in ("_dp", "_ap", "_cm", "_fpb", "_dwt", "_flm"):
            monkeypatch.setattr(ops, g, None)
        monkeypatch.setattr(ops._rp2_pinmap, "SWD_SWDIO", 14, raising=False)
        monkeypatch.setattr(ops._rp2_pinmap, "SWD_SWCLK", 15, raising=False)
        monkeypatch.setattr(ops.swd_dap, "DebugPort", _FakeDP, raising=False)
        monkeypatch.setattr(ops.swd_dap, "MEMAP", lambda dp: object(), raising=False)
        monkeypatch.setattr(ops.swd_dap, "CortexM", lambda ap: object(), raising=False)
        monkeypatch.setattr(ops.swd_dap, "FPB", lambda ap: object(), raising=False)
        monkeypatch.setattr(ops.swd_dap, "DWT", lambda ap: object(), raising=False)
        monkeypatch.setattr(pio_arbiter, "claim", lambda *a, **k: None)

    def test_same_clkdiv_reuses_session(self, monkeypatch):
        self._wire_fakes(monkeypatch)
        ops._ensure(16)
        ops._ensure(16)
        assert len(_FakeDP.built) == 1          # not rebuilt
        assert _FakeDP.built[0].connects == 2   # reconnected each call

    def test_changed_clkdiv_rebuilds_transport(self, monkeypatch):
        self._wire_fakes(monkeypatch)
        ops._ensure(16)
        first = _FakeDP.built[0]
        ops._ensure(32)
        assert len(_FakeDP.built) == 2          # rebuilt at the new clock
        assert first.swd.released is True        # old transport released
        assert _FakeDP.built[1].swd.clkdiv == 32
        assert ops._dp.swd.clkdiv == 32

    def test_default_clkdiv_is_spec_compliant(self, monkeypatch):
        self._wire_fakes(monkeypatch)
        ops._ensure()                            # no arg -> module default
        assert _FakeDP.built[0].swd.clkdiv == ops.swd_pio.DEFAULT_CLKDIV


class TestFlmAlgoInstall:
    """The host-supplied CMSIS algorithm seam.

    The pod carries no flash algorithms; the host extracts one from the target's
    CMSIS pack and installs it. These cover the install/replace bookkeeping and
    the refusal when nothing is installed - the failure a user hits flashing or
    erasing before the host has resolved a pack.
    """

    _ALGO = {
        "name": "nRF52840xxAA",
        "instructions": b"\x00\xbe\x00\xbe",
        "load_address": 0x20000000,
        "static_base": 0x20000400,
        "begin_stack": 0x20001000,
        "begin_data": 0x20002000,
        "pc_init": 0x20000021,
        "pc_erase_sector": 0x20000061,
        "pc_program_page": 0x20000081,
        "flash_base": 0x0,
        "flash_size": 0x100000,
        "page_size": 0x1000,
    }

    @pytest.fixture(autouse=True)
    def _clean_algo(self):
        saved = (ops._flm_algo, ops._flm)
        ops._flm_algo, ops._flm = None, None
        yield
        ops._flm_algo, ops._flm = saved

    def test_no_algo_installed_reports_not_installed(self):
        assert ops.flm_algo_info() == {"installed": False}

    def test_install_summarises_without_echoing_the_blob(self):
        info = ops.set_flm_algo(dict(self._ALGO))
        assert info["installed"] is True
        assert info["name"] == "nRF52840xxAA"
        assert info["blob_bytes"] == 4
        assert info["page_size"] == 0x1000
        assert info["erase_all"] is False        # no pc_eraseAll in this algo
        assert "instructions" not in info

    def test_install_reports_erase_all_when_algo_has_erasechip(self):
        algo = dict(self._ALGO, pc_eraseAll=0x200000A1)
        assert ops.set_flm_algo(algo)["erase_all"] is True

    def test_reinstall_drops_the_flasher_cached_over_the_old_algo(self):
        ops.set_flm_algo(dict(self._ALGO))
        ops._flm = object()                      # stand in for a built flasher
        ops.set_flm_algo(dict(self._ALGO, name="other"))
        assert ops._flm is None
        assert ops.flm_algo_info()["name"] == "other"

    def test_require_flm_without_an_algo_refuses(self):
        with pytest.raises(ValueError, match="no CMSIS flash algorithm installed"):
            ops._require_flm()


class TestFlmRestore:
    """Gap 1 of cmsis-flash-completion.md: an FLM operation ends with the core
    parked on the algorithm's own BKPT trampoline, and a plain resume cannot
    move it past that (the halt is the BKPT instruction, not a debug C_HALT
    request) - only a system reset restarts the DUT's own firmware."""

    @pytest.fixture(autouse=True)
    def _clean_algo(self):
        saved = (ops._flm_algo, ops._flm)
        yield
        ops._flm_algo, ops._flm = saved

    def test_restore_resets_before_resuming(self):
        cm = _FakeCM()
        ops._flm_restore(cm)
        assert cm.sysreset_called is True
        assert cm.is_halted() is False

    def test_erase_all_flm_restore_failure_is_reported_not_swallowed(
            self, monkeypatch):
        # The bug this closes: erase_all's finally used to swallow a restore
        # failure with a bare `except Exception: pass`, so a DUT left parked on
        # the algorithm's breakpoint looked like a clean {"ok": True} erase.
        # ops.py's own `import time` is CPython's real module here, which lacks
        # ticks_ms/ticks_diff (MicroPython-only names); stub them for the
        # elapsed-time bookkeeping erase_all does around the operation.
        monkeypatch.setattr(ops, "time", types.SimpleNamespace(
            ticks_ms=lambda: 0, ticks_diff=lambda a, b: 0))
        cm = _FakeCM()

        def _boom():
            raise RuntimeError("sysreset failed")
        cm.sysreset = _boom
        _patch_session(monkeypatch, cm=cm)
        fake_flasher = types.SimpleNamespace(
            reload=lambda: None, erase_all=lambda: None)
        monkeypatch.setattr(ops, "_require_flm", lambda: fake_flasher)

        result = ops.erase_all()

        assert result["ok"] is False
        assert "sysreset failed" in result["err"]

    def test_flash_file_flm_loader_resets_before_resuming(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(ops, "time", types.SimpleNamespace(
            ticks_ms=lambda: 0, ticks_diff=lambda a, b: 0))
        cm = _patch_session(monkeypatch, cm=_FakeCM())
        img = tmp_path / "img.bin"
        img.write_bytes(b"\x00" * 16)
        fake_flasher = types.SimpleNamespace()
        monkeypatch.setattr(ops, "_require_flm", lambda: fake_flasher)
        monkeypatch.setattr(
            ops, "_flm_program_file", lambda fl, addr, f, verify: len(f.read()))

        result = ops.flash_file(0x1000, str(img))

        assert result["ok"] is True
        assert cm.sysreset_called is True

    def test_erase_all_does_not_restore_when_no_algorithm_is_installed(
            self, monkeypatch):
        # _require_flm raising (no algorithm installed) means nothing has
        # touched the DUT yet - the finally must not sysreset a DUT that was
        # never halted. Reproduces a real bug: the equivalent lookup used to
        # be called inside the try, so this refusal still triggered a restore.
        monkeypatch.setattr(ops, "time", types.SimpleNamespace(
            ticks_ms=lambda: 0, ticks_diff=lambda a, b: 0))
        cm = _patch_session(monkeypatch, cm=_FakeCM())
        ops._flm_algo = None

        with pytest.raises(ValueError, match="no CMSIS flash algorithm installed"):
            ops.erase_all()

        assert cm.sysreset_called is False
        assert cm.is_halted() is True     # neither sysreset nor resume ran

    def test_erase_all_erase_and_restore_both_failing_reports_the_erase_error(
            self, monkeypatch):
        # The finally's "if err is None" guard must keep the erase's own
        # error rather than let a subsequent restore failure overwrite it -
        # the caller needs to know the erase failed, not just that the
        # restore afterward also failed.
        monkeypatch.setattr(ops, "time", types.SimpleNamespace(
            ticks_ms=lambda: 0, ticks_diff=lambda a, b: 0))
        cm = _FakeCM()

        def _boom():
            raise RuntimeError("sysreset failed too")
        cm.sysreset = _boom
        _patch_session(monkeypatch, cm=cm)

        def _erase_boom():
            raise RuntimeError("erase failed")
        fake_flasher = types.SimpleNamespace(
            reload=lambda: None, erase_all=_erase_boom)
        monkeypatch.setattr(ops, "_require_flm", lambda: fake_flasher)

        result = ops.erase_all()

        assert result["ok"] is False
        assert "erase failed" in result["err"]
        assert "sysreset failed too" not in result["err"]

    def test_flash_file_restores_even_when_programming_raises(
            self, monkeypatch, tmp_path):
        # The bug this closes: _flm_restore used to sit after the try/finally
        # that only closed the file, so a raising program left the DUT parked
        # on the algorithm's own breakpoint with no restore attempt at all.
        monkeypatch.setattr(ops, "time", types.SimpleNamespace(
            ticks_ms=lambda: 0, ticks_diff=lambda a, b: 0))
        cm = _patch_session(monkeypatch, cm=_FakeCM())
        img = tmp_path / "img.bin"
        img.write_bytes(b"\x00" * 16)
        fake_flasher = types.SimpleNamespace()
        monkeypatch.setattr(ops, "_require_flm", lambda: fake_flasher)

        def _boom(fl, addr, f, verify):
            raise RuntimeError("program failed")
        monkeypatch.setattr(ops, "_flm_program_file", _boom)

        with pytest.raises(RuntimeError, match="program failed"):
            ops.flash_file(0x1000, str(img))

        assert cm.sysreset_called is True

    def test_flash_stream_restore_failure_is_reported_not_swallowed(
            self, monkeypatch):
        # Mirrors erase_all's equivalent test: flash_stream's restore call was
        # unguarded, so a restore failure would replace the {"ok": ..., "err":
        # ...} result with a raw exception instead of surfacing in it.
        # netutil is an empty stub module for these tests (see the top of this
        # file) - fake its accept/recv_into/send_all rather than driving real
        # sockets, so a bind to an ephemeral port is the only real I/O needed.
        cm = _FakeCM()

        def _boom():
            raise RuntimeError("sysreset failed")
        cm.sysreset = _boom
        _patch_session(monkeypatch, cm=cm)
        fake_flasher = types.SimpleNamespace(
            page_size=64, program=lambda addr, data, erase, verify: None)
        monkeypatch.setattr(ops, "_require_flm", lambda: fake_flasher)
        monkeypatch.setattr(ops, "_flm_begin", lambda fl: None)
        monkeypatch.setattr(ops, "_flm_erase_range", lambda fl, a, n: None)
        monkeypatch.setattr(
            ops.netutil, "accept",
            lambda srv, timeout: (types.SimpleNamespace(close=lambda: None), None),
            raising=False)
        # A full read (== the requested length), so the transfer itself
        # succeeds and err is still None when the finally's restore runs.
        monkeypatch.setattr(ops.netutil, "recv_into", lambda cl, buf: len(buf),
                            raising=False)
        monkeypatch.setattr(ops.netutil, "send_all", lambda *a, **k: None,
                            raising=False)

        result = ops.flash_stream(0x1000, 4, port=0)

        assert "sysreset failed" in result["err"]


class TestSwdGuard:
    """The phase-7 re-entrancy guard (conflict-legibility.md item 5): a plain
    mutex on holders "swd" for genuinely concurrent entry, plus a short sticky
    window (ops.STICKY_S) that keeps refusing a DIFFERENT caller for a few
    seconds after the previous caller's last guarded call - closing the gap a
    one-shot pod_exec sequence opens between calls, where each call releases
    the REPL slot and would otherwise let a second caller's sequence interleave
    before the first sequence's next call runs.
    """

    def test_no_caller_is_never_gated(self, fake_clock):
        # A human at the REPL, or ops.* called directly with no client
        # wrapper: deliberate god-mode: never gated (Non-goals).
        ops._guard_enter(None, "reset")
        ops._guard_enter(None, "reset")           # a second one still no-ops
        assert ops.holders.who("swd") == {}

    def test_first_caller_claims_and_releases_on_exit(self, fake_clock):
        ops._guard_enter("agent-a", "reset")
        assert ops.holders.who("swd")["swd"]["caller"] == "agent-a"
        ops._guard_exit("agent-a")
        assert ops.holders.who("swd") == {}

    def test_concurrent_entry_by_a_different_caller_is_refused(self, fake_clock):
        # Held right now, never exited: the plain-mutex half of the guard, not
        # the sticky one - refused with no regard for elapsed time.
        ops._guard_enter("agent-a", "reset")
        with pytest.raises(ops.SwdBusy, match="agent-a"):
            ops._guard_enter("agent-b", "reset")

    def test_a_different_caller_right_after_exit_is_still_refused(self, fake_clock):
        ops._guard_enter("agent-a", "reset")
        ops._guard_exit("agent-a")
        with pytest.raises(ops.SwdBusy, match="agent-a"):
            ops._guard_enter("agent-b", "flash_file")

    def test_the_same_caller_reconnecting_is_refreshed_not_refused(self, fake_clock):
        ops._guard_enter("agent-a", "reset")
        ops._guard_exit("agent-a")
        fake_clock.ms += 1000                     # next call of the same sequence
        ops._guard_enter("agent-a", "flash_file")
        assert ops.holders.who("swd")["swd"]["caller"] == "agent-a"

    def test_a_different_caller_is_admitted_once_the_sticky_window_elapses(self, fake_clock):
        ops._guard_enter("agent-a", "reset")
        ops._guard_exit("agent-a")
        fake_clock.ms += (ops.STICKY_S + 1) * 1000
        ops._guard_enter("agent-b", "reset")      # does not raise
        assert ops.holders.who("swd")["swd"]["caller"] == "agent-b"

    def test_guard_exit_releases_the_mutex_even_when_the_call_raises(self, fake_clock):
        @ops._guarded("dummy")
        def _op(caller=None):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            _op(caller="agent-a")
        assert ops.holders.who("swd") == {}       # mutex released
        # but the sticky window still remembers agent-a was just here
        with pytest.raises(ops.SwdBusy, match="agent-a"):
            ops._guard_enter("agent-b", "reset")

    def test_a_guarded_ops_function_releases_on_its_normal_exit_path(
            self, monkeypatch, fake_clock):
        _patch_session(monkeypatch, cm=_FakeCM(halted=False))
        r = ops.halt(caller="agent-a")
        assert r["ok"] is True
        assert ops.holders.who("swd") == {}

    def test_a_guarded_ops_function_raises_swdbusy_for_a_different_caller(
            self, monkeypatch, fake_clock):
        _patch_session(monkeypatch, cm=_FakeCM(halted=False))
        ops.halt(caller="agent-a")
        with pytest.raises(ops.SwdBusy, match="agent-a"):
            ops.halt(caller="agent-b")

    def test_a_guarded_ops_function_still_works_with_no_caller_at_all(
            self, monkeypatch, fake_clock):
        # Existing callers (direct REPL use, and every test above this class)
        # never pass caller=; the decorator must not change that contract.
        _patch_session(monkeypatch, cm=_FakeCM(halted=False))
        assert ops.halt()["ok"] is True
