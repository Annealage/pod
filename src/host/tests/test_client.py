"""Tests for pod.client - argv construction and subprocess injection."""

import os
import subprocess
import pytest
from types import SimpleNamespace
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
    """Pod instance with injected fake runner.

    caller is pinned rather than left to the real resolve_caller() (which
    depends on the environment running the tests), so any test asserting on
    the caller= the SWD guard sees embedded in a generated exec is
    deterministic.
    """
    p = Pod(address=ADDRESS, repl_port=PORT, runner=fake_runner)
    p.caller = "test-caller"
    return p


def _norm(argv):
    """argv with the ampremote path reduced to its basename.

    _argv resolves the CLI next to sys.executable when one is installed there
    (a venv or uv tool install), so the leading element is layout-dependent.
    These tests cover argument construction, not which copy gets found.
    """
    return [os.path.basename(argv[0])] + list(argv[1:])


class TestArgvConstruction:
    def test_exec_argv(self, pod):
        argv = pod._argv("exec", "print(1)")
        assert _norm(argv) == ["ampremote", "connect", CONNECT_TARGET, "exec", "print(1)"]

    def test_eval_argv(self, pod):
        argv = pod._argv("eval", "1+1")
        assert _norm(argv) == ["ampremote", "connect", CONNECT_TARGET, "eval", "1+1"]

    def test_cp_argv(self, pod):
        # cp goes through fs cp
        argv = pod._argv("fs", "cp", "./main.py", ":main.py")
        assert _norm(argv) == ["ampremote", "connect", CONNECT_TARGET, "fs", "cp", "./main.py", ":main.py"]

    def test_mount_argv(self, pod):
        argv = pod._argv("mount", "/home/user/firmware")
        assert _norm(argv) == ["ampremote", "connect", CONNECT_TARGET, "mount", "/home/user/firmware"]

    def test_repl_argv(self, pod):
        argv = pod._argv("repl")
        assert _norm(argv) == ["ampremote", "connect", CONNECT_TARGET, "repl"]

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
        assert os.path.basename(call_argv[0]) == "ampremote"
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


class TestResolveRegsel:
    def test_names(self):
        from pod.client import _resolve_regsel
        assert _resolve_regsel("pc") == 15
        assert _resolve_regsel("SP") == 13
        assert _resolve_regsel("r0") == 0
        assert _resolve_regsel("xpsr") == 16

    def test_ints_passthrough(self):
        from pod.client import _resolve_regsel
        assert _resolve_regsel(7) == 7
        assert _resolve_regsel("18") == 18  # numeric strings -> int()

    def test_unknown_name_raises(self):
        from pod.client import _resolve_regsel
        with pytest.raises(ValueError):
            _resolve_regsel("banana")

    def test_out_of_range_raises(self):
        from pod.client import _resolve_regsel
        for bad in (19, -1, "99", "0x40"):
            with pytest.raises(ValueError):
                _resolve_regsel(bad)


class TestDutProtectRanges:
    def test_floor_only_when_undeclared(self):
        from pod.registry import dut_protect_ranges, CORTEX_M_SRAM_BASE
        assert dut_protect_ranges({}) == [[0, CORTEX_M_SRAM_BASE]]
        assert dut_protect_ranges(None) == [[0, CORTEX_M_SRAM_BASE]]

    def test_low_flash_subsumed_by_floor(self):
        # nRF52 / RP2350 flash sits below the SRAM base -> not appended
        from pod.registry import dut_protect_ranges, CORTEX_M_SRAM_BASE
        e = {"dut": {"flash_base": 0x10000000, "flash_size": 0x200000}}
        assert dut_protect_ranges(e) == [[0, CORTEX_M_SRAM_BASE]]

    def test_high_flash_appended(self):
        from pod.registry import dut_protect_ranges, CORTEX_M_SRAM_BASE
        e = {"dut": {"flash_base": 0x60000000, "flash_size": 0x100000}}
        assert dut_protect_ranges(e) == [[0, CORTEX_M_SRAM_BASE],
                                         [0x60000000, 0x60100000]]


class TestWriteMemProtect:
    def test_refuses_overlap_before_roundtrip(self, pod_fake, fake_runner):
        result = pod_fake.write_mem(0x1000, b"\xaa\xbb", protect=[[0, 0x20000000]])
        assert result["ok"] is False and "write-protected" in result["err"]
        fake_runner.assert_not_called()

    def test_allows_ram_and_encodes_protect(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'addr': 536870912, 'length': 2}\n", returncode=0)
        pod_fake.write_mem(0x20000000, b"\xaa\xbb", protect=[[0, 0x20000000]])
        code = fake_runner.call_args[0][0][4]
        assert "protect=[[0, 536870912]]" in code

    def test_read_mem_length_out_of_range_raises(self, pod):
        with pytest.raises(ValueError):
            pod.read_mem(0x20000000, 5000)

    def test_write_mem_oversize_raises(self, pod):
        with pytest.raises(ValueError):
            pod.write_mem(0x20000000, b"\x00" * 5000)


class TestDutDebugPeek:
    @staticmethod
    def _code(fake_runner):
        # exec argv is [ampremote, connect, target, exec, <code>]
        return fake_runner.call_args[0][0][4]

    def test_halt_code_and_result(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'halted': True, 'dhcsr': 131072}\n",
            returncode=0)
        result = pod_fake.halt_dut()
        assert "o.halt(caller='test-caller')" in self._code(fake_runner)
        assert result["ok"] is True and result["halted"] is True

    def test_resume_code(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'halted': False}\n", returncode=0)
        result = pod_fake.resume_dut()
        assert "o.resume(caller='test-caller')" in self._code(fake_runner)
        assert result["halted"] is False

    def test_read_reg_by_name_resolves_regsel(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'regsel': 15, 'value': 268439552}\n",
            returncode=0)
        result = pod_fake.read_reg("pc")
        assert "o.read_reg(15, caller='test-caller')" in self._code(fake_runner)
        assert result["value"] == 268439552

    def test_read_reg_by_int(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'regsel': 0, 'value': 1}\n", returncode=0)
        pod_fake.read_reg(0)
        assert "o.read_reg(0, caller='test-caller')" in self._code(fake_runner)

    def test_read_reg_not_halted(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': False, 'err': 'core is running; halt() it first'}\n",
            returncode=0)
        result = pod_fake.read_reg("pc")
        assert result["ok"] is False

    def test_write_reg_masks_and_resolves(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'regsel': 13, 'value': 536887296}\n",
            returncode=0)
        pod_fake.write_reg("sp", 0x20004000)
        code = self._code(fake_runner)
        assert "o.write_reg(13, %d, caller='test-caller')" % 0x20004000 in code

    def test_read_mem_code_and_hex(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'addr': 536870912, 'length': 4, "
                   "'hex': 'deadbeef'}\n", returncode=0)
        result = pod_fake.read_mem(0x20000000, 4)
        assert ("o.read_mem(%d, 4, caller='test-caller')" % 0x20000000
                in self._code(fake_runner))
        assert result["hex"] == "deadbeef"

    def test_write_mem_from_bytes_hexlifies(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'addr': 536870912, 'length': 4}\n",
            returncode=0)
        pod_fake.write_mem(0x20000000, b"\xde\xad\xbe\xef")
        code = self._code(fake_runner)
        assert ("o.write_mem(%d, 'deadbeef', protect=None, caller='test-caller')"
                % 0x20000000 in code)

    def test_write_mem_accepts_hex_string(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'addr': 536870912, 'length': 2}\n",
            returncode=0)
        pod_fake.write_mem(0x20000000, "beef")
        assert ("o.write_mem(%d, 'beef', protect=None, caller='test-caller')"
                % 0x20000000 in self._code(fake_runner))


class TestMcpDutDebugPeek:
    def _fake_pod(self, monkeypatch):
        import pod.mcp_server as m
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        calls = {}

        class FakePod:
            def halt_dut(self, keep_attached=False, force=False):
                calls["halt"] = keep_attached
                return {"ok": True, "halted": True, "dhcsr": 0x20000}

            def resume_dut(self):
                calls["resume"] = True
                return {"ok": True, "halted": False}

            def read_reg(self, reg):
                calls["read_reg"] = reg
                return {"ok": True, "regsel": 15, "value": 0x1000}

            def write_reg(self, reg, value):
                calls["write_reg"] = (reg, value)
                return {"ok": True, "regsel": 13, "value": value}

            def read_mem(self, addr, length):
                calls["read_mem"] = (addr, length)
                return {"ok": True, "addr": addr, "length": length, "hex": "00"}

            def write_mem(self, addr, data, protect=None):
                calls["write_mem"] = (addr, data)
                calls["write_mem_protect"] = protect
                return {"ok": True, "addr": addr, "length": 1}

        monkeypatch.setattr(m.Pod, "from_entry",
                            classmethod(lambda cls, e: FakePod()))
        return m, calls

    def test_halt_resume(self, monkeypatch):
        m, calls = self._fake_pod(monkeypatch)
        assert m.handle_dut_halt("x")["halted"] is True
        assert m.handle_dut_resume("x")["halted"] is False
        assert "halt" in calls and calls["resume"]

    def test_read_write_reg(self, monkeypatch):
        m, calls = self._fake_pod(monkeypatch)
        assert m._read_reg("x", "pc")["value"] == 0x1000
        m._write_reg("x", "sp", 0x20004000)
        assert calls["read_reg"] == "pc"
        assert calls["write_reg"] == ("sp", 0x20004000)

    def test_read_write_mem(self, monkeypatch):
        m, calls = self._fake_pod(monkeypatch)
        m._read_mem_inline("x", 0x20000000, 16)
        m._write_mem("x", 0x20000000, "deadbeef")
        assert calls["read_mem"] == (0x20000000, 16)
        assert calls["write_mem"] == (0x20000000, "deadbeef")


