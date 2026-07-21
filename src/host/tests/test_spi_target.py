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

    def test_spi_target_personality_and_table_size(self, monkeypatch, capsys):
        """spi-target passes personality and table_size to the Pod method."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target.return_value = {"ok": True}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv", [
            "pod", "spi-target", "test-label",
            "--personality", "regfile", "--table-size", "512"
        ])
        cli.main()
        call_kwargs = fake_pod.spi_target.call_args.kwargs
        assert call_kwargs["personality"] == "regfile"
        assert call_kwargs["table_size"] == 512

    def test_spi_target_mode_options(self, monkeypatch, capsys):
        """spi-target passes SPI mode 0-3."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target.return_value = {"ok": True}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv", [
            "pod", "spi-target", "test-label", "--mode", "3"
        ])
        cli.main()
        call_kwargs = fake_pod.spi_target.call_args.kwargs
        assert call_kwargs["mode"] == 3

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

    def test_handle_spi_target_personality_and_table_size(self, monkeypatch):
        """handle_spi_target passes personality and table_size."""
        monkeypatch.setattr(mcp_server, "get_pod",
                           lambda label: {"addr4": "192.168.0.100"})
        calls = []

        class FakePod:
            def spi_target(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True}

        with patch.object(mcp_server.Pod, "from_entry",
                         classmethod(lambda cls, e: FakePod())):
            mcp_server.handle_spi_target("test-pod", personality="regfile",
                                        table_size=512)
            assert calls[0]["personality"] == "regfile"
            assert calls[0]["table_size"] == 512

    def test_handle_spi_target_mode_values(self, monkeypatch):
        """handle_spi_target accepts all SPI modes 0-3."""
        monkeypatch.setattr(mcp_server, "get_pod",
                           lambda label: {"addr4": "192.168.0.100"})
        calls = []

        class FakePod:
            def spi_target(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True}

        with patch.object(mcp_server.Pod, "from_entry",
                         classmethod(lambda cls, e: FakePod())):
            for mode in [0, 1, 2, 3]:
                calls.clear()
                mcp_server.handle_spi_target("test-pod", mode=mode)
                assert calls[0]["mode"] == mode

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

    def test_handle_spi_target_regs_handler_exists(self):
        """Verify handle_spi_target_regs function exists in mcp_server."""
        assert hasattr(mcp_server, "handle_spi_target_regs")
        assert callable(mcp_server.handle_spi_target_regs)

    def test_handle_spi_target_regs_basic(self, monkeypatch):
        """handle_spi_target_regs resolves label and calls Pod method."""
        monkeypatch.setattr(mcp_server, "get_pod",
                           lambda label: {"addr4": "192.168.0.100"})
        calls = []

        class FakePod:
            def spi_target_regs(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True, "table": "read", "regs": [0, 1, 2]}

        with patch.object(mcp_server.Pod, "from_entry",
                         classmethod(lambda cls, e: FakePod())):
            result = mcp_server.handle_spi_target_regs("test-pod")
            assert result["ok"] is True
            assert result["table"] == "read"
            assert len(calls) == 1
            assert calls[0]["off"] == 0
            assert calls[0]["table"] == "read"

    def test_handle_spi_target_regs_custom_params(self, monkeypatch):
        """handle_spi_target_regs passes custom params to Pod.spi_target_regs."""
        monkeypatch.setattr(mcp_server, "get_pod",
                           lambda label: {"addr4": "192.168.0.100"})
        calls = []

        class FakePod:
            def spi_target_regs(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True}

        with patch.object(mcp_server.Pod, "from_entry",
                         classmethod(lambda cls, e: FakePod())):
            mcp_server.handle_spi_target_regs(
                "test-pod", off=5, length=16, write=[0xAA, 0xBB],
                table="write", name="alt")
            assert calls[0]["off"] == 5
            assert calls[0]["length"] == 16
            assert calls[0]["write"] == [0xAA, 0xBB]
            assert calls[0]["table"] == "write"
            assert calls[0]["name"] == "alt"


