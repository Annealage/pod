"""Tests for the pod-side holder record and its control listener.

Both are pure-MicroPython modules under src/mpy. They are imported here directly
because their logic (record keeping, request parsing) is plain Python; only the
socket half needs the pod, and that is covered by the hardware gate rather than
by these.
"""

import importlib.util
import pathlib
import sys
import types

import pytest

MPY = pathlib.Path(__file__).resolve().parents[3] / "src" / "mpy"


def _load(name, relpath, package_stub=None):
    """Import a src/mpy module by path, standing in for the on-pod package."""
    spec = importlib.util.spec_from_file_location(name, MPY / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    if package_stub is not None:
        sys.modules[package_stub] = types.ModuleType(package_stub)
        sys.modules[package_stub].__path__ = [str(MPY / "annealage_pod")]
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def holders():
    mod = _load("annealage_pod.holders", "annealage_pod/holders.py",
                package_stub="annealage_pod")
    # CPython's time has no ticks_ms/ticks_diff; the module only needs a
    # monotonic pair, so supply the MicroPython names.
    import time as _t
    mod.time = types.SimpleNamespace(
        ticks_ms=lambda: int(_t.monotonic() * 1000),
        ticks_diff=lambda a, b: a - b)
    mod.clear()
    yield mod
    mod.clear()


class TestHolderRecord:
    def test_nobody_holds_anything_to_begin_with(self, holders):
        assert holders.who() == {}
        assert holders.who("repl") == {}

    def test_note_then_who_names_the_caller(self, holders):
        holders.note("repl", "agent-a:corona@carbon", "socket REPL")
        rec = holders.who("repl")["repl"]
        assert rec["caller"] == "agent-a:corona@carbon"
        assert rec["detail"] == "socket REPL"
        assert rec["since_s"] >= 0

    def test_drop_clears_it_and_reports_whether_it_was_held(self, holders):
        holders.note("usbip", "agent-b")
        assert holders.drop("usbip") is True
        assert holders.who("usbip") == {}
        assert holders.drop("usbip") is False    # already gone

    def test_a_second_note_replaces_rather_than_stacking(self, holders):
        """Holding is a fact observed, not a queue: the last writer is the
        holder, because whoever is using it now is who a refusal must name."""
        holders.note("repl", "agent-a")
        holders.note("repl", "agent-b")
        assert holders.who("repl")["repl"]["caller"] == "agent-b"

    def test_resources_are_independent(self, holders):
        holders.note("repl", "agent-a")
        holders.note("swd", "agent-b")
        holders.drop("repl")
        assert "repl" not in holders.who()
        assert holders.who()["swd"]["caller"] == "agent-b"

    def test_who_with_no_argument_lists_everything_held(self, holders):
        holders.note("repl", "a")
        holders.note("swd", "b")
        assert set(holders.who()) >= {"repl", "swd"}

    def test_there_is_no_expiry(self, holders):
        """A holder record has no TTL by design. Liveness comes from the
        resource, so nothing here may time out and hand the bench to someone
        while the previous holder is still using it."""
        holders.note("repl", "agent-a")
        now = holders.time.ticks_ms()
        holders.time.ticks_ms = lambda: now + 86_400_000      # a day later
        rec = holders.who("repl")["repl"]
        assert rec["caller"] == "agent-a"
        assert rec["since_s"] >= 86_400


class TestEvict:
    """The phase-6 anti-bump gate's audit trail: a console line, not a second
    holder record, since the record overwrite is what tells the evicted
    caller - this is only for whoever is watching the pod's own console."""

    def test_prints_who_bumped_whom_off_what(self, holders, capsys):
        holders.evict("usbip", "agent-a:corona@carbon", "agent-b:corona@laptop")
        out = capsys.readouterr().out
        assert "agent-a:corona@carbon" in out
        assert "agent-b:corona@laptop" in out
        assert "usbip" in out

    def test_detail_is_optional(self, holders, capsys):
        holders.evict("usbip", "a", "b")
        out = capsys.readouterr().out
        assert out.strip()

    def test_detail_appears_when_given(self, holders, capsys):
        holders.evict("peripherals", "a", "b", detail="spi_target, i2c_target")
        out = capsys.readouterr().out
        assert "spi_target, i2c_target" in out


@pytest.fixture
def control(holders):
    mod = _load("annealage_pod.control_test", "annealage_pod/control.py",
                package_stub="annealage_pod")
    mod.holders = holders
    return mod


class TestControlProtocol:
    def test_ping(self, control):
        assert control.handle("ping") == {"ok": True, "pong": True}

    def test_who_reports_the_record(self, control, holders):
        holders.note("repl", "agent-a", "socket REPL")
        body = control.handle("who")
        assert body["ok"] is True
        assert body["holders"]["repl"]["caller"] == "agent-a"

    def test_who_can_be_filtered(self, control, holders):
        holders.note("repl", "agent-a")
        holders.note("swd", "agent-b")
        assert set(control.handle("who repl")["holders"]) == {"repl"}

    def test_an_unheld_resource_is_empty_not_an_error(self, control):
        body = control.handle("who usbip")
        assert body["ok"] is True and body["holders"] == {}

    def test_an_empty_request_is_refused_clearly(self, control):
        for blank in ("", "   ", "\n"):
            body = control.handle(blank)
            assert body["ok"] is False and "empty" in body["err"]

    def test_an_unknown_verb_says_what_is_accepted(self, control):
        body = control.handle("evict repl")
        assert body["ok"] is False
        assert "who" in body["err"] and "ping" in body["err"]

    def test_the_listener_exposes_no_way_to_mutate_a_holder(self, control,
                                                            holders):
        """Read-only on purpose: anything that changes pod state belongs on the
        REPL, where a human can see it happen."""
        holders.note("repl", "agent-a")
        for attempt in ("drop repl", "note repl me", "clear", "release repl"):
            assert control.handle(attempt)["ok"] is False
        assert holders.who("repl")["repl"]["caller"] == "agent-a"

    def test_case_and_whitespace_are_tolerated(self, control):
        assert control.handle("  PING  ") == {"ok": True, "pong": True}
