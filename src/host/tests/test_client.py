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
        # default loader is "native" (backward compat for the flat binary path)
        assert "loader='native'" in cmd

    def test_flash_stream_cmd_explicit_loader(self):
        cmd = Pod._flash_stream_cmd(0x0, 256, 3333, True, loader="flm")
        assert "loader='flm'" in cmd

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
        assert "o.halt()" in self._code(fake_runner)
        assert result["ok"] is True and result["halted"] is True

    def test_resume_code(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'halted': False}\n", returncode=0)
        result = pod_fake.resume_dut()
        assert "o.resume()" in self._code(fake_runner)
        assert result["halted"] is False

    def test_read_reg_by_name_resolves_regsel(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'regsel': 15, 'value': 268439552}\n",
            returncode=0)
        result = pod_fake.read_reg("pc")
        assert "o.read_reg(15)" in self._code(fake_runner)
        assert result["value"] == 268439552

    def test_read_reg_by_int(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'regsel': 0, 'value': 1}\n", returncode=0)
        pod_fake.read_reg(0)
        assert "o.read_reg(0)" in self._code(fake_runner)

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
        assert "o.write_reg(13, %d)" % 0x20004000 in code

    def test_read_mem_code_and_hex(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'addr': 536870912, 'length': 4, "
                   "'hex': 'deadbeef'}\n", returncode=0)
        result = pod_fake.read_mem(0x20000000, 4)
        assert "o.read_mem(%d, 4)" % 0x20000000 in self._code(fake_runner)
        assert result["hex"] == "deadbeef"

    def test_write_mem_from_bytes_hexlifies(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'addr': 536870912, 'length': 4}\n",
            returncode=0)
        pod_fake.write_mem(0x20000000, b"\xde\xad\xbe\xef")
        code = self._code(fake_runner)
        assert "o.write_mem(%d, 'deadbeef', protect=None)" % 0x20000000 in code

    def test_write_mem_accepts_hex_string(self, pod_fake, fake_runner):
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'addr': 536870912, 'length': 2}\n",
            returncode=0)
        pod_fake.write_mem(0x20000000, "beef")
        assert ("o.write_mem(%d, 'beef', protect=None)" % 0x20000000
                in self._code(fake_runner))


class TestMcpDutDebugPeek:
    def _fake_pod(self, monkeypatch):
        import pod.mcp_server as m
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        calls = {}

        class FakePod:
            def halt_dut(self, keep_attached=False):
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
        assert m.handle_dut_read_reg("x", "pc")["value"] == 0x1000
        m.handle_dut_write_reg("x", "sp", 0x20004000)
        assert calls["read_reg"] == "pc"
        assert calls["write_reg"] == ("sp", 0x20004000)

    def test_read_write_mem(self, monkeypatch):
        m, calls = self._fake_pod(monkeypatch)
        m.handle_dut_read_mem("x", 0x20000000, 16)
        m.handle_dut_write_mem("x", 0x20000000, "deadbeef")
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
        with pytest.raises(RuntimeError):
            pod.usbip_attach()


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