class TestSpiTargetRegsClient:
    """Test Pod.spi_target_regs code generation."""

    @staticmethod
    def _code(fake_runner):
        """Extract the generated on-pod code from the runner call args."""
        return fake_runner.call_args[0][0][4]

    def test_spi_target_regs_read_basic(self, pod_fake, fake_runner):
        """spi_target_regs generates correct code for a read operation."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'table': 'read', 'regs': [0, 1, 2, 3]}\n",
            returncode=0)
        result = pod_fake.spi_target_regs()
        code = self._code(fake_runner)
        assert "annealage_pod.peripherals" in code
        assert "p.spi_target_regs(" in code
        assert "table='read'" in code
        assert result["ok"] is True
        assert result["table"] == "read"
        assert result["regs"] == [0, 1, 2, 3]

    def test_spi_target_regs_write_table(self, pod_fake, fake_runner):
        """spi_target_regs with table='write' includes it in the code."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'table': 'write', 'regs': [0xAA, 0xBB]}\n",
            returncode=0)
        result = pod_fake.spi_target_regs(table="write")
        code = self._code(fake_runner)
        assert "table='write'" in code

    def test_spi_target_regs_with_write_payload(self, pod_fake, fake_runner):
        """spi_target_regs with write payload passes it as repr list."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'table': 'read', 'regs': [0xAA, 0xBB]}\n",
            returncode=0)
        result = pod_fake.spi_target_regs(write=[0xAA, 0xBB], off=2)
        code = self._code(fake_runner)
        assert "off=2" in code
        assert "write=[170, 187]" in code

    def test_spi_target_regs_with_length(self, pod_fake, fake_runner):
        """spi_target_regs with length parameter."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'table': 'read', 'regs': [1, 2, 3, 4, 5]}\n",
            returncode=0)
        result = pod_fake.spi_target_regs(off=10, length=5)
        code = self._code(fake_runner)
        assert "off=10" in code
        assert "length=5" in code

    def test_spi_target_regs_custom_name(self, pod_fake, fake_runner):
        """spi_target_regs with custom instance name."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'table': 'read', 'regs': []}\n",
            returncode=0)
        result = pod_fake.spi_target_regs(name="alt_spi")
        code = self._code(fake_runner)
        assert "name='alt_spi'" in code


class TestSpiTargetRegsCli:
    """Test the CLI spi-target-regs subcommand."""

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
        capsys.readouterr()

    def test_cmd_spi_regs_exists(self):
        """Verify cmd_spi_regs exists in cli."""
        assert hasattr(cli, "cmd_spi_regs")
        assert callable(cli.cmd_spi_regs)

    def test_spi_target_regs_basic_dispatch(self, monkeypatch, capsys):
        """spi-target-regs dispatches to cmd_spi_regs with correct args."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target_regs.return_value = {"ok": True, "table": "read",
                                                  "regs": [1, 2, 3]}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv",
                           ["pod", "spi-target-regs", "test-label"])
        ret = cli.main()
        assert ret in (0, None)
        fake_pod.spi_target_regs.assert_called_once()
        call_kwargs = fake_pod.spi_target_regs.call_args.kwargs
        assert call_kwargs["off"] == 0
        assert call_kwargs["table"] == "read"
        assert call_kwargs["name"] == "spi_target"

    def test_spi_target_regs_custom_off_and_length(self, monkeypatch, capsys):
        """spi-target-regs with --off and --length."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target_regs.return_value = {"ok": True}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv", [
            "pod", "spi-target-regs", "test-label",
            "--off", "5", "--length", "10"
        ])
        cli.main()
        call_kwargs = fake_pod.spi_target_regs.call_args.kwargs
        assert call_kwargs["off"] == 5
        assert call_kwargs["length"] == 10

    def test_spi_target_regs_with_write(self, monkeypatch, capsys):
        """spi-target-regs with --write payload."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target_regs.return_value = {"ok": True}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv", [
            "pod", "spi-target-regs", "test-label",
            "--write", "0xAA", "0xBB", "0xCC"
        ])
        cli.main()
        call_kwargs = fake_pod.spi_target_regs.call_args.kwargs
        assert call_kwargs["write"] == [0xAA, 0xBB, 0xCC]

    def test_spi_target_regs_write_table(self, monkeypatch, capsys):
        """spi-target-regs with --table write."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target_regs.return_value = {"ok": True}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv", [
            "pod", "spi-target-regs", "test-label",
            "--table", "write"
        ])
        cli.main()
        call_kwargs = fake_pod.spi_target_regs.call_args.kwargs
        assert call_kwargs["table"] == "write"

    def test_spi_target_regs_custom_name(self, monkeypatch, capsys):
        """spi-target-regs with --name."""
        self._register_test_pod(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.spi_target_regs.return_value = {"ok": True}
        monkeypatch.setattr(cli, "Pod",
                           SimpleNamespace(from_entry=lambda e: fake_pod))
        monkeypatch.setattr(sys, "argv", [
            "pod", "spi-target-regs", "test-label",
            "--name", "alt_spi"
        ])
        cli.main()
        call_kwargs = fake_pod.spi_target_regs.call_args.kwargs
        assert call_kwargs["name"] == "alt_spi"


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


# Load the on-pod regtable helper in isolation: _regtable.py is pure.
def _load_regtable():
    import importlib.util
    import os
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "mpy", "annealage_pod", "_regtable.py"))
    spec = importlib.util.spec_from_file_location("_regtable_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRegTable:
    """Register-table state machine: write-then-read, pointer updates, wrap logic."""

    RegTable = staticmethod(_load_regtable().RegTable)
    parse_cmd = staticmethod(_load_regtable().parse_cmd)
    apply_write = staticmethod(_load_regtable().apply_write)

    def test_parse_cmd_write(self):
        """parse_cmd extracts offset and read/write direction."""
        offset, is_read = self.parse_cmd(0x05)  # no bit7
        assert offset == 5
        assert is_read is False

    def test_parse_cmd_read(self):
        """parse_cmd with bit7 set indicates a read."""
        offset, is_read = self.parse_cmd(0x85)  # bit7 set
        assert offset == 5
        assert is_read is True

    def test_parse_cmd_offset_0(self):
        """parse_cmd(0x00) is write to offset 0."""
        offset, is_read = self.parse_cmd(0x00)
        assert offset == 0
        assert is_read is False

    def test_parse_cmd_offset_127(self):
        """parse_cmd(0x7F) is write to offset 127 (max 7-bit)."""
        offset, is_read = self.parse_cmd(0x7F)
        assert offset == 127
        assert is_read is False

    def test_parse_cmd_read_127(self):
        """parse_cmd(0xFF) is read from offset 127."""
        offset, is_read = self.parse_cmd(0xFF)
        assert offset == 127
        assert is_read is True

    def test_apply_write_basic(self):
        """apply_write stores bytes at offset."""
        buf = bytearray(256)
        written = self.apply_write(buf, 10, [0xAA, 0xBB], 256)
        assert written == 2
        assert buf[10] == 0xAA
        assert buf[11] == 0xBB

    def test_apply_write_wraps(self):
        """apply_write wraps around the buffer size."""
        buf = bytearray(4)
        written = self.apply_write(buf, 2, [0xA, 0xB, 0xC, 0xD], 4)
        assert written == 4
        assert buf[2] == 0xA
        assert buf[3] == 0xB
        assert buf[0] == 0xC
        assert buf[1] == 0xD

    def test_apply_write_masks_bytes(self):
        """apply_write masks each byte to 0xFF."""
        buf = bytearray(4)
        self.apply_write(buf, 0, [0x1AA, 0x2BB], 4)
        assert buf[0] == 0xAA
        assert buf[1] == 0xBB

    def test_regtable_write_then_read(self):
        """RegTable: write payload, then read advances pointer."""
        tbl = self.RegTable(size=256)
        # Seed the read table
        for i in range(256):
            tbl.read_table[i] = i
        # Write transaction: [0x10, 0xAA, 0xBB]
        tbl.feed([0x10, 0xAA, 0xBB])
        assert tbl.reg_ptr == 0x10
        assert tbl.last_offset == 0x10
        assert tbl.last_length == 3
        assert tbl.last_was_write is True
        assert tbl.write_table[0x10] == 0xAA
        assert tbl.write_table[0x11] == 0xBB
        # Read 2 bytes from pointer
        read_data = tbl.read(2)
        assert read_data == [0x10, 0x11]
        assert tbl.reg_ptr == 0x12

    def test_regtable_pointer_only_update(self):
        """RegTable: zero-payload write updates pointer without writing."""
        tbl = self.RegTable(size=256)
        for i in range(256):
            tbl.write_table[i] = i
            tbl.read_table[i] = (i * 2) & 0xFF
        # Write [0x05] with no payload: pointer-only update
        tbl.feed([0x05])
        assert tbl.reg_ptr == 5
        assert tbl.last_was_write is True
        assert tbl.last_length == 1
        assert tbl.write_table[5] == 5  # unchanged

    def test_regtable_read_command(self):
        """RegTable: bit7 read command does not write."""
        tbl = self.RegTable(size=256)
        for i in range(256):
            tbl.write_table[i] = 0xFF
        # Read command [0x85, dummy, dummy]: bit7 = 1, offset = 5
        tbl.feed([0x85, 0x00, 0x00])
        assert tbl.reg_ptr == 5
        assert tbl.last_was_write is False
        assert tbl.write_table[5] == 0xFF  # unchanged

    def test_regtable_wrap_past_table_end(self):
        """RegTable: write wraps pointer around table size."""
        tbl = self.RegTable(size=4)
        # Feed a write starting at offset 2 with 3 bytes
        tbl.feed([0x02, 0xA, 0xB, 0xC])
        assert tbl.reg_ptr == 2
        assert tbl.write_table[2] == 0xA
        assert tbl.write_table[3] == 0xB
        assert tbl.write_table[0] == 0xC

    def test_regtable_offset_windows(self):
        """RegTable: reads advance through the table, wrapping."""
        tbl = self.RegTable(size=4)
        for i in range(4):
            tbl.read_table[i] = (i + 10) & 0xFF
        # Pointer at 3, read 4 bytes (wraps to 0, 1, 2, 3)
        tbl.feed([0x03])
        read_data = tbl.read(4)
        assert read_data == [13, 10, 11, 12]
        assert tbl.reg_ptr == 3  # wrapped full cycle

    def test_regtable_offset_modulo_size(self):
        """RegTable: command offset is modulo size if size < 128."""
        tbl = self.RegTable(size=64)  # smaller than 7-bit offset space
        # Command byte 0x7F (offset 127 in 7-bit) should wrap to 127 % 64 = 63
        tbl.feed([0x7F, 0x42])
        assert tbl.reg_ptr == 63
        assert tbl.write_table[63] == 0x42


class TestSpiTargetPersonality:
    """Test regfile personality parameter passing and defaults."""

    def test_spi_target_personality_stream_default(self, pod_fake, fake_runner):
        """spi_target without personality argument defaults to 'stream'."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'personality': 'stream'}\n",
            returncode=0)
        result = pod_fake.spi_target()
        code = fake_runner.call_args[0][0][4]
        assert "personality='stream'" in code

    def test_spi_target_personality_regfile(self, pod_fake, fake_runner):
        """spi_target with personality='regfile' passes it through."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'personality': 'regfile', 'table_size': 256}\n",
            returncode=0)
        result = pod_fake.spi_target(personality="regfile")
        code = fake_runner.call_args[0][0][4]
        assert "personality='regfile'" in code

    def test_spi_target_table_size_default(self, pod_fake, fake_runner):
        """spi_target without table_size defaults to 256."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'table_size': 256}\n",
            returncode=0)
        result = pod_fake.spi_target()
        code = fake_runner.call_args[0][0][4]
        assert "table_size=256" in code

    def test_spi_target_table_size_custom(self, pod_fake, fake_runner):
        """spi_target with custom table_size passes it through."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'table_size': 512}\n",
            returncode=0)
        result = pod_fake.spi_target(table_size=512)
        code = fake_runner.call_args[0][0][4]
        assert "table_size=512" in code

    def test_spi_target_mode_1(self, pod_fake, fake_runner):
        """spi_target with mode=1 passes it through."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'mode': 1}\n",
            returncode=0)
        result = pod_fake.spi_target(mode=1)
        code = fake_runner.call_args[0][0][4]
        assert "mode=1" in code

    def test_spi_target_mode_2(self, pod_fake, fake_runner):
        """spi_target with mode=2 passes it through."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'mode': 2}\n",
            returncode=0)
        result = pod_fake.spi_target(mode=2)
        code = fake_runner.call_args[0][0][4]
        assert "mode=2" in code

    def test_spi_target_mode_3(self, pod_fake, fake_runner):
        """spi_target with mode=3 passes it through."""
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'mode': 3}\n",
            returncode=0)
        result = pod_fake.spi_target(mode=3)
        code = fake_runner.call_args[0][0][4]
        assert "mode=3" in code


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