class TestUsbipAttach:
    def test_attach_flow(self, pod, monkeypatch):
        import pod.usbip as u
        calls = {}
        monkeypatch.setattr(u, "ensure_server", lambda p: calls.setdefault("ensure", True))
        monkeypatch.setattr(u, "list_remote",
                            lambda host: [{"busid": "1-1", "vid": "f055", "pid": "9802"}])
        monkeypatch.setattr(u, "attach",
                            lambda host, busid: calls.setdefault("attach", (host, busid)))
        monkeypatch.setattr(u, "serial_devices", lambda: set())
        monkeypatch.setattr(u, "wait_for_new_tty", lambda before: "/dev/ttyACM1")
        dev = pod.usbip_attach()
        assert dev == {"busid": "1-1", "vid": "f055", "pid": "9802",
                       "tty": "/dev/ttyACM1"}
        assert calls["ensure"] is True
        assert calls["attach"][1] == "1-1"

    def test_attach_no_device_raises(self, pod, monkeypatch):
        import pod.usbip as u
        monkeypatch.setattr(u, "ensure_server", lambda p: None)
        monkeypatch.setattr(u, "list_remote", lambda host: [])
        monkeypatch.setattr(pod, "reprobe_dut", lambda: {"ok": True, "mounted": 0})
        with pytest.raises(RuntimeError):
            pod.usbip_attach()

    def test_attach_reprobes_then_succeeds(self, pod, monkeypatch):
        """An empty first export triggers reprobe_dut, and the retry finds the
        DUT (the mounted-but-unexportable / warm-reset recovery path)."""
        import pod.usbip as u
        calls = {"list": 0, "reprobe": 0}

        def _list(host):
            calls["list"] += 1
            return [] if calls["list"] == 1 else [
                {"busid": "1-1", "vid": "f055", "pid": "9802"}]

        def _reprobe():
            calls["reprobe"] += 1
            return {"ok": True, "mounted": 2}

        monkeypatch.setattr(u, "ensure_server", lambda p: None)
        monkeypatch.setattr(u, "list_remote", _list)
        monkeypatch.setattr(u, "attach", lambda host, busid: None)
        monkeypatch.setattr(u, "serial_devices", lambda: set())
        monkeypatch.setattr(u, "wait_for_new_tty", lambda before: "/dev/ttyACM1")
        monkeypatch.setattr(pod, "reprobe_dut", _reprobe)
        dev = pod.usbip_attach()
        assert dev["busid"] == "1-1" and dev["tty"] == "/dev/ttyACM1"
        assert calls["reprobe"] == 1 and calls["list"] == 2

    def test_reprobe_dut_parses_result(self, pod, monkeypatch):
        monkeypatch.setattr(pod, "exec", lambda code: "{'ok': True, 'mounted': 2}\n")
        assert pod.reprobe_dut() == {"ok": True, "mounted": 2}


class TestNotImplementedStubs:
    # usbip_attach and uart_stream are implemented (see TestUsbipAttach and the
    # UART bridge tests in test_uart.py); telemetry remains a stub pending its phase.
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


class _FakeSock:
    """Serves a byte buffer via recv() in <= `split`-byte chunks; b'' at EOF."""

    def __init__(self, data, split=3):
        self.data = data
        self.pos = 0
        self.split = split

    def recv(self, n):
        end = min(self.pos + min(n, self.split), len(self.data))
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk


class TestLogicAnalyser:
    def test_la_stream_cmd_basic(self):
        cmd = Pod._la_stream_cmd(16, 1, 1000000, 8000, None, 3336, 0)
        assert "la_stream" in cmd
        assert "16" in cmd
        assert "width=1" in cmd
        assert "rate=1000000" in cmd
        assert "depth=8000" in cmd
        assert "trigger=None" in cmd
        assert "port=3336" in cmd
        assert "sm_id=0" in cmd

    def test_la_stream_cmd_trigger(self):
        cmd = Pod._la_stream_cmd(16, 8, 2000000, 4000, (16, "rise"), 3336, 0)
        assert "width=8" in cmd
        assert "rate=2000000" in cmd
        assert "trigger=(16, 'rise')" in cmd

    def test_recv_exact_assembles(self):
        assert Pod._recv_exact(_FakeSock(b"abcdef", split=2), 6) == b"abcdef"

    def test_recv_exact_eof(self):
        with pytest.raises(EOFError):
            Pod._recv_exact(_FakeSock(b"abc", split=2), 6)


class TestExecError:
    def _failing_pod(self, stderr):
        runner = MagicMock(return_value=MagicMock(
            returncode=1, stdout="", stderr=stderr))
        return Pod(address="10.0.0.1", runner=runner)

    def test_raw_repl_classified(self):
        from pod.client import PodExecError
        p = self._failing_pod("ampremote: could not enter raw repl")
        with pytest.raises(PodExecError) as e:
            p.exec("print(1)")
        assert "raw-REPL" in e.value.reason
        assert "raw repl" in e.value.stderr

    def test_syntax_classified(self):
        from pod.client import PodExecError
        p = self._failing_pod("SyntaxError: invalid syntax")
        with pytest.raises(PodExecError) as e:
            p.exec("def")
        assert "syntax" in e.value.reason

    def test_success_returns_stdout(self):
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout="ok\n"))
        p = Pod(address="10.0.0.1", runner=runner)
        assert p.exec("print('ok')") == "ok\n"


class TestAttachedPorts:
    def test_filters_by_pod_address(self, monkeypatch):
        import pod.usbip as u
        monkeypatch.setattr(u, "ports", lambda: [
            {"port": 0, "remote": "192.168.0.146", "busid": "1-1"},
            {"port": 1, "remote": "10.9.9.9", "busid": "1-1"}])
        p = Pod(addr4="192.168.0.146", hostname="annealage-pod.local")
        assert p.attached_ports() == [0]

    def test_detach_all_detaches_matching(self, monkeypatch):
        import pod.usbip as u
        detached = []
        monkeypatch.setattr(u, "ports", lambda: [
            {"port": 0, "remote": "192.168.0.146", "busid": "1-1"}])
        monkeypatch.setattr(u, "detach", lambda port: detached.append(port) or True)
        p = Pod(addr4="192.168.0.146")
        assert p.usbip_detach() == {"detached": [0]}
        assert detached == [0]

    def test_matches_resolver_cached_address(self, monkeypatch):
        """A port attached via the resolver's cached connect address (e.g. the
        mDNS-fallback tier resolved something not among the static handles) is
        still ours - otherwise detach would silently no-op and leave it orphaned."""
        import pod.usbip as u
        monkeypatch.setattr(u, "ports", lambda: [
            {"port": 3, "remote": "fd00:dead:beef::9", "busid": "1-1"}])
        p = Pod(hostname="annealage-pod.local", addr4="192.168.0.146")
        # attach() resolved to a ULA that is not in the static handles
        p._resolver._resolved = "fd00:dead:beef::9"
        assert p.attached_ports() == [3]

    def test_matches_ipv6_by_interface_id_across_prefix_change(self, monkeypatch):
        """#3: the pod is known only by its old ULA, but the DUT is attached over
        a different ULA/global prefix sharing the pod's EUI-64 interface id. The
        exact-string compare misses it; the interface-id match still finds it, so
        detach does not silently no-op and run SWD under a live forward."""
        import pod.usbip as u
        monkeypatch.setattr(u, "ports", lambda: [
            {"port": 2, "remote": "fd99:aaaa:bbbb:0:2ecf:67ff:feb1:8946",
             "busid": "1-1"}])
        p = Pod(addr6=["fd32:7709:b6ad:0:2ecf:67ff:feb1:8946"])
        assert p.attached_ports() == [2]

    def test_matches_link_local_by_interface_id(self, monkeypatch):
        """A link-local attach matches by interface id even when only the ULA is
        a stored handle (both share the pod's EUI-64 id)."""
        import pod.usbip as u
        monkeypatch.setattr(u, "ports", lambda: [
            {"port": 4, "remote": "fe80::2ecf:67ff:feb1:8946", "busid": "1-1"}])
        p = Pod(addr6=["fd32:7709:b6ad:0:2ecf:67ff:feb1:8946"])
        assert p.attached_ports() == [4]

    def test_ignores_ipv6_with_different_interface_id(self, monkeypatch):
        """A different board (different MAC -> different interface id) is not
        matched, so detach never touches another pod's port."""
        import pod.usbip as u
        monkeypatch.setattr(u, "ports", lambda: [
            {"port": 0, "remote": "fd32:7709:b6ad:0:aaaa:bbbb:cccc:dddd",
             "busid": "1-1"}])
        p = Pod(addr6=["fd32:7709:b6ad:0:2ecf:67ff:feb1:8946"])
        assert p.attached_ports() == []

    def test_ipv4_only_pod_keeps_exact_match(self, monkeypatch):
        """A v4-only pod has no interface id, so matching stays exact - an
        unrelated IPv6 attach is never matched by an empty interface-id set."""
        import pod.usbip as u
        monkeypatch.setattr(u, "ports", lambda: [
            {"port": 1, "remote": "fe80::2ecf:67ff:feb1:8946", "busid": "1-1"}])
        p = Pod(addr4="192.168.0.146")
        assert p.attached_ports() == []


