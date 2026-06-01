"""Tests for pod.client - argv construction and subprocess injection."""

import subprocess
import pytest
from unittest.mock import MagicMock

from pod.client import Pod


ADDRESS = "192.168.0.121"
PORT = 8266
CONNECT_TARGET = f"socket://{ADDRESS}:{PORT}"


@pytest.fixture
def pod():
    """Pod instance with default (real) runner - tests must not invoke it."""
    return Pod(address=ADDRESS, repl_port=PORT)


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


class TestArgvConstruction:
    def test_exec_argv(self, pod):
        argv = pod._argv("exec", "print(1)")
        assert argv == ["ampremote", "connect", CONNECT_TARGET, "exec", "print(1)"]

    def test_eval_argv(self, pod):
        argv = pod._argv("eval", "1+1")
        assert argv == ["ampremote", "connect", CONNECT_TARGET, "eval", "1+1"]

    def test_cp_argv(self, pod):
        # cp goes through fs cp
        argv = pod._argv("fs", "cp", "./main.py", ":main.py")
        assert argv == ["ampremote", "connect", CONNECT_TARGET, "fs", "cp", "./main.py", ":main.py"]

    def test_mount_argv(self, pod):
        argv = pod._argv("mount", "/home/user/firmware")
        assert argv == ["ampremote", "connect", CONNECT_TARGET, "mount", "/home/user/firmware"]

    def test_repl_argv(self, pod):
        argv = pod._argv("repl")
        assert argv == ["ampremote", "connect", CONNECT_TARGET, "repl"]

    def test_connect_target_format(self, pod):
        # Verify the socket:// URI is formed correctly
        argv = pod._argv("exec", "x")
        assert argv[2] == CONNECT_TARGET

    def test_different_port(self):
        p = Pod(address="10.0.0.1", repl_port=9000)
        argv = p._argv("repl")
        assert argv[2] == "socket://10.0.0.1:9000"


class TestExecWithFakeRunner:
    def test_exec_calls_runner(self, pod_fake, fake_runner):
        pod_fake.exec("print('hello')")
        fake_runner.assert_called_once()

    def test_exec_argv_passed_to_runner(self, pod_fake, fake_runner):
        pod_fake.exec("print('hello')")
        call_argv = fake_runner.call_args[0][0]
        assert call_argv[0] == "ampremote"
        assert call_argv[3] == "exec"
        assert call_argv[4] == "print('hello')"

    def test_exec_returns_stdout(self, pod_fake, fake_runner):
        fake_runner.return_value.stdout = "hello\n"
        result = pod_fake.exec("print('hello')")
        assert result == "hello\n"

    def test_exec_passes_capture_output(self, pod_fake, fake_runner):
        pod_fake.exec("x")
        kwargs = fake_runner.call_args[1]
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

    def test_cp_calls_runner(self, pod_fake, fake_runner):
        pod_fake.cp("./main.py", ":main.py")
        fake_runner.assert_called_once()
        call_argv = fake_runner.call_args[0][0]
        assert "fs" in call_argv
        assert "cp" in call_argv

    def test_eval_calls_runner(self, pod_fake, fake_runner):
        fake_runner.return_value.stdout = "2\n"
        result = pod_fake.eval("1+1")
        call_argv = fake_runner.call_args[0][0]
        assert call_argv[3] == "eval"
        assert result == "2\n"


class TestDutOps:
    def test_flash_dut_invokes_cp_and_flash_file(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'addr': 0, 'bytes': 4096, 'ms': 459}\n",
            returncode=0)
        result = pod_fake.flash_dut("fw.bin")
        calls = [c.args[0] for c in fake_runner.call_args_list]
        assert any(
            c[:5] == ["ampremote", "connect", CONNECT_TARGET, "fs", "cp"]
            and "fw.bin" in c for c in calls)
        assert any("flash_file" in " ".join(c) for c in calls)
        assert result.get("ok") is True and result.get("bytes") == 4096

    def test_reset_dut_invokes_reset(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'mode': 'sysreset'}\n", returncode=0)
        result = pod_fake.reset_dut()
        calls = [c.args[0] for c in fake_runner.call_args_list]
        assert any("o.reset" in " ".join(c) for c in calls)
        assert result.get("ok") is True

    def test_read_dut_invokes_dump_and_cp_back(self, pod_fake, fake_runner):
        pod_fake.read_dut(0x1000, 256, "/tmp/out.bin")
        calls = [c.args[0] for c in fake_runner.call_args_list]
        assert any("dump_to_file" in " ".join(c) for c in calls)
        assert any(
            c[:5] == ["ampremote", "connect", CONNECT_TARGET, "fs", "cp"]
            and "/tmp/out.bin" in c for c in calls)


class TestNotImplementedStubs:
    def test_usbip_attach_raises(self, pod):
        with pytest.raises(NotImplementedError) as exc_info:
            pod.usbip_attach()
        assert "Phase 4" in str(exc_info.value)

    def test_uart_stream_raises(self, pod):
        with pytest.raises(NotImplementedError) as exc_info:
            pod.uart_stream()
        assert "Phase 5" in str(exc_info.value)

    def test_telemetry_raises(self, pod):
        with pytest.raises(NotImplementedError) as exc_info:
            pod.telemetry()
        assert "Phase 5" in str(exc_info.value)

    def test_gdb_endpoint_raises(self, pod):
        with pytest.raises(NotImplementedError) as exc_info:
            pod.gdb_endpoint()
        assert "Phase 3" in str(exc_info.value)
