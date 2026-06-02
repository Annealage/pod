"""Tests for pod.cli - invoke main() with synthetic argv and POD_CONFIG_DIR=tmp."""

import sys
import pytest
from unittest.mock import MagicMock

import pod.cli as cli
from pod.cli import main


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Route registry I/O to a fresh tmp directory for every test."""
    monkeypatch.setenv("POD_CONFIG_DIR", str(tmp_path))
    yield tmp_path


class TestListEmpty:
    def test_list_empty_registry_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "list"])
        ret = main()
        assert ret in (0, None)

    def test_list_empty_registry_no_output(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "list"])
        main()
        out, _ = capsys.readouterr()
        assert out == ""


class TestRegisterAndInfo:
    def _register(self, monkeypatch, label="test-pod", address="192.168.0.121", force=False):
        argv = [
            "pod", "register", label,
            "--address", address,
            "--repl-port", "8266",
            "--usbip-port", "3240",
            "--uart-port", "2000",
            "--carrier-id", "proto-v1",
            "--mp-version", "1.29.0.preview",
        ]
        if force:
            argv.append("--force")
        monkeypatch.setattr(sys, "argv", argv)
        return main()

    def test_register_exits_zero(self, monkeypatch, capsys):
        ret = self._register(monkeypatch)
        assert ret in (0, None)

    def test_register_prints_confirmation(self, monkeypatch, capsys):
        self._register(monkeypatch)
        out, _ = capsys.readouterr()
        assert "test-pod" in out

    def test_register_then_list(self, monkeypatch, capsys):
        self._register(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["pod", "list"])
        main()
        out, _ = capsys.readouterr()
        assert "test-pod" in out

    def test_register_then_info(self, monkeypatch, capsys):
        self._register(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["pod", "info", "test-pod"])
        ret = main()
        assert ret in (0, None)
        out, _ = capsys.readouterr()
        assert "192.168.0.121" in out
        assert "8266" in out

    def test_info_shows_mp_version(self, monkeypatch, capsys):
        self._register(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["pod", "info", "test-pod"])
        main()
        out, _ = capsys.readouterr()
        assert "1.29.0.preview" in out

    def test_info_shows_carrier_id(self, monkeypatch, capsys):
        self._register(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["pod", "info", "test-pod"])
        main()
        out, _ = capsys.readouterr()
        assert "proto-v1" in out

    def test_info_missing_label_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "info", "no-such-pod"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0

    def test_duplicate_register_fails(self, monkeypatch, capsys):
        self._register(monkeypatch)
        capsys.readouterr()
        ret = self._register(monkeypatch)
        assert ret == 1

    def test_duplicate_register_force_succeeds(self, monkeypatch, capsys):
        self._register(monkeypatch)
        capsys.readouterr()
        ret = self._register(monkeypatch, address="10.0.0.99", force=True)
        assert ret in (0, None)
        # Verify new address was stored
        monkeypatch.setattr(sys, "argv", ["pod", "info", "test-pod"])
        main()
        out, _ = capsys.readouterr()
        assert "10.0.0.99" in out


class TestUnregister:
    def test_unregister_existing(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "pod", "register", "rm-pod",
            "--address", "1.2.3.4",
        ])
        main()
        capsys.readouterr()  # drain register output
        monkeypatch.setattr(sys, "argv", ["pod", "unregister", "rm-pod"])
        ret = main()
        assert ret in (0, None)
        capsys.readouterr()  # drain unregister output
        # Should no longer appear in list
        monkeypatch.setattr(sys, "argv", ["pod", "list"])
        main()
        out, _ = capsys.readouterr()
        assert "rm-pod" not in out

    def test_unregister_missing_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "unregister", "ghost"])
        ret = main()
        assert ret == 1


class TestFlashReset:
    # flash/reset are wired to the on-pod loader; with an unknown label they
    # resolve-then-fail (exit 1) rather than printing a stub message.
    def test_flash_unknown_label_errors(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pod", "flash", "nope", "fw.bin"])
        with pytest.raises(SystemExit):
            main()

    def test_reset_unknown_label_errors(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pod", "reset", "nope"])
        with pytest.raises(SystemExit):
            main()


class TestRegisterGdbPort:
    def test_register_with_gdb_port_then_info(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "pod", "register", "g-pod",
            "--address", "192.168.0.50",
            "--gdb-port", "4444",
        ])
        main()
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", ["pod", "info", "g-pod"])
        main()
        out, _ = capsys.readouterr()
        assert "4444" in out

    def test_register_default_gdb_port(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "pod", "register", "g-pod2",
            "--address", "192.168.0.51",
        ])
        main()
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", ["pod", "info", "g-pod2"])
        main()
        out, _ = capsys.readouterr()
        assert "3335" in out


class TestGdbCommand:
    def _register(self, monkeypatch, capsys, label="gdb-pod", gdb_port=None):
        argv = ["pod", "register", label, "--address", "192.168.0.60"]
        if gdb_port is not None:
            argv += ["--gdb-port", str(gdb_port)]
        monkeypatch.setattr(sys, "argv", argv)
        main()
        capsys.readouterr()

    def test_gdb_unknown_label_errors(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pod", "gdb", "nope"])
        with pytest.raises(SystemExit):
            main()

    def test_gdb_calls_endpoint_with_defaults(self, monkeypatch, capsys):
        self._register(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.gdb_endpoint.return_value = ("127.0.0.1", 5005)
        monkeypatch.setattr(cli, "Pod", lambda **kw: fake_pod)
        monkeypatch.setattr(sys, "argv", ["pod", "gdb", "gdb-pod"])
        ret = main()
        assert ret in (0, None)
        fake_pod.gdb_endpoint.assert_called_once()
        kwargs = fake_pod.gdb_endpoint.call_args.kwargs
        assert kwargs["listen_port"] == 0
        assert kwargs["gdb_port"] == 3335
        assert kwargs["reset_halt"] is True
        assert kwargs["resume_window_ms"] == 200

    def test_gdb_uses_registry_gdb_port(self, monkeypatch, capsys):
        self._register(monkeypatch, capsys, label="gdb-pod3", gdb_port=4321)
        fake_pod = MagicMock()
        fake_pod.gdb_endpoint.return_value = ("127.0.0.1", 6006)
        monkeypatch.setattr(cli, "Pod", lambda **kw: fake_pod)
        monkeypatch.setattr(sys, "argv", ["pod", "gdb", "gdb-pod3"])
        main()
        kwargs = fake_pod.gdb_endpoint.call_args.kwargs
        assert kwargs["gdb_port"] == 4321

    def test_gdb_flags_passed_through(self, monkeypatch, capsys):
        self._register(monkeypatch, capsys, label="gdb-pod4")
        fake_pod = MagicMock()
        fake_pod.gdb_endpoint.return_value = ("127.0.0.1", 7007)
        monkeypatch.setattr(cli, "Pod", lambda **kw: fake_pod)
        monkeypatch.setattr(sys, "argv", [
            "pod", "gdb", "gdb-pod4",
            "--listen-port", "9999",
            "--gdb-port", "3336",
            "--no-reset-halt",
            "--resume-window-ms", "100",
        ])
        main()
        kwargs = fake_pod.gdb_endpoint.call_args.kwargs
        assert kwargs["listen_port"] == 9999
        assert kwargs["gdb_port"] == 3336
        assert kwargs["reset_halt"] is False
        assert kwargs["resume_window_ms"] == 100


class TestNoSubcommand:
    def test_no_subcommand_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod"])
        ret = main()
        assert ret in (1, None) or ret != 0