class TestMcpPodExec:
    def test_pod_exec_runs_on_pod(self, monkeypatch):
        import pod.mcp_server as m
        monkeypatch.setattr(m, "get_pod",
                            lambda label: {"addr4": "10.0.0.1", "repl_port": 8266})

        class FakePod:
            def exec(self, code):
                return "rp2\n"
        monkeypatch.setattr(m.Pod, "from_entry",
                            classmethod(lambda cls, e: FakePod()))
        assert m.handle_pod_exec("x", "print(1)") == "rp2\n"


class TestDutExecTurnkey:
    """The rebuild path. Each test pins forwarded_tty to None and attached_ports
    to []: dut_exec calls attached_ports() unconditionally before forwarded_tty
    ever sees its result, so pinning forwarded_tty alone still leaves a real
    `sudo usbip port` subprocess call in every test - pinning attached_ports
    too removes it, which is faster and (unlike pinning forwarded_tty alone)
    is not just "the result no longer depends on real host state" but "no real
    subprocess runs at all"."""

    @pytest.fixture(autouse=True)
    def _nothing_attached(self, monkeypatch):
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: None)
        monkeypatch.setattr(Pod, "attached_ports", lambda self: [])

    def test_flow_runs_mpremote_on_tty(self, monkeypatch):
        import pod.usbip as u
        monkeypatch.setattr('time.sleep', lambda *a: None)
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout="42\n", stderr=""))
        p = Pod(addr4="10.0.0.1", runner=runner)
        monkeypatch.setattr(u, "list_remote",
                            lambda host: [{"busid": "1-1", "vid": "f055", "pid": "9802"}])
        monkeypatch.setattr(p, "usbip_detach", lambda *a, **k: {"detached": []})
        monkeypatch.setattr(p, "usbip_attach",
                            lambda ensure=True: {"busid": "1-1", "tty": "/dev/ttyACM1"})
        res = p.dut_exec("print(6*7)")
        assert res["stdout"] == "42\n" and res["tty"] == "/dev/ttyACM1"
        argv = runner.call_args[0][0]
        assert argv[:5] == ["mpremote", "connect", "/dev/ttyACM1", "resume", "exec"]
        assert argv[5] == "print(6*7)"

    def test_raises_when_no_tty(self, monkeypatch):
        import pod.usbip as u
        monkeypatch.setattr('time.sleep', lambda *a: None)
        p = Pod(addr4="10.0.0.1", runner=MagicMock())
        monkeypatch.setattr(u, "list_remote", lambda host: [{"busid": "1-1"}])
        monkeypatch.setattr(p, "usbip_detach", lambda *a, **k: None)
        monkeypatch.setattr(p, "usbip_attach", lambda ensure=True: {"busid": "1-1", "tty": None})
        with pytest.raises(RuntimeError):
            p.dut_exec("print(1)")


class TestMcpDutExec:
    def test_dut_exec_delegates_to_turnkey(self, monkeypatch):
        import pod.mcp_server as m
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})

        class FakePod:
            def dut_exec(self, code):
                return {"tty": "/dev/ttyACM1", "stdout": "42\n",
                        "returncode": 0, "stderr": ""}
        monkeypatch.setattr(m.Pod, "from_entry",
                            classmethod(lambda cls, e: FakePod()))
        assert m.handle_dut_exec("x", "print(6*7)")["stdout"] == "42\n"


class TestAttachGuard:
    def _pod_and_detached(self, monkeypatch):
        import pod.usbip as u
        detached = []
        monkeypatch.setattr(u, "ports",
                            lambda: [{"port": 0, "remote": "192.168.0.146", "busid": "1-1"}])
        monkeypatch.setattr(u, "detach", lambda port: detached.append(port) or True)
        runner = MagicMock(return_value=MagicMock(returncode=0, stdout="{'ok': True}\n"))
        return Pod(addr4="192.168.0.146", runner=runner), detached

    def test_reset_detaches_live_attach(self, monkeypatch):
        p, detached = self._pod_and_detached(monkeypatch)
        p.reset_dut()
        assert detached == [0]

    def test_keep_attached_skips_detach(self, monkeypatch):
        p, detached = self._pod_and_detached(monkeypatch)
        p.reset_dut(keep_attached=True)
        assert detached == []

    def test_halt_detaches_live_attach(self, monkeypatch):
        p, detached = self._pod_and_detached(monkeypatch)
        p.halt_dut()
        assert detached == [0]

    def test_halt_keep_attached_skips_detach(self, monkeypatch):
        p, detached = self._pod_and_detached(monkeypatch)
        p.halt_dut(keep_attached=True)
        assert detached == []


class TestUsbipConflictGate:
    """Phase 6 anti-bump gate: flash/erase/reset/halt/reprobe refuse a live
    usbip session held by another host unless force=True bumps it."""

    def _pod(self, monkeypatch, held_by_other):
        from pod.client import Pod as _Pod
        evicted = []
        p = Pod(addr4="192.168.0.146",
                runner=MagicMock(return_value=MagicMock(
                    returncode=0, stdout="{'ok': True, 'ms': 1, 'err': None}\n")))
        monkeypatch.setattr(_Pod, "usbip_held_by_other", lambda self: held_by_other)
        monkeypatch.setattr(_Pod, "who",
                            lambda self, resource=None, timeout=3.0:
                                {"usbip": {"caller": "other@host/1"}})
        monkeypatch.setattr(_Pod, "attached_ports", lambda self: [])
        # This class is about the usbip anti-bump gate, not CMSIS algorithm
        # resolution: stand in for an already-installed algorithm so
        # flash_dut/erase_dut do not go looking for a pack.
        monkeypatch.setattr(_Pod, "ensure_flm_algo",
                            lambda self, **kw: {"installed": True})

        def _fake_exec(code):
            if "holders" in code and "evict" in code:
                evicted.append(code)
                return "ok\n"
            return "{'ok': True, 'ms': 1, 'mounted': 0, 'err': None}\n"
        monkeypatch.setattr(p, "exec", _fake_exec)
        return p, evicted

    # ── not held / unknown: fails open, no refusal, no stole_from ──────────

    def test_not_held_proceeds_and_carries_no_stole_from(self, monkeypatch):
        p, evicted = self._pod(monkeypatch, held_by_other=False)
        result = p.reset_dut()
        assert "stole_from" not in result
        assert evicted == []

    def test_unknown_fails_open(self, monkeypatch):
        # usbip_held_by_other() returns None when the pod cannot say (no
        # control port / unreachable) - must not be read as "held".
        p, evicted = self._pod(monkeypatch, held_by_other=None)
        result = p.reset_dut()
        assert "stole_from" not in result
        assert evicted == []

    # ── held by another: refuses, names the holder ─────────────────────────

    def test_reset_refuses_when_held_by_other(self, monkeypatch):
        from pod.client import PodConflictError
        p, evicted = self._pod(monkeypatch, held_by_other=True)
        with pytest.raises(PodConflictError) as excinfo:
            p.reset_dut()
        assert excinfo.value.resource == "usbip"
        assert excinfo.value.holder == {"caller": "other@host/1"}
        assert "other@host/1" in str(excinfo.value)
        assert evicted == []          # no bump attempted, so no eviction log

    def test_flash_refuses_when_held_by_other(self, monkeypatch, tmp_path):
        from pod.client import PodConflictError
        p, _ = self._pod(monkeypatch, held_by_other=True)
        image = tmp_path / "fw.bin"
        image.write_bytes(b"\x00" * 16)
        with pytest.raises(PodConflictError):
            p.flash_dut(str(image))

    def test_erase_refuses_when_held_by_other(self, monkeypatch):
        from pod.client import PodConflictError
        p, _ = self._pod(monkeypatch, held_by_other=True)
        with pytest.raises(PodConflictError):
            p.erase_dut()

    def test_halt_refuses_when_held_by_other(self, monkeypatch):
        from pod.client import PodConflictError
        p, _ = self._pod(monkeypatch, held_by_other=True)
        with pytest.raises(PodConflictError):
            p.halt_dut()

    def test_reprobe_refuses_when_held_by_other(self, monkeypatch):
        from pod.client import PodConflictError
        p, _ = self._pod(monkeypatch, held_by_other=True)
        with pytest.raises(PodConflictError):
            p.reprobe_dut()

    # ── force=True: bumps, logs the eviction, reports stole_from ───────────

    def test_reset_force_bumps_and_logs_eviction(self, monkeypatch):
        p, evicted = self._pod(monkeypatch, held_by_other=True)
        result = p.reset_dut(force=True)
        assert result["stole_from"] == {"caller": "other@host/1"}
        assert len(evicted) == 1
        assert "usbip" in evicted[0] and "other@host/1" in evicted[0]

    def test_erase_force_bumps_and_reports_stole_from(self, monkeypatch):
        p, evicted = self._pod(monkeypatch, held_by_other=True)
        result = p.erase_dut(force=True)
        assert result["stole_from"] == {"caller": "other@host/1"}
        assert len(evicted) == 1

    def test_reprobe_force_bumps_and_reports_stole_from(self, monkeypatch):
        p, evicted = self._pod(monkeypatch, held_by_other=True)
        result = p.reprobe_dut(force=True)
        assert result["stole_from"] == {"caller": "other@host/1"}
        assert len(evicted) == 1

    def test_flash_mass_erase_does_not_double_gate_or_double_evict(self, monkeypatch, tmp_path):
        # The internal mass_erase erase must not re-run the gate: the outer
        # flash_dut call already decided go/no-go, and a second gate check
        # would either re-refuse a forced bump or log a duplicate eviction.
        p, evicted = self._pod(monkeypatch, held_by_other=True)
        monkeypatch.setattr(p, "_stream_region",
                            lambda cmd, payload, size, port:
                                {"ok": True, "addr": 0, "bytes": size, "err": None})
        image = tmp_path / "fw.bin"
        image.write_bytes(b"\x00" * 16)
        result = p.flash_dut(str(image), mass_erase=True, force=True, verify=False)
        assert result["ok"] is True
        assert result["stole_from"] == {"caller": "other@host/1"}
        assert len(evicted) == 1      # exactly one eviction log, not two


