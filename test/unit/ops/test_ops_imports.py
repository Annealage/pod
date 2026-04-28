"""WS-H unit tests: ops/* import on the Unix port and pick the
pure-MP fallback when the C shim is absent.

The C shims (ops_ota, ops_wdt, ops_log) are ESP-IDF-only and never
compile for the Unix port. These tests exercise the wrapper-level
contract:

  - The submodules import cleanly.
  - With no C shim available, each wrapper exposes the documented
    fallback behaviour.
  - Public API surface (function names, callability) stays
    identical to the on-target path.
"""

import sys

import pytest


def test_ops_submodules_import():
    """All four ops/* submodules must import on Unix without
    optional C shims."""
    from annealage_pod.ops import log, ota, time, wdt  # noqa: F401


def test_ops_ota_shim_absent_on_unix():
    """The C shim is never compiled into the Unix port; verify
    the wrapper sees None and the fallback gate engages."""
    from annealage_pod.ops import ota

    # The shim must be absent in this environment.
    assert ota._ops_ota_c is None, \
        "ops_ota C shim should never be present on the Unix port"

    # Without the shim and without experimental_pure_mp, update()
    # raises NotImplementedError (per ota.py docstring).
    with pytest.raises(NotImplementedError):
        ota.update("https://example.invalid/firmware.bin")


def test_ops_ota_mark_valid_returns_false_on_unix():
    """mark_valid() must return False (not raise) when neither
    the shim nor esp32.Partition is available."""
    from annealage_pod.ops import ota

    assert ota._esp32 is None, \
        "esp32 module should not be present on the Unix port"

    assert ota.mark_valid() is False
    # Alias stays in sync.
    assert ota.mark_app_valid is ota.mark_valid


def test_ops_wdt_shim_absent_on_unix():
    """The wdt wrapper must fall back to machine.WDT when the
    shim is absent. machine.WDT is also absent on Unix, so
    subscribe()/kick() return None/False without raising."""
    from annealage_pod.ops import wdt

    assert wdt._ops_wdt_c is None
    # machine.WDT is not on the Unix port either.
    assert wdt._WDT is None

    assert wdt.subscribe() is None
    assert wdt.kick() is False
    assert wdt.unsubscribe() is False
    assert wdt.is_subscribed() is False


def test_ops_log_shim_absent_on_unix():
    """The log wrapper must fall back to the os.dupterm path on
    Unix. start() with no port bound returns the listener (or
    None if socket import failed)."""
    from annealage_pod.ops import log

    assert log._ops_log_c is None

    # client_count() before start() must be 0.
    assert log.client_count() == 0


def test_ops_log_fallback_listener_lifecycle():
    """The pure-MP fallback path must bind, accept, stop. We
    bind to ephemeral port 0 and immediately stop without
    accepting; this is the contract used by tests."""
    from annealage_pod.ops import log

    listener = log.start(port=0)
    assert listener is not None, "fallback listener must bind on Unix"
    assert log.client_count() == 0
    log.stop()
    assert log._listener is None


def test_ops_time_uses_pure_mp():
    """time submodule is pure MP per WS-H scope. It must import
    and expose sync()/server() regardless of ntptime presence."""
    from annealage_pod.ops import time as ops_time

    # ntptime may or may not be on PYTHONPATH for Unix; either way
    # sync() and server() must be callable.
    assert callable(ops_time.sync)
    assert callable(ops_time.server)

    if ops_time._ntptime is None:
        # Without ntptime, sync returns False and server returns None.
        assert ops_time.sync() is False
        assert ops_time.server() is None


def test_ops_log_start_socket_alias():
    """start_socket alias kept for WS-E backward compatibility."""
    from annealage_pod.ops import log

    assert log.start_socket is log.start


def test_ops_ota_shim_path_with_fake_module(monkeypatch):
    """Inject a fake `ops_ota` C shim and verify the wrapper
    routes update()/mark_valid() through it."""
    from annealage_pod.ops import ota

    calls = {"update": [], "mark_app_valid": 0}

    class FakeShim:
        @staticmethod
        def update(url, cert_pem):
            calls["update"].append((url, cert_pem))
            return True

        @staticmethod
        def mark_app_valid():
            calls["mark_app_valid"] += 1
            return True

    monkeypatch.setattr(ota, "_ops_ota_c", FakeShim)

    assert ota.update("https://x.example/y.bin") is True
    assert calls["update"] == [("https://x.example/y.bin", None)]

    assert ota.update("https://x.example/y.bin", cert_pem="PEM") is True
    assert calls["update"][-1] == ("https://x.example/y.bin", "PEM")

    assert ota.mark_valid() is True
    assert calls["mark_app_valid"] == 1


def test_ops_wdt_shim_path_with_fake_module(monkeypatch):
    """Inject a fake `ops_wdt` C shim and verify the wrapper
    routes subscribe/kick/unsubscribe through it."""
    from annealage_pod.ops import wdt

    state = {"subscribed": False, "kicks": 0, "timeout": None}

    class FakeShim:
        @staticmethod
        def subscribe(timeout_ms=30000):
            state["subscribed"] = True
            state["timeout"] = timeout_ms
            return None

        @staticmethod
        def kick():
            state["kicks"] += 1
            return None

        @staticmethod
        def unsubscribe():
            state["subscribed"] = False
            return None

        @staticmethod
        def is_subscribed():
            return state["subscribed"]

    monkeypatch.setattr(wdt, "_ops_wdt_c", FakeShim)
    monkeypatch.setattr(wdt, "_subscribed_via_shim", False)

    assert wdt.subscribe(timeout_ms=12345) is FakeShim
    assert state["subscribed"] is True
    assert state["timeout"] == 12345

    assert wdt.kick() is True
    assert state["kicks"] == 1

    assert wdt.is_subscribed() is True
    assert wdt.unsubscribe() is True
    assert state["subscribed"] is False


def test_ops_log_shim_path_with_fake_module(monkeypatch):
    """Inject a fake `ops_log` C shim and verify the wrapper
    routes start/stop/client_count through it."""
    from annealage_pod.ops import log

    state = {"started": False, "port": None, "core": None}

    class FakeShim:
        @staticmethod
        def start(port=514, core=-1):
            state["started"] = True
            state["port"] = port
            state["core"] = core
            return None

        @staticmethod
        def stop():
            state["started"] = False
            return None

        @staticmethod
        def client_count():
            return 1 if state["started"] else 0

    monkeypatch.setattr(log, "_ops_log_c", FakeShim)

    assert log.start(port=4567, core=1) is FakeShim
    assert state == {"started": True, "port": 4567, "core": 1}
    assert log.client_count() == 1

    assert log.stop() is True
    assert state["started"] is False
    assert log.client_count() == 0
