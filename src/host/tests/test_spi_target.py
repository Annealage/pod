"""Tests for SPI target: Pod.spi_target/spi_target_status, CLI, and MCP handlers."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pod.client import Pod
import pod.cli as cli
import pod.mcp_server as mcp_server


ADDRESS = "192.168.0.121"
PORT = 8266
CONNECT_TARGET = f"socket://{ADDRESS}:{PORT}"


@pytest.fixture
def fake_runner():
    """A mock subprocess.run replacement that returns a configurable result."""
    mock = MagicMock()
    mock.return_value = MagicMock(stdout="ok\n", returncode=0)
    return mock


@pytest.fixture
def pod_fake(fake_runner):
    """Pod instance with injected fake runner."""
    return Pod(address=ADDRESS, repl_port=PORT, runner=fake_runner)


class TestSpiTargetClient:
    """Test Pod.spi_target and Pod.spi_target_status code generation."""

    @staticmethod
    def _code(fake_runner):
        """Extract the generated on-pod code from the runner call args."""
        # exec argv is [ampremote, connect, target, exec, <code>]
        return fake_runner.call_args[0][0][4]

    def test_spi_target_basic_code(self, pod_fake, fake_runner):
        """spi_target generates the correct on-pod code with default params."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'name': 'spi_target', 'mode': 0, 'bits': 8, "
                   "'miso': 16, 'mosi': 19, 'sck': 18, 'cs': 17, 'size': 1024}\n",
            returncode=0)
        result = pod_fake.spi_target()
        code = self._code(fake_runner)
        assert "annealage_pod.peripherals" in code
        assert "p.spi_target(" in code
        assert "mode=0" in code
        assert "bits=8" in code
        assert "miso=16" in code
        assert "mosi=19" in code
        assert "sck=18" in code
        assert "cs=17" in code
        assert "size=1024" in code
        assert result["ok"] is True
        assert result["mode"] == 0

    def test_spi_target_custom_params(self, pod_fake, fake_runner):
        """spi_target with custom pins and size passes them through."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'name': 'test_spi', 'miso': 12, 'mosi': 13, "
                   "'sck': 14, 'cs': 15, 'size': 512}\n",
            returncode=0)
        result = pod_fake.spi_target(miso=12, mosi=13, sck=14, cs=15,
                                      size=512, name="test_spi")
        code = self._code(fake_runner)
        assert "miso=12" in code
        assert "mosi=13" in code
        assert "sck=14" in code
        assert "cs=15" in code
        assert "size=512" in code
        assert "name='test_spi'" in code
        assert result["name"] == "test_spi"

    def test_spi_target_status_basic(self, pod_fake, fake_runner):
        """spi_target_status generates the correct on-pod code."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'name': 'spi_target', 'mode': 0, 'bits': 8, "
                   "'bytes_rx': 42, 'transfers_total': 3, 'last_cs_len': 8, "
                   "'size': 1024, 'captured': [0, 1, 2, 3, 4, 5, 6, 7]}\n",
            returncode=0)
        result = pod_fake.spi_target_status()
        code = self._code(fake_runner)
        assert "annealage_pod.peripherals" in code
        assert "p.spi_target_status(" in code
        assert "name='spi_target'" in code
        assert result["ok"] is True
        assert result["bytes_rx"] == 42
        assert result["transfers_total"] == 3
        assert result["last_cs_len"] == 8
        assert result["captured"] == [0, 1, 2, 3, 4, 5, 6, 7]

    def test_spi_target_status_custom_name(self, pod_fake, fake_runner):
        """spi_target_status with custom instance name."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'name': 'spi_alt', 'bytes_rx': 100}\n",
            returncode=0)
        result = pod_fake.spi_target_status(name="spi_alt")
        code = self._code(fake_runner)
        assert "name='spi_alt'" in code
        assert result["name"] == "spi_alt"

    def test_spi_target_returns_dict(self, pod_fake, fake_runner):
        """spi_target parses the returned dict from on-pod output."""
        fake_runner.return_value = MagicMock(
            stdout="some noise\n{'ok': True, 'size': 1024}\nmore noise\n",
            returncode=0)
        result = pod_fake.spi_target()
        assert isinstance(result, dict)
        assert result["ok"] is True
        assert result["size"] == 1024


class TestSpiTargetCli:
    """Test the CLI subparsers and command dispatch for SPI target."""

    @pytest.fixture(autouse=True)
    def isolated_registry(self, tmp_path, monkeypatch):
        """Route registry I/O to a tmp directory for each test."""
        monkeypatch.setenv("POD_CONFIG_DIR", str(tmp_path))
        yield tmp_path

    def _register_test_pod(self, monkeypatch, capsys):
        """Helper to register a test pod in the isolated registry."""
        monkeypatch.setattr(sys, "argv", [
            "pod", "register", "test-label",
            "--address", "192.168.0.121",
            "--repl-port", "8266",
            "--no-probe",
        ])
        cli.main()
        capsys.readouterr()  # drain output

    def test_spi_target_functions_exist(self):
        """Verify spi_target and spi_target_status command functions exist."""
        assert hasattr(cli, "cmd_spi_target")
        assert hasattr(cli, "cmd_spi_target_status")
        assert callable(cli.cmd_spi_target)
        assert callable(cli.cmd_spi_target_status)

    def test_spi_target_basic_dispatch(self, monkeypatch, capsys):
        """spi-target dispatches to cmd_spi_target with correct args."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target.return_value = {"ok": True, "size": 1024}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv",
                           ["pod", "spi-target", "test-label"])
        ret = cli.main()
        assert ret in (0, None)
        fake_pod.spi_target.assert_called_once()
        # Check that defaults were used
        call_kwargs = fake_pod.spi_target.call_args.kwargs
        assert call_kwargs["mode"] == 0
        assert call_kwargs["bits"] == 8
        assert call_kwargs["miso"] == 16
        assert call_kwargs["mosi"] == 19
        assert call_kwargs["sck"] == 18
        assert call_kwargs["cs"] == 17
        assert call_kwargs["size"] == 1024

    def test_spi_target_custom_args(self, monkeypatch, capsys):
        """spi-target passes custom args to the Pod method."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target.return_value = {"ok": True}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv", [
            "pod", "spi-target", "test-label",
            "--miso", "10", "--mosi", "11", "--sck", "12", "--cs", "13",
            "--size", "512", "--name", "alt_spi"
        ])
        cli.main()
        call_kwargs = fake_pod.spi_target.call_args.kwargs
        assert call_kwargs["miso"] == 10
        assert call_kwargs["mosi"] == 11
        assert call_kwargs["sck"] == 12
        assert call_kwargs["cs"] == 13
        assert call_kwargs["size"] == 512
        assert call_kwargs["name"] == "alt_spi"

    def test_spi_target_status_dispatch(self, monkeypatch, capsys):
        """spi-target-status dispatches to cmd_spi_target_status."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target_status.return_value = {"ok": True, "bytes_rx": 42}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv",
                           ["pod", "spi-target-status", "test-label"])
        ret = cli.main()
        assert ret in (0, None)
        fake_pod.spi_target_status.assert_called_once()
        call_kwargs = fake_pod.spi_target_status.call_args.kwargs
        assert call_kwargs["name"] == "spi_target"

    def test_spi_target_status_custom_name(self, monkeypatch, capsys):
        """spi-target-status passes custom instance name."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target_status.return_value = {"ok": True}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv", [
            "pod", "spi-target-status", "test-label",
            "--name", "alt_spi"
        ])
        cli.main()
        call_kwargs = fake_pod.spi_target_status.call_args.kwargs
        assert call_kwargs["name"] == "alt_spi"

    def test_spi_target_failure_exit_code(self, monkeypatch, capsys):
        """spi-target returns exit code 1 if Pod method returns ok=False."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target.return_value = {"ok": False, "err": "invalid mode"}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv", ["pod", "spi-target", "test-label"])
        ret = cli.main()
        assert ret == 1

    def test_spi_target_success_exit_code(self, monkeypatch, capsys):
        """spi-target returns exit code 0 if Pod method returns ok=True."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target.return_value = {"ok": True}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv", ["pod", "spi-target", "test-label"])
        ret = cli.main()
        assert ret in (0, None)


class TestSpiTargetMcp:
    """Test the MCP server handlers for SPI target."""

    def test_handle_spi_target_basic(self, monkeypatch):
        """handle_spi_target resolves label and calls Pod method."""
        monkeypatch.setattr(mcp_server, "get_pod",
                           lambda label: {"addr4": "192.168.0.100", "repl_port": 8266})
        calls = []

        class FakePod:
            def spi_target(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True, "size": 1024}

        with patch.object(mcp_server.Pod, "from_entry",
                         classmethod(lambda cls, e: FakePod())):
            result = mcp_server.handle_spi_target("test-pod")
            assert result["ok"] is True
            assert result["size"] == 1024
            assert len(calls) == 1
            # Verify defaults
            assert calls[0]["mode"] == 0
            assert calls[0]["bits"] == 8
            assert calls[0]["miso"] == 16

    def test_handle_spi_target_custom_params(self, monkeypatch):
        """handle_spi_target passes custom params to Pod.spi_target."""
        monkeypatch.setattr(mcp_server, "get_pod",
                           lambda label: {"addr4": "192.168.0.100"})
        calls = []

        class FakePod:
            def spi_target(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True}

        with patch.object(mcp_server.Pod, "from_entry",
                         classmethod(lambda cls, e: FakePod())):
            mcp_server.handle_spi_target("test-pod", miso=12, mosi=13, sck=14,
                                        cs=15, size=512, name="alt")
            assert calls[0]["miso"] == 12
            assert calls[0]["mosi"] == 13
            assert calls[0]["sck"] == 14
            assert calls[0]["cs"] == 15
            assert calls[0]["size"] == 512
            assert calls[0]["name"] == "alt"

    def test_handle_spi_target_status_basic(self, monkeypatch):
        """handle_spi_target_status resolves label and calls Pod method."""
        monkeypatch.setattr(mcp_server, "get_pod",
                           lambda label: {"addr4": "192.168.0.100"})
        calls = []

        class FakePod:
            def spi_target_status(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True, "bytes_rx": 42}

        with patch.object(mcp_server.Pod, "from_entry",
                         classmethod(lambda cls, e: FakePod())):
            result = mcp_server.handle_spi_target_status("test-pod")
            assert result["ok"] is True
            assert result["bytes_rx"] == 42
            assert len(calls) == 1
            assert calls[0]["name"] == "spi_target"

    def test_handle_spi_target_status_custom_name(self, monkeypatch):
        """handle_spi_target_status passes custom instance name."""
        monkeypatch.setattr(mcp_server, "get_pod",
                           lambda label: {"addr4": "192.168.0.100"})
        calls = []

        class FakePod:
            def spi_target_status(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True}

        with patch.object(mcp_server.Pod, "from_entry",
                         classmethod(lambda cls, e: FakePod())):
            mcp_server.handle_spi_target_status("test-pod", name="alt_spi")
            assert calls[0]["name"] == "alt_spi"

    def test_spi_target_handler_exists(self):
        """Verify handle_spi_target function exists in mcp_server."""
        assert hasattr(mcp_server, "handle_spi_target")
        assert callable(mcp_server.handle_spi_target)

    def test_spi_target_status_handler_exists(self):
        """Verify handle_spi_target_status function exists in mcp_server."""
        assert hasattr(mcp_server, "handle_spi_target_status")
        assert callable(mcp_server.handle_spi_target_status)


class TestSpiTargetPins:
    """Test that `pod pins` renders the spi_target block via _format_pinmap."""

    def test_format_pinmap_spi_target(self):
        """_format_pinmap emits a spi_target line from a pinmap dict."""
        lines = cli._format_pinmap(
            {"spi_target": {"miso": 16, "mosi": 19, "sck": 18, "cs": 17}})
        joined = "\n".join(lines)
        assert "spi_target:" in joined
        assert "miso=GP16" in joined
        assert "mosi=GP19" in joined
        assert "sck=GP18" in joined
        assert "cs=GP17" in joined

    def test_format_pinmap_no_spi_target(self):
        """No spi_target line when the key is absent from the pinmap."""
        lines = cli._format_pinmap({"nrst": 13})
        assert not any("spi_target" in ln for ln in lines)


# Load the on-pod ring helper in isolation: annealage_pod.spi_target imports
# rp2/machine and cannot be imported under CPython, but _ring.py is pure.
def _load_ring():
    import importlib.util
    import os
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "mpy", "annealage_pod", "_ring.py"))
    spec = importlib.util.spec_from_file_location("_spi_ring_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRingOrder:
    """MOSI capture ring index math (the highest-risk pure logic; wp = bx % size)."""

    ring_order = staticmethod(_load_ring().ring_order)

    def test_empty(self):
        assert self.ring_order(0, 8, 0) == []

    def test_before_wrap(self):
        assert self.ring_order(5, 8, 5) == [0, 1, 2, 3, 4]

    def test_exactly_full_no_wrap(self):
        # bx == size is still the non-wrap case: data is [0, size) in order.
        assert self.ring_order(8, 8, 0) == [0, 1, 2, 3, 4, 5, 6, 7]

    def test_first_wrap(self):
        # size+1 bytes: newest (byte 8) overwrote entry 0, oldest live is entry 1.
        assert self.ring_order(9, 8, 1) == [1, 2, 3, 4, 5, 6, 7, 0]

    def test_multi_wrap(self):
        # 19 bytes, wp = 19 % 8 = 3; chronological window starts at the oldest.
        assert self.ring_order(19, 8, 3) == [3, 4, 5, 6, 7, 0, 1, 2]

    def test_large_wrap_length_capped(self):
        # A >65535-style stream into a small ring returns exactly `size` entries.
        assert len(self.ring_order(100000, 1024, 100000 % 1024)) == 1024