class TestFlashDutUsesFlm:
    """flash_dut/_flash_dut_elf always resolve and install the DUT's CMSIS
    algorithm before flashing or erasing (see ensure_flm_algo); there is no
    other flash backend to thread through."""

    def _make_elf_pod(self, monkeypatch, fake_runner):
        """Pod with ELF geometry declared and elf_loader stubbed out."""
        import pod.elf_loader as el
        import pod.usbip as u
        monkeypatch.setattr(u, "ports", lambda: [])
        monkeypatch.setattr(el, "is_elf", lambda path: True)
        monkeypatch.setattr(el, "parse_load_segments",
                            lambda path, ranges: [(0x0, b"\xaa" * 8, "flash")])
        p = Pod(addr4="10.0.0.1", runner=fake_runner)
        p._elf_flash_ranges = [(0x0, 0x100000)]
        return p

    def test_elf_flash_resolves_the_algorithm_before_streaming(
            self, monkeypatch, fake_runner):
        p = self._make_elf_pod(monkeypatch, fake_runner)
        seen = {}
        monkeypatch.setattr(
            p, "ensure_flm_algo",
            lambda **kw: seen.update(kw) or {"installed": True})
        monkeypatch.setattr(
            p, "_stream_region",
            lambda cmd, payload, size, port: {"ok": True, "addr": 0,
                                              "bytes": size, "err": None})
        monkeypatch.setattr(p, "_verify_flashed", lambda *a, **k: {"ok": True})

        p.flash_dut("fake.elf")

        assert seen.get("addr") == 0x0

    def test_elf_mass_erase_happens_before_segments_stream(
            self, monkeypatch, fake_runner):
        # mass_erase=True on an ELF image must erase before any segment is
        # streamed; capture ordering via a shared events list.
        import pod.elf_loader as el
        import pod.usbip as u

        monkeypatch.setattr(u, "ports", lambda: [])
        monkeypatch.setattr(el, "is_elf", lambda path: True)
        monkeypatch.setattr(el, "parse_load_segments",
                            lambda path, ranges: [(0x0, b"\xaa" * 8, "flash")])

        p = Pod(addr4="10.0.0.1", runner=fake_runner)
        p._elf_flash_ranges = [(0x0, 0x100000)]
        p._flm_installed = {"installed": True, "name": "fake"}

        events = []
        monkeypatch.setattr(
            p, "_erase_all_on_pod",
            lambda *a, **k: events.append("erase") or {"ok": True})

        def _fake_stream(cmd, payload, size, port):
            events.append("stream")
            return {"ok": True, "addr": 0, "bytes": size, "err": None}

        monkeypatch.setattr(p, "_stream_region", _fake_stream)
        monkeypatch.setattr(p, "_verify_flashed", lambda *a, **k: {"ok": True})

        p.flash_dut("fake.elf", mass_erase=True)

        assert events == ["erase", "stream"]

    def test_flat_binary_target_reaches_flm_resolution(self, monkeypatch,
                                                        fake_runner):
        # Gap 7 of cmsis-flash-completion.md: flash_dut(target=...) used to be
        # accepted and silently read nowhere. It belongs in the pack lookup.
        import pod.usbip as u
        import tempfile, os

        monkeypatch.setattr(u, "ports", lambda: [])
        p = Pod(addr4="10.0.0.1", runner=fake_runner)
        seen = {}
        monkeypatch.setattr(
            p, "ensure_flm_algo",
            lambda **kw: seen.update(kw) or {"installed": True})
        monkeypatch.setattr(
            p, "_flash_region_chunked",
            lambda *a, **k: {"ok": True, "addr": 0, "bytes": 0, "err": None})

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"\x00" * 16)
            tmp = f.name
        try:
            p.flash_dut(tmp, target="STM32F407VG")
        finally:
            os.unlink(tmp)

        assert seen.get("device") == "STM32F407VG"

    def test_elf_target_reaches_flm_resolution(self, monkeypatch, fake_runner):
        p = self._make_elf_pod(monkeypatch, fake_runner)
        seen = {}
        monkeypatch.setattr(
            p, "ensure_flm_algo",
            lambda **kw: seen.update(kw) or {"installed": True})
        monkeypatch.setattr(
            p, "_flash_dut_elf",
            lambda *a, **k: {"ok": True, "segments": [], "bytes": 0, "err": None})

        p.flash_dut("fake.elf", target="STM32F407VG")

        assert seen.get("device") == "STM32F407VG"


class TestRecoverDutRepl:
    """recover_dut_repl drives the DUT tty (Ctrl-C then Ctrl-B) via a ReplSession;
    patch the session with a recording fake so the sequence + detection are tested
    without real hardware."""

    def _install_fake(self, monkeypatch, text, *, open_raises=None):
        events = []

        class FakeSession:
            def __init__(self, target, **kw):
                events.append(("init", target, kw.get("reconnect")))

            def open(self):
                if open_raises is not None:
                    raise open_raises
                return self

            def tell(self):
                return 0

            def interrupt(self):
                events.append(("interrupt",))

            def send(self, data, newline=True):
                events.append(("send", data, newline))
                return len(data)

            def read_since(self, cursor):
                return {"text": text, "cursor": len(text), "dropped": 0}

            def close(self):
                events.append(("close",))
                return {"ok": True}

        import pod.session
        monkeypatch.setattr(pod.session, "ReplSession", FakeSession)
        return events

    def test_sends_ctrlc_then_ctrlb_and_detects_prompt(self, monkeypatch):
        events = self._install_fake(monkeypatch, "\r\nMicroPython v1.29\r\n>>> ")
        p = Pod(address=ADDRESS, repl_port=PORT)
        res = p.recover_dut_repl("/dev/ttyACM9", settle=0, read_wait=0)
        assert res["ok"] is True
        assert res["recovered"] is True
        assert res["prompt_seen"] is True
        # Ctrl-C (interrupt), a CR to probe whether the friendly REPL answers,
        # then Ctrl-B (0x02) to leave raw mode, then a CR to draw a fresh prompt.
        # The probe has to precede the Ctrl-B: Ctrl-B answers with the friendly
        # banner from either mode, so only the probe distinguishes them.
        ops = [e for e in events if e[0] in ("interrupt", "send")]
        assert ops[0] == ("interrupt",)
        assert ops[1] == ("send", b"\r", False)
        assert ops[2] == ("send", b"\x02", False)
        assert ops[3] == ("send", b"\r", False)
        assert ("close",) in events            # tty released (DTR restored to opener)
        # Opened non-reconnecting on the given device.
        assert events[0] == ("init", "/dev/ttyACM9", False)

    def test_reports_raw_banner_without_prompt_as_not_recovered(self, monkeypatch):
        self._install_fake(monkeypatch, "raw REPL; CTRL-B to exit\r\n")
        p = Pod(address=ADDRESS, repl_port=PORT)
        res = p.recover_dut_repl("/dev/ttyACM9", settle=0, read_wait=0)
        assert res["was_raw"] is True
        assert res["recovered"] is False       # no friendly '>>>' came back

    def test_busy_or_absent_tty_returns_error(self, monkeypatch):
        self._install_fake(monkeypatch, "", open_raises=OSError("device busy"))
        p = Pod(address=ADDRESS, repl_port=PORT)
        res = p.recover_dut_repl("/dev/ttyACM9")
        assert res["ok"] is False
        assert "could not open" in res["err"]


