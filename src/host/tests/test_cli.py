"""Tests for pod.cli - invoke main() with synthetic argv and POD_CONFIG_DIR=tmp."""

import sys
import pytest

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


class TestStubs:
    def test_flash_prints_not_implemented(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "flash", "some-pod", "fw.bin"])
        ret = main()
        out, _ = capsys.readouterr()
        assert "not yet implemented" in out.lower() or "phase" in out.lower()
        assert ret == 1

    def test_reset_prints_not_implemented(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "reset", "some-pod"])
        ret = main()
        out, _ = capsys.readouterr()
        assert "not yet implemented" in out.lower() or "phase" in out.lower()
        assert ret == 1


class TestNoSubcommand:
    def test_no_subcommand_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod"])
        ret = main()
        assert ret in (1, None) or ret != 0
