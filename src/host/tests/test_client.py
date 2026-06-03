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
    def test_flash_stream_cmd(self):
        cmd = Pod._flash_stream_cmd(0x1000, 4096, 3333, True)
        assert "flash_stream" in cmd
        assert "4096" in cmd and "4096" in cmd
        assert "port=3333" in cmd
        assert "verify=True" in cmd
        # addr is rendered as the decimal of 0x1000
        assert str(0x1000) in cmd

    def test_reset_dut_invokes_reset(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'mode': 'sysreset'}\n", returncode=0)
        result = pod_fake.reset_dut()
        calls = [c.args[0] for c in fake_runner.call_args_list]
        assert any("o.reset" in " ".join(c) for c in calls)
        assert result.get("ok") is True

    def test_dump_stream_cmd(self):
        cmd = Pod._dump_stream_cmd(0x1000, 256, 3334)
        assert "dump_stream" in cmd
        assert "256" in cmd
        assert "port=3334" in cmd
        assert str(0x1000) in cmd

    def test_gdb_serve_cmd(self):
        cmd = Pod._gdb_serve_cmd(3335, True)
        assert "gdb_serve" in cmd
        assert "port=3335" in cmd
        assert "reset_halt=True" in cmd

    def test_gdb_serve_cmd_no_reset_halt(self):
        cmd = Pod._gdb_serve_cmd(3335, False)
        assert "reset_halt=False" in cmd


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


class TestPeripherals:
    @staticmethod
    def _code(fake_runner):
        # exec argv is [ampremote, connect, target, exec, <code>]
        return fake_runner.call_args[0][0][4]

    def test_i2c_target_code(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'name': 'i2c_target', 'addr': 66, 'bus': 1, "
                   "'scl': 11, 'sda': 10, 'size': 256}\n",
            returncode=0)
        result = pod_fake.i2c_target(addr=0x42, regs=[0xAB, 0xCD])
        code = self._code(fake_runner)
        assert "annealage_pod.peripherals" in code
        assert "p.i2c_target(" in code
        assert "addr=66" in code
        assert "[171, 205]" in code
        assert "scl=11" in code and "sda=10" in code
        assert result["ok"] is True and result["addr"] == 66

    def test_i2c_target_no_regs(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(stdout="{'ok': True}\n", returncode=0)
        pod_fake.i2c_target()
        assert "regs=None" in self._code(fake_runner)

    def test_i2c_target_regs_write(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'regs': [1, 2]}\n", returncode=0)
        result = pod_fake.i2c_target_regs(off=4, write=[1, 2])
        code = self._code(fake_runner)
        assert "i2c_target_regs(" in code
        assert "off=4" in code
        assert "write=[1, 2]" in code
        assert result["regs"] == [1, 2]

    def test_i2c_target_regs_read(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'regs': [171, 205]}\n", returncode=0)
        pod_fake.i2c_target_regs(off=0, length=2)
        code = self._code(fake_runner)
        assert "write=None" in code
        assert "length=2" in code

    def test_gpio_read(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'pin': 5, 'value': 1}\n", returncode=0)
        result = pod_fake.gpio(5)
        code = self._code(fake_runner)
        assert "p.gpio(5" in code
        assert "value=None" in code
        assert result["value"] == 1

    def test_gpio_drive(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'pin': 5, 'value': 0}\n", returncode=0)
        pod_fake.gpio(5, value=0)
        assert "value=0" in self._code(fake_runner)

    def test_adc(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'pin': 26, 'u16': 32768, 'volts': 1.65}\n",
            returncode=0)
        result = pod_fake.adc(26)
        assert "p.adc(26)" in self._code(fake_runner)
        assert result["u16"] == 32768

    def test_peripheral_release(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'released': ['i2c_target']}\n", returncode=0)
        result = pod_fake.peripheral_release("i2c_target")
        code = self._code(fake_runner)
        assert "p.release(" in code and "i2c_target" in code
        assert result["released"] == ["i2c_target"]

    def test_peripheral_list(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'instances': []}\n", returncode=0)
        result = pod_fake.peripheral_list()
        assert "p.instances()" in self._code(fake_runner)
        assert result["instances"] == []