class TestFlashVerify:
    """End-to-end flash verify (task #11): _verify_flashed CRC-compares a
    just-flashed region against the source, with retries so a flaky read cannot
    false-fail. flash_dut wires it in so a mid-stream chunk drop fails loudly."""

    def _pod_with_crc(self, crc_returns):
        """Pod whose flash_crc yields the given result dicts in sequence."""
        import zlib  # noqa: F401 - referenced by callers building expectations
        p = Pod(address=ADDRESS, repl_port=PORT)
        seq = list(crc_returns)
        calls = []

        def fake_crc(addr, length, clkdiv=8):
            calls.append((addr, length))
            return seq.pop(0)

        p.flash_crc = fake_crc
        p._crc_calls = calls
        return p

    def test_verify_passes_on_match(self):
        import zlib
        src = b"firmware-bytes-example" * 8
        want = zlib.crc32(src) & 0xFFFFFFFF
        p = self._pod_with_crc([{"ok": True, "crc": want}])
        v = p._verify_flashed(0x1000, src)
        assert v["ok"] is True and v["crc"] == want

    def test_verify_fails_on_persistent_mismatch(self):
        src = b"x" * 100
        bad = 0xDEADBEEF
        p = self._pod_with_crc([{"ok": True, "crc": bad}] * 3)
        v = p._verify_flashed(0x1000, src, retries=3)
        assert v["ok"] is False
        assert "crc mismatch" in v["err"]
        assert len(p._crc_calls) == 3   # exhausted all retries

    def test_verify_passes_when_a_retry_reads_clean(self):
        import zlib
        src = b"data" * 50
        want = zlib.crc32(src) & 0xFFFFFFFF
        # first read flaky (wrong crc), second read clean (matches)
        p = self._pod_with_crc([{"ok": True, "crc": 0x1234},
                                {"ok": True, "crc": want}])
        v = p._verify_flashed(0x2000, src, retries=3)
        assert v["ok"] is True
        assert len(p._crc_calls) == 2   # stopped as soon as it matched

    def test_verify_tolerates_read_error_then_matches(self):
        import zlib
        src = b"seg" * 40
        want = zlib.crc32(src) & 0xFFFFFFFF
        p = self._pod_with_crc([{"ok": False, "err": "flaky read"},
                                {"ok": True, "crc": want}])
        v = p._verify_flashed(0x3000, src, retries=3)
        assert v["ok"] is True

    def test_flash_dut_flat_fails_when_verify_mismatches(self, monkeypatch, tmp_path):
        # A mid-stream drop: streaming reports ok, but the read-back CRC never
        # matches -> flash_dut must return ok=False naming the verify failure.
        import pod.elf_loader as el
        import pod.usbip as u
        monkeypatch.setattr(u, "ports", lambda: [])
        monkeypatch.setattr(el, "is_elf", lambda path: False)
        img = tmp_path / "fw.bin"
        img.write_bytes(b"\xa5" * 4096)
        p = Pod(addr4="10.0.0.1")
        monkeypatch.setattr(p, "ensure_flm_algo", lambda **kw: {"installed": True})
        monkeypatch.setattr(p, "_stream_region",
                            lambda cmd, payload, size, port: {"ok": True, "addr": 0, "bytes": size})
        monkeypatch.setattr(p, "flash_crc",
                            lambda addr, length, clkdiv=8: {"ok": True, "crc": 0x0})  # never matches
        res = p.flash_dut(str(img))
        assert res["ok"] is False
        assert "end-to-end verify" in res["err"]
        assert res["verify"]["ok"] is False

    def test_flash_dut_flat_passes_when_verify_matches(self, monkeypatch, tmp_path):
        import zlib
        import pod.elf_loader as el
        import pod.usbip as u
        monkeypatch.setattr(u, "ports", lambda: [])
        monkeypatch.setattr(el, "is_elf", lambda path: False)
        data = b"\xa5" * 4096
        img = tmp_path / "fw.bin"
        img.write_bytes(data)
        want = zlib.crc32(data) & 0xFFFFFFFF
        p = Pod(addr4="10.0.0.1")
        monkeypatch.setattr(p, "ensure_flm_algo", lambda **kw: {"installed": True})
        monkeypatch.setattr(p, "_stream_region",
                            lambda cmd, payload, size, port: {"ok": True, "addr": 0, "bytes": size})
        monkeypatch.setattr(p, "flash_crc",
                            lambda addr, length, clkdiv=8: {"ok": True, "crc": want})
        res = p.flash_dut(str(img))
        assert res["ok"] is True
        assert res["verify"]["ok"] is True

    def test_flash_dut_skips_verify_when_disabled(self, monkeypatch, tmp_path):
        import pod.elf_loader as el
        import pod.usbip as u
        monkeypatch.setattr(u, "ports", lambda: [])
        monkeypatch.setattr(el, "is_elf", lambda path: False)
        img = tmp_path / "fw.bin"
        img.write_bytes(b"\x00" * 512)
        p = Pod(addr4="10.0.0.1")
        monkeypatch.setattr(p, "ensure_flm_algo", lambda **kw: {"installed": True})
        monkeypatch.setattr(p, "_stream_region",
                            lambda cmd, payload, size, port: {"ok": True, "addr": 0, "bytes": size})
        called = []
        monkeypatch.setattr(p, "flash_crc",
                            lambda addr, length, clkdiv=8: called.append(1) or {"ok": True, "crc": 0})
        res = p.flash_dut(str(img), verify=False)
        assert res["ok"] is True
        assert called == []          # verify=False -> no read-back CRC
        assert "verify" not in res


class TestFlashChunking:
    """<=64KB page-aligned sub-flashes (task #35): _flash_region_chunked splits a
    large region so no single flash op starves the pod loop, with boundaries on
    absolute 64KB multiples so no two sub-flashes share a flash page."""

    def _chunk_pod(self):
        p = Pod(address=ADDRESS, repl_port=PORT)
        subs = []

        def fake_cmd(a, n, port, verify, caller=None):
            subs.append((a, n))
            return "CMD"

        p._flash_stream_cmd = fake_cmd
        p._stream_region = lambda cmd, payload, size, port: {"ok": True,
                                                             "addr": 0, "bytes": size}
        p._verify_flashed = lambda lma, source, **k: {"ok": True}
        p._subs = subs
        return p

    def test_small_region_single_subflash(self):
        p = self._chunk_pod()
        r = p._flash_region_chunked(0x0, b"\x00" * 4096, 3333, True)
        assert r["ok"] is True
        assert p._subs == [(0x0, 4096)]

    def test_large_aligned_region_splits_at_64k(self):
        p = self._chunk_pod()
        data = b"\x00" * (150 * 1024)
        r = p._flash_region_chunked(0x0, data, 3333, True)
        assert r["ok"] is True
        assert p._subs == [(0x0, 65536), (0x10000, 65536), (0x20000, 22528)]
        assert all(n <= 64 * 1024 for _, n in p._subs)
        assert all(a % (64 * 1024) == 0 for a, _ in p._subs)

    def test_unaligned_start_boundaries_stay_64k_aligned(self):
        p = self._chunk_pod()
        data = b"\x00" * (80 * 1024)
        start = 0x1000
        r = p._flash_region_chunked(start, data, 3333, True)
        assert r["ok"] is True
        # first sub-flash runs only to the next 64KB boundary
        assert p._subs[0] == (0x1000, 0x10000 - 0x1000)
        assert all(n <= 64 * 1024 for _, n in p._subs)
        for a, _ in p._subs[1:]:
            assert a % (64 * 1024) == 0          # no shared page at a boundary
        # sub-flashes tile [start, start+len) exactly, no gap/overlap
        pos = start
        for a, n in p._subs:
            assert a == pos
            pos += n
        assert pos == start + len(data)

    def test_subflash_failure_stops_and_names_addr(self):
        p = self._chunk_pod()
        calls = []

        def failing_stream(cmd, payload, size, port):
            calls.append(size)
            return {"ok": len(calls) < 2, "err": "reset"}   # 2nd sub-flash fails

        p._stream_region = failing_stream
        r = p._flash_region_chunked(0x0, b"\x00" * (100 * 1024), 3333, True)
        assert r["ok"] is False
        assert "0x00010000" in r["err"]          # the failing sub-flash address

    def test_verify_runs_once_over_whole_region(self):
        p = self._chunk_pod()
        vcalls = []
        p._verify_flashed = lambda lma, source, **k: (
            vcalls.append((lma, len(source))) or {"ok": True})
        data = b"\xaa" * (130 * 1024)
        r = p._flash_region_chunked(0x0, data, 3333, True)
        assert r["ok"] is True
        assert vcalls == [(0x0, 130 * 1024)]     # one verify, whole region