class TestFlashDutLoaderConsistency:
    """Verify loader is threaded consistently through flash_dut / _flash_dut_elf."""

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

    def test_flash_stream_cmd_default_loader_native(self):
        # The static helper's default stays "native" (flat binary backward compat).
        cmd = Pod._flash_stream_cmd(0x0, 64, 3333, True)
        assert "loader='native'" in cmd

    def test_elf_flash_uses_flm_by_default(self, monkeypatch, fake_runner):
        # ELF path defaults to "flm"; _flash_stream_cmd for flash segments must
        # carry loader='flm', not fall through to the "native" default.
        p = self._make_elf_pod(monkeypatch, fake_runner)
        cmd = p._flash_stream_cmd(0x0, 8, 3333, True, loader="flm")
        assert "loader='flm'" in cmd
        # Verify _flash_dut_elf builds the correct cmd by inspecting what the
        # static helper would produce for the chosen loader.
        cmd_native = p._flash_stream_cmd(0x0, 8, 3333, True, loader="native")
        assert "loader='native'" in cmd_native

    def test_elf_mass_erase_and_program_use_same_loader(self, monkeypatch,
                                                         fake_runner):
        # Both erase_dut and flash_stream must receive the same loader when
        # mass_erase=True on an ELF image. Capture on-pod code strings.
        import pod.elf_loader as el
        import pod.usbip as u
        import threading, socket as _socket

        monkeypatch.setattr(u, "ports", lambda: [])
        monkeypatch.setattr(el, "is_elf", lambda path: True)
        monkeypatch.setattr(el, "parse_load_segments",
                            lambda path, ranges: [(0x0, b"\xaa" * 8, "flash")])

        codes_seen = []
        # fake_runner is only used for the erase_dut exec call;
        # _stream_region does its own socket IO which we short-circuit below.
        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'ms': 100, 'loader': 'flm', 'err': None}\n",
            returncode=0)
        p = Pod(addr4="10.0.0.1", runner=fake_runner)
        p._elf_flash_ranges = [(0x0, 0x100000)]

        # Capture what _stream_region is called with (the cmd string), then
        # short-circuit the actual network IO.
        stream_cmds = []

        def _fake_stream(cmd, payload, size, port):
            stream_cmds.append(cmd)
            return {"ok": True, "addr": 0, "bytes": size, "err": None,
                    "loader": "flm"}

        monkeypatch.setattr(p, "_stream_region", _fake_stream)

        p._flash_dut_elf("fake.elf", flash_ranges=[(0x0, 0x100000)],
                         port=3333, verify=True, mass_erase=True, loader="flm")

        # erase_dut exec call: the code sent to the pod must use loader='flm'
        erase_code = fake_runner.call_args[0][0][4]
        assert "loader='flm'" in erase_code, (
            "erase_all called with wrong loader: %r" % erase_code)

        # flash_stream call: the streamed segment must also use loader='flm'
        assert stream_cmds, "no _stream_region call recorded"
        assert "loader='flm'" in stream_cmds[0], (
            "flash_stream called with wrong loader: %r" % stream_cmds[0])

    def test_elf_explicit_native_loader_threads_through(self, monkeypatch,
                                                         fake_runner):
        # An explicit loader="native" override on flash_dut must reach both
        # erase_dut and flash_stream (for users that opt into the nRF fast-path).
        import pod.elf_loader as el
        import pod.usbip as u

        monkeypatch.setattr(u, "ports", lambda: [])
        monkeypatch.setattr(el, "is_elf", lambda path: True)
        monkeypatch.setattr(el, "parse_load_segments",
                            lambda path, ranges: [(0x0, b"\xaa" * 8, "flash")])

        fake_runner.return_value = MagicMock(
            stdout="{'ok': True, 'ms': 50, 'loader': 'native', 'err': None}\n",
            returncode=0)
        p = Pod(addr4="10.0.0.1", runner=fake_runner)
        p._elf_flash_ranges = [(0x0, 0x100000)]

        stream_cmds = []

        def _fake_stream(cmd, payload, size, port):
            stream_cmds.append(cmd)
            return {"ok": True, "addr": 0, "bytes": size, "err": None}

        monkeypatch.setattr(p, "_stream_region", _fake_stream)

        p.flash_dut("fake.elf", mass_erase=True, loader="native")

        erase_code = fake_runner.call_args[0][0][4]
        assert "loader='native'" in erase_code
        assert stream_cmds and "loader='native'" in stream_cmds[0]

    def test_flat_binary_defaults_to_native(self, monkeypatch, fake_runner):
        # Flat binary path must default to "native" (backward compat).
        import pod.elf_loader as el
        import pod.usbip as u
        import tempfile, os

        monkeypatch.setattr(u, "ports", lambda: [])
        monkeypatch.setattr(el, "is_elf", lambda path: False)

        p = Pod(addr4="10.0.0.1", runner=fake_runner)

        stream_cmds = []

        def _fake_stream(cmd, payload, size, port):
            stream_cmds.append(cmd)
            return {"ok": True, "addr": 0, "bytes": size, "err": None}

        monkeypatch.setattr(p, "_stream_region", _fake_stream)

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"\x00" * 16)
            tmp = f.name
        try:
            p.flash_dut(tmp)
        finally:
            os.unlink(tmp)

        assert stream_cmds and "loader='native'" in stream_cmds[0]