class TestAmpremoteResolution:
    """pod.client must shell out to the ampremote that ships with its own
    interpreter, so the subprocess transport and pod.session's in-process
    mpremote import are the same distribution. Resolving through PATH instead
    can silently put the two on different code, which is the split this
    resolution exists to prevent.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from pod import client
        saved = client._AMPREMOTE_EXE
        client._AMPREMOTE_EXE = None
        yield
        client._AMPREMOTE_EXE = saved

    def test_prefers_the_cli_beside_sys_executable(self, monkeypatch, tmp_path):
        from pod import client
        exe = tmp_path / "ampremote"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        monkeypatch.setattr(client.sys, "executable", str(tmp_path / "python3"))
        assert client._ampremote_exe() == str(exe)

    def test_falls_back_to_path_when_absent(self, monkeypatch, tmp_path):
        from pod import client
        monkeypatch.setattr(client.sys, "executable", str(tmp_path / "python3"))
        assert client._ampremote_exe() == "ampremote"

    def test_non_executable_neighbour_is_not_used(self, monkeypatch, tmp_path):
        # A same-named file without the exec bit must not be selected.
        from pod import client
        (tmp_path / "ampremote").write_text("not executable")
        monkeypatch.setattr(client.sys, "executable", str(tmp_path / "python3"))
        assert client._ampremote_exe() == "ampremote"

    def test_result_is_cached(self, monkeypatch, tmp_path):
        from pod import client
        monkeypatch.setattr(client.sys, "executable", str(tmp_path / "python3"))
        first = client._ampremote_exe()
        exe = tmp_path / "ampremote"          # appears after the first call
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        assert client._ampremote_exe() == first

    def test_argv_uses_the_resolved_cli(self, monkeypatch, tmp_path):
        from pod import client
        exe = tmp_path / "ampremote"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        monkeypatch.setattr(client.sys, "executable", str(tmp_path / "python3"))
        client._AMPREMOTE_EXE = None
        p = Pod(addr4="10.0.0.1")
        assert p._argv("exec", "x")[0] == str(exe)
class TestFlmAlgoInstall:
    """Shipping a CMSIS algorithm to the pod.

    The image goes over the REPL in base64 chunks (a vendor algorithm is tens
    of KB and must not become one huge source literal on the pod), then the
    metadata installs it. These cover the chunking, the transfer check, and the
    caching that stops a mass-erase-then-flash re-shipping the image.
    """

    def _algo(self, size=32):
        return {
            "name": "TESTDEV",
            "instructions": bytes(range(256)) * (size // 256 + 1),
            "load_address": 0x20000000,
            "page_size": 0x1000,
            "flash_base": 0x0,
            "flash_size": 0x100000,
            "sectors": [(0x0, 0x1000)],
        }

    def _pod(self, monkeypatch, staged_reply=None):
        p = Pod(addr4="10.0.0.1")
        sent = []

        def _fake_exec(code):
            sent.append(code)
            if "stage_flm_blob" in code:
                return "%d\n" % (staged_reply if staged_reply is not None
                                 else _fake_exec.staged)
            return "{'installed': True, 'name': 'TESTDEV', 'blob_bytes': 1}\n"

        _fake_exec.staged = 0
        monkeypatch.setattr(p, "exec", _fake_exec)
        return p, sent, _fake_exec

    def test_image_is_staged_in_chunks_then_installed(self, monkeypatch):
        algo = self._algo(size=20000)
        p, sent, fx = self._pod(monkeypatch)
        fx.staged = len(algo["instructions"])

        info = p.install_flm_algo(algo)

        stages = [c for c in sent if "stage_flm_blob" in c]
        # one reset call with no argument, then one per base64 chunk
        assert stages[0].endswith("stage_flm_blob())")
        expected_chunks = -(-len(_b64_of(algo)) // Pod._FLM_B64_CHUNK)
        assert len(stages) == 1 + expected_chunks
        assert info["installed"] is True

    def test_metadata_is_installed_without_the_image(self, monkeypatch):
        algo = self._algo()
        p, sent, fx = self._pod(monkeypatch)
        fx.staged = len(algo["instructions"])

        p.install_flm_algo(algo)

        install = [c for c in sent if "set_flm_algo" in c]
        assert len(install) == 1
        # The image never appears in the install call; it was staged already.
        assert "instructions" not in install[0]
        assert "'page_size': 4096" in install[0]
        assert "'sectors': [(0, 4096)]" in install[0]

    def test_short_transfer_is_refused(self, monkeypatch):
        algo = self._algo()
        p, sent, fx = self._pod(monkeypatch, staged_reply=3)
        with pytest.raises(RuntimeError, match="transfer incomplete"):
            p.install_flm_algo(algo)

    def test_ensure_uses_the_pod_when_it_already_has_one(self, monkeypatch):
        p = Pod(addr4="10.0.0.1")
        monkeypatch.setattr(
            p, "exec",
            lambda code: "{'installed': True, 'name': 'ALREADY'}\n")
        monkeypatch.setattr(p, "resolve_flm_algo",
                            lambda **k: pytest.fail("should not resolve a pack"))
        assert p.ensure_flm_algo()["name"] == "ALREADY"

    def test_ensure_is_free_after_the_first_install(self, monkeypatch):
        p = Pod(addr4="10.0.0.1")
        calls = []
        monkeypatch.setattr(p, "exec", lambda code: calls.append(code) or
                            "{'installed': False}\n")
        monkeypatch.setattr(p, "resolve_flm_algo", lambda **k: self._algo())
        monkeypatch.setattr(p, "install_flm_algo",
                            lambda algo: {"installed": True, "name": "X"})

        p.ensure_flm_algo()
        before = len(calls)
        p.ensure_flm_algo()
        p.ensure_flm_algo()
        assert len(calls) == before, "cached install still talked to the pod"

    def test_ensure_re_resolves_when_a_named_device_disagrees_with_the_cache(
            self, monkeypatch):
        # Gap 7's own failure mode surviving inside the fix meant to close it:
        # a device= that disagrees with what is cached/installed must not be
        # silently ignored (naming a device is a request for that one, not a
        # hint the caller thinks force= might be needed) - otherwise a fresh
        # `pod dut flash --target X` process can flash a real DUT
        # with the wrong part's algorithm with no error.
        p = Pod(addr4="10.0.0.1")
        p._flm_installed = {"installed": True, "name": "nRF52840_xxAA"}
        monkeypatch.setattr(
            p, "exec", lambda code: pytest.fail("should not ask the pod - "
                                                "the client's own cache disagreed"))
        resolved = {}
        monkeypatch.setattr(
            p, "resolve_flm_algo",
            lambda **k: resolved.update(k) or {"name": k.get("device")})
        monkeypatch.setattr(p, "install_flm_algo",
                            lambda algo: {"installed": True, **algo})

        result = p.ensure_flm_algo(device="STM32F407VG")

        assert resolved.get("device") == "STM32F407VG"
        assert result["name"] == "STM32F407VG"

    def test_ensure_reuses_the_cache_when_the_named_device_matches(
            self, monkeypatch):
        p = Pod(addr4="10.0.0.1")
        p._flm_installed = {"installed": True, "name": "nRF52840_xxAA"}
        monkeypatch.setattr(
            p, "resolve_flm_algo",
            lambda **k: pytest.fail("should have reused the matching cache"))

        result = p.ensure_flm_algo(device="nRF52840_xxAA")

        assert result == {"installed": True, "name": "nRF52840_xxAA"}

    def _declared_pod(self, monkeypatch, pod_reports):
        from pod import cmsis_pack
        p = Pod.from_entry({"addr4": "10.0.0.1", "dut": {
            "target_family": "MIMXRT1052xxxxB",
            "flash_algorithm": "MIMXRT105x_QuadSPI_4KB_SEC"}})
        monkeypatch.setattr(p, "exec", lambda code: repr(pod_reports) + "\n")
        resolved = {}
        monkeypatch.setattr(
            cmsis_pack, "algo_for_device",
            lambda device, **k: resolved.update(device=device, **k) or
            {"name": device, "algorithm": k.get("algorithm"),
             "flash_base": 0x60000000, "flash_size": 0x800000})
        monkeypatch.setattr(p, "install_flm_algo",
                            lambda algo: {"installed": True, **algo})
        return p, resolved

    def test_ensure_replaces_a_pod_algorithm_other_than_the_declared_one(
            self, monkeypatch):
        # The pod holds the pack default (HyperFlash) from an earlier session;
        # running it against the board's QSPI part would fail or corrupt it.
        p, resolved = self._declared_pod(monkeypatch, {
            "installed": True, "name": "MIMXRT1052xxxxB",
            "algorithm": "MIMXRT105x_HYPER_256KB_SEC"})

        result = p.ensure_flm_algo()

        assert resolved["algorithm"] == "MIMXRT105x_QuadSPI_4KB_SEC"
        assert result["algorithm"] == "MIMXRT105x_QuadSPI_4KB_SEC"

    def test_ensure_keeps_a_pod_algorithm_matching_the_declared_one(
            self, monkeypatch):
        installed = {"installed": True, "name": "MIMXRT1052xxxxB",
                     "algorithm": "mimxrt105x_quadspi_4kb_sec"}
        p, resolved = self._declared_pod(monkeypatch, installed)

        assert p.ensure_flm_algo() == installed
        assert not resolved

    def test_declared_algorithm_does_not_follow_another_named_device(
            self, monkeypatch):
        p, resolved = self._declared_pod(monkeypatch, {"installed": False})

        p.ensure_flm_algo(device="nRF52840_xxAA")

        assert resolved["device"] == "nRF52840_xxAA"
        assert resolved["algorithm"] is None

    def test_ensure_always_resolves_fresh_when_an_explicit_pack_is_named(
            self, monkeypatch):
        # An explicit pack has no comparable field in the installed summary
        # to check for a match, so naming one always resolves fresh rather
        # than risk silently reusing an unrelated cached algorithm.
        p = Pod(addr4="10.0.0.1")
        p._flm_installed = {"installed": True, "name": "nRF52840_xxAA"}
        resolved = {}
        monkeypatch.setattr(
            p, "resolve_flm_algo",
            lambda **k: resolved.update(k) or {"name": "from-pack"})
        monkeypatch.setattr(p, "install_flm_algo",
                            lambda algo: {"installed": True, **algo})

        result = p.ensure_flm_algo(pack="/path/to/x.pack")

        assert resolved.get("pack") == "/path/to/x.pack"
        assert result["name"] == "from-pack"

    def test_resolve_needs_a_declared_device(self, monkeypatch):
        p = Pod(addr4="10.0.0.1")
        with pytest.raises(ValueError, match="no CMSIS device name"):
            p.resolve_flm_algo()

    def test_resolve_uses_the_registry_target_family(self, monkeypatch):
        from pod import cmsis_pack
        p = Pod.from_entry({"address": "10.0.0.1",
                            "dut": {"target_family": "nRF52840_xxAA"}})
        seen = {}
        monkeypatch.setattr(cmsis_pack, "algo_for_device",
                            lambda device, **kw: seen.update(
                                device=device, **kw) or {"name": device})
        p.resolve_flm_algo(addr=0x1000)
        assert seen["device"] == "nRF52840_xxAA"
        assert seen["addr"] == 0x1000
        assert seen["allow_download"] is False

    def test_resolve_clamps_flash_geometry_to_the_registry_when_declared(
            self, monkeypatch):
        # Gap 4 of cmsis-flash-completion.md: the registry describes the actual
        # part on the bench, the pack's own geometry only lays out the
        # algorithm - a pack that over-declares its size must not let
        # erase_all's sector sweep run past the end of real flash.
        from pod import cmsis_pack
        p = Pod.from_entry({"address": "10.0.0.1",
                            "dut": {"target_family": "nRF52840_xxAA",
                                    "flash_base": "0x0", "flash_size": "0x100000"}})
        monkeypatch.setattr(
            cmsis_pack, "algo_for_device",
            lambda device, **kw: {"name": device, "flash_base": 0x0,
                                  "flash_size": 0x200000})

        algo = p.resolve_flm_algo()

        assert algo["flash_base"] == 0x0
        assert algo["flash_size"] == 0x100000     # the registry's 1 MB, not the pack's 2 MB

    def test_resolve_leaves_pack_geometry_alone_without_a_registry_range(
            self, monkeypatch):
        from pod import cmsis_pack
        p = Pod.from_entry({"address": "10.0.0.1",
                            "dut": {"target_family": "nRF52840_xxAA"}})
        monkeypatch.setattr(
            cmsis_pack, "algo_for_device",
            lambda device, **kw: {"name": device, "flash_base": 0x0,
                                  "flash_size": 0x200000})

        algo = p.resolve_flm_algo()

        assert algo["flash_size"] == 0x200000

    def test_resolve_leaves_a_different_base_algorithm_untouched(
            self, monkeypatch):
        # A sub-region algorithm at a different base than the registry's main
        # flash (UICR, OTP, a second bank picked via addr=) describes memory
        # the registry's single declared range has no opinion on. Clamping it
        # anyway (as the first version of this fix did) rewrote a 4 KB UICR
        # algorithm to claim 1 MB of main flash on the real nRF52840 pack,
        # and would index its own sector map against the wrong origin on any
        # part where the map is non-uniform.
        from pod import cmsis_pack
        p = Pod.from_entry({"address": "10.0.0.1",
                            "dut": {"target_family": "nRF52840_xxAA",
                                    "flash_base": "0x0", "flash_size": "0x100000"}})
        monkeypatch.setattr(
            cmsis_pack, "algo_for_device",
            lambda device, **kw: {"name": device, "flash_base": 0x10001000,
                                  "flash_size": 0x1000,
                                  "sectors": [(0, 0x1000)]})

        algo = p.resolve_flm_algo(addr=0x10001000)

        assert algo["flash_base"] == 0x10001000
        assert algo["flash_size"] == 0x1000


def _b64_of(algo):
    import base64
    return base64.b64encode(algo["instructions"]).decode("ascii")


# ── a link this host already holds is reused, not rebuilt ──────────────────


class TestForwardedLinkIsReused:
    """The pod's usbip server allows one import per busid, so rebuilding a link
    this host already holds is both refused and pointless. Both the attach and
    the exec path have to notice."""

    def _pod(self, runner=None):
        return Pod(address=ADDRESS, repl_port=PORT, runner=runner)

    def test_usbip_attach_returns_the_held_attachment(self, monkeypatch):
        p = self._pod()
        monkeypatch.setattr(p, "attached_ports", lambda: [0])
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: "/dev/ttyACM6")
        monkeypatch.setattr("pod.usbip.ports",
                            lambda **k: [{"port": 0, "remote": "x",
                                          "busid": "1-1"}])
        monkeypatch.setattr("pod.usbip.ensure_server",
                            lambda *a, **k: pytest.fail("touched the pod REPL"))
        monkeypatch.setattr("pod.usbip.attach",
                            lambda *a, **k: pytest.fail("re-imported"))

        dev = p.usbip_attach()

        assert dev["already_attached"] is True
        assert dev["tty"] == "/dev/ttyACM6"
        assert dev["busid"] == "1-1"
        assert dev["port"] == 0

    def test_dut_exec_uses_the_held_tty_without_rebuilding(self, monkeypatch):
        runs = []

        def runner(argv, **kw):
            runs.append(argv)
            return SimpleNamespace(returncode=0, stdout="54\n", stderr="")

        p = self._pod(runner)
        # Unmocked, this calls attached_ports() -> usbip.ports() -> a real
        # `sudo usbip port` subprocess. That alone was a pre-existing flake:
        # time.sleep is patched process-wide below (pod.client.time IS the
        # shared time module, not a copy), so CPython's own subprocess.Popen
        # timeout-poll loop (Lib/subprocess.py's _wait, exponential-backoff
        # time.sleep calls while reaping the child) got swept into `slept`
        # whenever the real subprocess took a few OS-scheduler ticks longer to
        # reap under load on a shared machine - intermittent, and nothing to
        # do with the code under test. forwarded_tty is already faked to
        # ignore its argument, so the value here is arbitrary.
        monkeypatch.setattr(p, "attached_ports", lambda: [0])
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: "/dev/ttyACM6")
        slept = []
        monkeypatch.setattr("pod.client.time.sleep", lambda s: slept.append(s))
        monkeypatch.setattr(p, "usbip_detach",
                            lambda *a, **k: pytest.fail("detached a live link"))
        monkeypatch.setattr(p, "usbip_attach",
                            lambda *a, **k: pytest.fail("re-attached"))

        res = p.dut_exec("print(6*9)")

        assert res["reattached"] is False
        assert res["tty"] == "/dev/ttyACM6"
        assert slept == []                      # none of the settle delays run
        assert runs[0][:3] == ["mpremote", "connect", "/dev/ttyACM6"]

    def test_dut_exec_rebuilds_when_nothing_is_attached(self, monkeypatch):
        def runner(argv, **kw):
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        p = self._pod(runner)
        # Same gap as the sibling test above: unmocked, this calls a real
        # `sudo usbip port` subprocess for a result forwarded_tty ignores
        # anyway (it is faked to always report nothing attached).
        monkeypatch.setattr(p, "attached_ports", lambda: [])
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: None)
        monkeypatch.setattr("pod.usbip.list_remote", lambda *a, **k: [{"busid": "1-1"}])
        monkeypatch.setattr("pod.client.time.sleep", lambda s: None)
        monkeypatch.setattr(p, "usbip_detach", lambda *a, **k: {"detached": []})
        monkeypatch.setattr(p, "usbip_attach",
                            lambda *a, **k: {"tty": "/dev/ttyACM9"})

        res = p.dut_exec("print(1)")

        assert res["reattached"] is True
        assert res["tty"] == "/dev/ttyACM9"


# ── a forwarded tty is scoped to the pod that forwarded it ─────────────────


class TestForwardedTtyIsPodScoped:
    """Every forwarded device looks alike from a device node, so an unscoped
    lookup would hand one pod's caller another pod's DUT. The vhci status table
    maps a port to the local bus address the tty's own sysfs path spells, which
    is the link between them."""

    STATUS = (
        "hub port sta spd dev      sockfd local_busid\n"
        "hs  0000 006 002 00010001 000003 5-1\n"
        "hs  0001 006 002 00010002 000004 5-2\n"
        "hs  0002 004 000 00000000 000000 0-0\n"
    )

    def test_parses_only_ports_in_use(self):
        from pod import usbip
        assert usbip.parse_vhci_status(self.STATUS) == {0: "5-1", 1: "5-2"}

    def test_empty_and_garbage_are_tolerated(self):
        from pod import usbip
        assert usbip.parse_vhci_status("") == {}
        assert usbip.parse_vhci_status("header only\n") == {}
        assert usbip.parse_vhci_status("hub port\nnot a row\n") == {}

    def test_no_ports_means_no_tty(self):
        from pod import usbip
        assert usbip.forwarded_tty([], _status={0: "5-1"}) is None
        assert usbip.forwarded_tty(None, _status={0: "5-1"}) is None

    def test_a_port_this_pod_does_not_hold_is_not_matched(self):
        from pod import usbip
        # port 7 is not in the status table at all
        assert usbip.forwarded_tty([7], _status={0: "5-1"}) is None


class TestUsbipAttachShape:
    def test_already_attached_carries_the_same_keys_as_a_fresh_attach(self, monkeypatch):
        p = Pod(address=ADDRESS, repl_port=PORT)
        monkeypatch.setattr(p, "attached_ports", lambda: [0])
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: "/dev/ttyACM6")
        monkeypatch.setattr("pod.usbip.ports",
                            lambda **k: [{"port": 0, "remote": "x", "busid": "1-1"}])
        monkeypatch.setattr("pod.usbip.list_remote",
                            lambda *a, **k: [{"busid": "1-1", "vid": "f055",
                                              "pid": "9802"}])
        dev = p.usbip_attach()
        # the CLI prints dev['vid']/dev['pid'] outside its try block, so a missing
        # key here is an unhandled traceback rather than a message
        for key in ("busid", "vid", "pid", "tty"):
            assert key in dev, key
        assert dev["already_attached"] is True

    def test_ids_are_read_locally_without_touching_the_pod(self, monkeypatch):
        """The ids come from the tty's own sysfs node. Querying the pod for them
        would put a round trip on the one route whose purpose is to touch it."""
        p = Pod(address=ADDRESS, repl_port=PORT)
        monkeypatch.setattr(p, "attached_ports", lambda: [0])
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: "/dev/ttyACM6")
        monkeypatch.setattr("pod.usbip.ports", lambda **k: [])
        monkeypatch.setattr("pod.usbip.tty_usb_ids", lambda tty: ("f055", "9802"))
        monkeypatch.setattr("pod.usbip.list_remote",
                            lambda *a, **k: pytest.fail("queried the pod"))
        dev = p.usbip_attach()
        assert (dev["vid"], dev["pid"]) == ("f055", "9802")

    def test_ids_absent_rather_than_fatal_when_sysfs_says_nothing(self, monkeypatch):
        p = Pod(address=ADDRESS, repl_port=PORT)
        monkeypatch.setattr(p, "attached_ports", lambda: [0])
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: "/dev/ttyACM6")
        monkeypatch.setattr("pod.usbip.ports", lambda **k: [])
        monkeypatch.setattr("pod.usbip.tty_usb_ids", lambda tty: (None, None))
        dev = p.usbip_attach()
        assert dev["vid"] is None and dev["tty"] == "/dev/ttyACM6"


class TestStaleForwardedTtyIsRebuilt:
    """A device node surviving a DUT re-enumeration is the stale-export case; the
    reuse fast path must not turn that into a reported DUT error."""

    def test_a_reused_tty_that_fails_is_rebuilt_once(self, monkeypatch):
        calls = []

        def runner(argv, **kw):
            calls.append(argv[2])
            if argv[2] == "/dev/stale":
                # what a vanished device looks like, not a DUT-side error
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr="failed to access /dev/stale")
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        p = Pod(address=ADDRESS, repl_port=PORT, runner=runner)
        monkeypatch.setattr("pod.client.time.sleep", lambda s: None)
        monkeypatch.setattr(p, "attached_ports", lambda: [0])
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: "/dev/stale")
        monkeypatch.setattr(p, "_rebuild_dut_link", lambda: "/dev/fresh")

        res = p.dut_exec("print(1)")

        assert "/dev/stale" in calls and "/dev/fresh" in calls
        assert res["tty"] == "/dev/fresh"
        assert res["reattached"] is True
        assert res["returncode"] == 0

    def test_a_dut_side_exception_does_not_rebuild_or_rerun(self, monkeypatch):
        """A traceback is a working link running failing code. Rebuilding on it
        would tear the link down and execute the caller's code a second time."""
        calls = []

        def runner(argv, **kw):
            calls.append(argv[2])
            return SimpleNamespace(
                returncode=1, stdout="",
                stderr="Traceback (most recent call last):\n  ZeroDivisionError")

        p = Pod(address=ADDRESS, repl_port=PORT, runner=runner)
        monkeypatch.setattr("pod.client.time.sleep", lambda s: None)
        monkeypatch.setattr(p, "attached_ports", lambda: [0])
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: "/dev/ttyACM6")
        monkeypatch.setattr(p, "_rebuild_dut_link",
                            lambda: pytest.fail("rebuilt on a DUT-side error"))

        res = p.dut_exec("1/0")

        assert calls == ["/dev/ttyACM6"]        # ran exactly once
        assert res["returncode"] == 1
        assert "ZeroDivisionError" in res["stderr"]



# ── a busy pod is not a broken pod ─────────────────────────────────────────


class TestBusyIsNotAFault:
    """The documented response to an unreachable pod is reset and power-cycle.
    Against a pod that is merely in use, that destroys another agent's session,
    so contention has to be classified apart from a transport fault."""

    def test_the_pods_own_busy_line_is_recognised(self):
        from pod.client import _classify_exec_failure as c
        reason = c("annealage-pod: BUSY - REPL in use by another client", "")
        assert "busy" in reason.lower()

    def test_a_dropped_connection_reads_as_contention_not_a_fault(self):
        """The pod closes the socket on a second REPL client, which is what an
        agent actually sees when it collides."""
        from pod.client import _classify_exec_failure as c
        reason = c("read failed: [Errno 104] Connection reset by peer", "")
        assert "already in use" in reason
        assert "raw-REPL entry" not in reason

    def test_genuine_transport_faults_are_still_transport_faults(self):
        from pod.client import _classify_exec_failure as c
        assert c("could not enter raw repl", "") == \
            "raw-REPL entry / connection failed"
        assert c("operation timed out", "") == "timeout reaching the pod"
        assert c("Traceback ... ZeroDivisionError", "") == "exception on the pod"

    def test_the_busy_message_warns_against_resetting(self):
        from pod.client import PodExecError
        exc = PodExecError("exec", 1, "",
                           "annealage-pod: BUSY - REPL in use by another client",
                           caller="agent-b:corona@carbon")
        assert exc.busy is True
        text = str(exc)
        assert "do not reset" in text.lower()
        assert "agent-b:corona@carbon" in text

    def test_a_real_fault_carries_no_such_hint(self):
        from pod.client import PodExecError
        exc = PodExecError("exec", 1, "", "could not enter raw repl")
        assert exc.busy is False
        assert "do not reset" not in str(exc).lower()


class TestCallerIdentity:
    """A label, not a credential: nothing checks it and it grants nothing. Its
    only job is to let a refusal name a party."""

    def test_pod_caller_env_wins(self, monkeypatch):
        from pod.client import resolve_caller
        monkeypatch.setenv("POD_CALLER", "agent-a:corona@carbon")
        monkeypatch.delenv("CLAUDE_NET_AGENT", raising=False)
        assert resolve_caller() == "agent-a:corona@carbon"

    def test_the_agent_name_wins_over_the_env(self, monkeypatch):
        from pod.client import resolve_caller
        monkeypatch.setenv("CLAUDE_NET_AGENT", "usbhost:corona@carbon")
        monkeypatch.setenv("POD_CALLER", "ignored")
        assert resolve_caller() == "usbhost:corona@carbon"

    def test_falls_back_to_user_host_pid(self, monkeypatch):
        from pod.client import resolve_caller
        monkeypatch.delenv("CLAUDE_NET_AGENT", raising=False)
        monkeypatch.delenv("POD_CALLER", raising=False)
        name = resolve_caller()
        assert "@" in name and "/" in name

    def test_blank_env_does_not_win(self, monkeypatch):
        from pod.client import resolve_caller
        monkeypatch.setenv("POD_CALLER", "   ")
        monkeypatch.delenv("CLAUDE_NET_AGENT", raising=False)
        assert resolve_caller().strip() != ""
        assert resolve_caller() != "   "

    def test_a_client_carries_one(self):
        p = Pod(address=ADDRESS, repl_port=PORT)
        assert p.caller and isinstance(p.caller, str)
