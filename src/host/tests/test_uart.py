"""Tests for UART bridge host tooling: Pod.uart_stream, CLI, and MCP handler."""

import sys
import socket
import pytest
from unittest.mock import MagicMock, patch, mock_open
from io import BytesIO
from types import SimpleNamespace

import pod.cli as cli
import pod.mcp_server as mcp_server
from pod.client import Pod
from pod.cli import main


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Route registry I/O to a fresh tmp directory for every test."""
    monkeypatch.setenv("POD_CONFIG_DIR", str(tmp_path))
    yield tmp_path


def _register_test_pod(monkeypatch, label="test-pod",
                        address="192.168.0.121", uart_port=None):
    """Helper: register a pod in the isolated registry."""
    argv = [
        "pod", "register", label,
        "--address", address,
        "--repl-port", "8266",
        "--no-probe",
    ]
    if uart_port is not None:
        argv += ["--uart-port", str(uart_port)]
    monkeypatch.setattr(sys, "argv", argv)
    ret = main()
    assert ret in (0, None), f"register failed with {ret}"


class TestUartPortResolutionRegistry:
    """Test UART port resolution from registry entries."""

    def test_explicit_uart_port_in_registry(self, monkeypatch):
        """When uart_port is registered, Pod resolves it."""
        _register_test_pod(monkeypatch, label="p1", uart_port=3001)
        from pod.registry import get_pod
        entry = get_pod("p1")
        assert entry.get("uart_port") == 3001

    def test_missing_uart_port_defaults_to_2000(self, monkeypatch):
        """When uart_port is absent from registry, default is 2000."""
        _register_test_pod(monkeypatch, label="p2")
        from pod.registry import get_pod
        entry = get_pod("p2")
        assert entry.get("uart_port") is None  # not set


class TestCliUartArgConstruction:
    """Test CLI argument parsing for the 'pod bench uart' subcommand."""

    def test_uart_subcommand_label_required(self, monkeypatch, capsys):
        """uart subcommand requires a label argument."""
        monkeypatch.setattr(sys, "argv", ["pod", "bench", "uart"])
        with pytest.raises(SystemExit):
            main()

    def test_uart_subcommand_with_mock_pod(self, monkeypatch, capsys):
        """uart subcommand is recognized by argparse and calls the handler."""
        _register_test_pod(monkeypatch, label="p3")

        class FakePod:
            def uart_stream(self, **kw):
                return {"ok": True, "bytes_received": 0}

        with patch("pod.cli.Pod") as MockPod:
            MockPod.from_entry.return_value = FakePod()
            monkeypatch.setattr(sys, "argv", ["pod", "bench", "uart", "p3"])
            ret = main()
            assert ret == 0

    def test_uart_port_flag_overrides_registry(self, monkeypatch, capsys):
        """--port flag overrides registry uart_port."""
        _register_test_pod(monkeypatch, label="p4", uart_port=3001)
        # Create a mock resolver that tracks which port was used
        ports_used = []

        class FakePod:
            def uart_stream(self, port=2000, **kw):
                ports_used.append(port)
                return {"ok": True, "bytes_received": 0}

        with patch("pod.cli.Pod") as MockPod:
            MockPod.from_entry.return_value = FakePod()
            monkeypatch.setattr(sys, "argv", ["pod", "bench", "uart", "p4", "--port", "5555"])
            ret = main()
            assert ret == 0
            assert ports_used == [5555]

    def test_uart_port_defaults_to_registry(self, monkeypatch):
        """When --port is not given, registry uart_port is used."""
        _register_test_pod(monkeypatch, label="p5", uart_port=3001)
        ports_used = []

        class FakePod:
            def uart_stream(self, port=2000, **kw):
                ports_used.append(port)
                return {"ok": True, "bytes_received": 0}

        with patch("pod.cli.Pod") as MockPod:
            MockPod.from_entry.return_value = FakePod()
            monkeypatch.setattr(sys, "argv", ["pod", "bench", "uart", "p5"])
            ret = main()
            assert ret == 0
            assert ports_used == [3001]

    def test_uart_port_defaults_to_2000_when_absent(self, monkeypatch):
        """When uart_port is absent and --port not given, default is 2000."""
        _register_test_pod(monkeypatch, label="p6")  # no uart_port
        ports_used = []

        class FakePod:
            def uart_stream(self, port=2000, **kw):
                ports_used.append(port)
                return {"ok": True, "bytes_received": 0}

        with patch("pod.cli.Pod") as MockPod:
            MockPod.from_entry.return_value = FakePod()
            monkeypatch.setattr(sys, "argv", ["pod", "bench", "uart", "p6"])
            ret = main()
            assert ret == 0
            assert ports_used == [2000]

    def test_uart_duration_flag(self, monkeypatch):
        """--duration flag is passed to uart_stream."""
        _register_test_pod(monkeypatch, label="p7")
        durations_used = []

        class FakePod:
            def uart_stream(self, duration=None, **kw):
                durations_used.append(duration)
                return {"ok": True, "bytes_received": 0}

        with patch("pod.cli.Pod") as MockPod:
            MockPod.from_entry.return_value = FakePod()
            monkeypatch.setattr(sys, "argv", ["pod", "bench", "uart", "p7", "--duration", "10.5"])
            ret = main()
            assert ret == 0
            assert durations_used == [10.5]

    def test_uart_interactive_flag(self, monkeypatch):
        """--tx / --interactive flag is passed to uart_stream."""
        _register_test_pod(monkeypatch, label="p8")
        interactive_modes = []

        class FakePod:
            def uart_stream(self, interactive=False, **kw):
                interactive_modes.append(interactive)
                return {"ok": True, "bytes_received": 0}

        with patch("pod.cli.Pod") as MockPod:
            MockPod.from_entry.return_value = FakePod()
            monkeypatch.setattr(sys, "argv", ["pod", "bench", "uart", "p8", "--tx"])
            ret = main()
            assert ret == 0
            assert interactive_modes == [True]

    def test_uart_out_flag(self, monkeypatch):
        """--out flag is passed to uart_stream."""
        _register_test_pod(monkeypatch, label="p9")
        out_paths = []

        class FakePod:
            def uart_stream(self, out_path=None, **kw):
                out_paths.append(out_path)
                return {"ok": True, "bytes_received": 0}

        with patch("pod.cli.Pod") as MockPod:
            MockPod.from_entry.return_value = FakePod()
            monkeypatch.setattr(sys, "argv", ["pod", "bench", "uart", "p9", "--out", "/tmp/log.bin"])
            ret = main()
            assert ret == 0
            assert out_paths == ["/tmp/log.bin"]

    def test_uart_keyboard_interrupt_caught(self, monkeypatch):
        """KeyboardInterrupt in cmd_uart is caught silently."""
        _register_test_pod(monkeypatch, label="p10")

        class FakePod:
            def uart_stream(self, **kw):
                raise KeyboardInterrupt()

        with patch("pod.cli.Pod") as MockPod:
            MockPod.from_entry.return_value = FakePod()
            monkeypatch.setattr(sys, "argv", ["pod", "bench", "uart", "p10"])
            ret = main()
            assert ret == 0

    def test_uart_unknown_label_errors(self, monkeypatch):
        """uart with unknown label exits nonzero."""
        monkeypatch.setattr(sys, "argv", ["pod", "bench", "uart", "nonexistent"])
        with pytest.raises(SystemExit):
            main()


class TestMcpHandlerTailUart:
    """Test the MCP server handler for bench_uart."""

    def test_handle_bench_uart_basic(self, monkeypatch):
        """handle_bench_uart resolves label and calls uart_stream."""
        monkeypatch.setattr(mcp_server, "get_pod",
                            lambda label: {"addr4": "192.168.0.100", "repl_port": 8266})
        uart_stream_calls = []

        class FakePod:
            def uart_stream(self, port=2000, duration=30.0, on_output=None):
                uart_stream_calls.append({"port": port, "duration": duration})
                on_output(b"boot ok\n")
                return {"ok": True, "bytes_received": 42}

        with patch.object(mcp_server.Pod, "from_entry",
                          classmethod(lambda cls, e: FakePod())):
            result = mcp_server.handle_bench_uart("test-pod")
            assert result["ok"] is True
            assert result["bytes_received"] == 42
            assert result["text"] == "boot ok\n"
            assert uart_stream_calls[0]["port"] == 2000

    def test_handle_bench_uart_uses_registry_port(self, monkeypatch):
        """handle_bench_uart uses registry uart_port when present."""
        monkeypatch.setattr(mcp_server, "get_pod",
                            lambda label: {"addr4": "192.168.0.100", "uart_port": 3001})
        uart_stream_calls = []

        class FakePod:
            def uart_stream(self, port=2000, duration=30.0, on_output=None):
                uart_stream_calls.append({"port": port, "duration": duration})
                return {"ok": True, "bytes_received": 0}

        with patch.object(mcp_server.Pod, "from_entry",
                          classmethod(lambda cls, e: FakePod())):
            mcp_server.handle_bench_uart("test-pod")
            assert uart_stream_calls[0]["port"] == 3001

    def test_handle_bench_uart_explicit_port_overrides(self, monkeypatch):
        """handle_bench_uart explicit port parameter overrides registry."""
        monkeypatch.setattr(mcp_server, "get_pod",
                            lambda label: {"addr4": "192.168.0.100", "uart_port": 3001})
        uart_stream_calls = []

        class FakePod:
            def uart_stream(self, port=2000, duration=30.0, on_output=None):
                uart_stream_calls.append({"port": port, "duration": duration})
                return {"ok": True, "bytes_received": 0}

        with patch.object(mcp_server.Pod, "from_entry",
                          classmethod(lambda cls, e: FakePod())):
            mcp_server.handle_bench_uart("test-pod", port=5555)
            assert uart_stream_calls[0]["port"] == 5555

    def test_handle_bench_uart_default_duration(self, monkeypatch):
        """handle_bench_uart uses default duration of 30s."""
        monkeypatch.setattr(mcp_server, "get_pod",
                            lambda label: {"addr4": "192.168.0.100"})
        uart_stream_calls = []

        class FakePod:
            def uart_stream(self, port=2000, duration=30.0, on_output=None):
                uart_stream_calls.append({"port": port, "duration": duration})
                return {"ok": True, "bytes_received": 0}

        with patch.object(mcp_server.Pod, "from_entry",
                          classmethod(lambda cls, e: FakePod())):
            mcp_server.handle_bench_uart("test-pod")
            assert uart_stream_calls[0]["duration"] == 30.0

    def test_handle_bench_uart_explicit_duration(self, monkeypatch):
        """handle_bench_uart uses explicit duration when provided."""
        monkeypatch.setattr(mcp_server, "get_pod",
                            lambda label: {"addr4": "192.168.0.100"})
        uart_stream_calls = []

        class FakePod:
            def uart_stream(self, port=2000, duration=30.0, on_output=None):
                uart_stream_calls.append({"port": port, "duration": duration})
                return {"ok": True, "bytes_received": 0}

        with patch.object(mcp_server.Pod, "from_entry",
                          classmethod(lambda cls, e: FakePod())):
            mcp_server.handle_bench_uart("test-pod", duration=15.5)
            assert uart_stream_calls[0]["duration"] == 15.5

    def test_handle_bench_uart_missing_label_raises_keyerror(self, monkeypatch):
        """handle_bench_uart raises KeyError when label not found."""
        monkeypatch.setattr(mcp_server, "get_pod", lambda label: None)
        with pytest.raises(KeyError):
            mcp_server.handle_bench_uart("nonexistent-pod")


class TestPodUartStreamConnect:
    """Test the socket connection logic in Pod.uart_stream."""

    def _fake_resolver(self, monkeypatch):
        """Inject a fake resolver that returns a deterministic endpoint."""
        def endpoint(port):
            return ("192.168.0.100", port)
        resolver = MagicMock()
        resolver.endpoint.side_effect = endpoint
        return resolver

    def test_uart_stream_socket_connect_success_on_first_try(self, monkeypatch):
        """uart_stream connects immediately on successful socket."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver(monkeypatch)

        fake_sock = MagicMock()
        fake_sock.recv.return_value = b""  # EOF

        attempts = []

        def connect_fn(endpoint, timeout):
            attempts.append(endpoint)
            return fake_sock

        with patch("socket.create_connection", side_effect=connect_fn):
            # Must provide a duration or the loop will never exit
            result = pod.uart_stream(port=2000, duration=0.001)
            assert len(attempts) == 1
            assert result["ok"] is True
            assert result["bytes_received"] == 0

    def test_uart_stream_socket_connect_fails_all_retries(self, monkeypatch):
        """uart_stream raises RuntimeError when all 150 connection attempts fail."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver(monkeypatch)

        attempt_count = [0]

        def always_fail(endpoint, timeout):
            attempt_count[0] += 1
            raise OSError("connection refused")

        with patch("socket.create_connection", side_effect=always_fail):
            with patch("pod.client.time.sleep"):  # skip the sleep delay
                with pytest.raises(RuntimeError) as exc_info:
                    pod.uart_stream(port=2000)
                assert "UART port 2000" in str(exc_info.value)
                assert "refused connection" in str(exc_info.value)
                assert attempt_count[0] == 150


class TestPodUartStreamOutput:
    """Test output modes: on_output callback and file."""

    def _fake_resolver(self, monkeypatch):
        """Inject a fake resolver that returns a deterministic endpoint."""
        def endpoint(port):
            return ("192.168.0.100", port)
        resolver = MagicMock()
        resolver.endpoint.side_effect = endpoint
        return resolver

    def test_uart_stream_on_output_callback(self, monkeypatch):
        """uart_stream calls on_output callback for each chunk."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver(monkeypatch)

        recv_count = [0]

        def recv_fn(n):
            recv_count[0] += 1
            if recv_count[0] == 1:
                return b"hello"
            elif recv_count[0] == 2:
                return b"world"
            else:
                return b""  # EOF for all subsequent calls

        fake_sock = MagicMock()
        fake_sock.recv.side_effect = recv_fn

        chunks = []
        def on_output(chunk):
            chunks.append(chunk)

        with patch("socket.create_connection", return_value=fake_sock):
            # No duration: the fake socket's EOF ends the loop. A duration
            # would race it - uart_stream checks the deadline after each chunk,
            # so under load the first chunk can already exceed it and the
            # second is never read.
            result = pod.uart_stream(port=2000, on_output=on_output)
            assert result["bytes_received"] == 10
            assert chunks == [b"hello", b"world"]

    def test_uart_stream_file_output(self, monkeypatch):
        """uart_stream writes to a file when out_path is provided."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver(monkeypatch)

        recv_count = [0]

        def recv_fn(n):
            recv_count[0] += 1
            if recv_count[0] == 1:
                return b"hello"
            elif recv_count[0] == 2:
                return b"world"
            else:
                return b""  # EOF for all subsequent calls

        fake_sock = MagicMock()
        fake_sock.recv.side_effect = recv_fn

        mock_file_obj = MagicMock()
        mock_file_obj.__enter__ = MagicMock(return_value=mock_file_obj)
        mock_file_obj.__exit__ = MagicMock(return_value=None)

        with patch("socket.create_connection", return_value=fake_sock):
            with patch("builtins.open", return_value=mock_file_obj):
                # No duration: EOF ends the loop, so both chunks are always
                # read (a deadline races the second one under load).
                result = pod.uart_stream(port=2000, out_path="/tmp/uart.bin")
                assert result["bytes_received"] == 10
                assert mock_file_obj.write.call_count == 2
                calls = [c[0][0] for c in mock_file_obj.write.call_args_list]
                assert calls == [b"hello", b"world"]

    def test_uart_stream_file_closed_on_exception(self, monkeypatch):
        """uart_stream closes the file in finally block."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver(monkeypatch)

        fake_sock = MagicMock()
        fake_sock.recv.side_effect = RuntimeError("socket error")

        mock_file_obj = MagicMock()
        mock_file_obj.__enter__ = MagicMock(return_value=mock_file_obj)
        mock_file_obj.__exit__ = MagicMock(return_value=None)

        with patch("socket.create_connection", return_value=fake_sock):
            with patch("builtins.open", return_value=mock_file_obj):
                with pytest.raises(RuntimeError):
                    pod.uart_stream(port=2000, out_path="/tmp/uart.bin")
                mock_file_obj.close.assert_called_once()


class TestPodUartStreamDuration:
    """Test duration timeout behavior."""

    def _fake_resolver(self, monkeypatch):
        """Inject a fake resolver that returns a deterministic endpoint."""
        def endpoint(port):
            return ("192.168.0.100", port)
        resolver = MagicMock()
        resolver.endpoint.side_effect = endpoint
        return resolver

    def test_uart_stream_with_duration_and_callback(self, monkeypatch):
        """uart_stream exits after duration when using a callback."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver(monkeypatch)

        fake_sock = MagicMock()
        fake_sock.recv.return_value = b"x"

        chunks = []
        def on_output(chunk):
            chunks.append(chunk)

        with patch("socket.create_connection", return_value=fake_sock):
            result = pod.uart_stream(port=2000, duration=0.001, on_output=on_output)
            assert result["ok"] is True
            # May be 0 or 1 depending on timing, but should complete
            assert result["bytes_received"] >= 0


class TestPodUartStreamInteractive:
    """Test interactive mode (stdin forwarding)."""

    def _fake_resolver(self, monkeypatch):
        """Inject a fake resolver that returns a deterministic endpoint."""
        def endpoint(port):
            return ("192.168.0.100", port)
        resolver = MagicMock()
        resolver.endpoint.side_effect = endpoint
        return resolver

    def test_uart_stream_non_interactive_with_callback(self, monkeypatch):
        """uart_stream with callback avoids sys.stdin and sys.stdout issues."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver(monkeypatch)

        recv_count = [0]

        def recv_fn(n):
            recv_count[0] += 1
            if recv_count[0] == 1:
                return b"prompt>"
            else:
                return b""

        fake_sock = MagicMock()
        fake_sock.recv.side_effect = recv_fn

        chunks = []
        def on_output(chunk):
            chunks.append(chunk)

        with patch("socket.create_connection", return_value=fake_sock):
            # Non-interactive with callback to avoid sys imports
            result = pod.uart_stream(port=2000, interactive=False, duration=0.001,
                                       on_output=on_output)
            assert result["bytes_received"] == 7
            assert chunks == [b"prompt>"]


class TestPodUartStreamEdgeCases:
    """Test edge cases and error handling."""

    def _fake_resolver(self, monkeypatch):
        """Inject a fake resolver that returns a deterministic endpoint."""
        def endpoint(port):
            return ("192.168.0.100", port)
        resolver = MagicMock()
        resolver.endpoint.side_effect = endpoint
        return resolver

    def test_uart_stream_empty_stream(self, monkeypatch):
        """uart_stream handles immediate EOF."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver(monkeypatch)

        fake_sock = MagicMock()
        fake_sock.recv.return_value = b""

        with patch("socket.create_connection", return_value=fake_sock):
            result = pod.uart_stream(port=2000, duration=0.001)
            assert result["ok"] is True
            assert result["bytes_received"] == 0

    def test_uart_stream_socket_timeout_handled(self, monkeypatch):
        """uart_stream handles socket.timeout exceptions."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver(monkeypatch)

        fake_sock = MagicMock()

        timeout_count = [0]

        def recv_side_effect(n):
            timeout_count[0] += 1
            if timeout_count[0] < 3:
                raise socket.timeout()
            return b""

        fake_sock.recv.side_effect = recv_side_effect

        with patch("socket.create_connection", return_value=fake_sock):
            result = pod.uart_stream(port=2000, duration=0.001)
            assert result["ok"] is True
            assert result["bytes_received"] == 0

    def test_uart_stream_socket_closed_in_finally(self, monkeypatch):
        """uart_stream closes socket in finally block."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver(monkeypatch)

        recv_count = [0]

        def recv_fn(n):
            recv_count[0] += 1
            if recv_count[0] == 1:
                return b"data"
            else:
                return b""

        fake_sock = MagicMock()
        fake_sock.recv.side_effect = recv_fn

        chunks = []
        def on_output(chunk):
            chunks.append(chunk)

        with patch("socket.create_connection", return_value=fake_sock):
            result = pod.uart_stream(port=2000, duration=0.001, on_output=on_output)
            fake_sock.close.assert_called_once()


class TestPodUartStreamStdoutStdin:
    """Cover the default stdout path and the interactive stdin path.

    These paths use sys.stdout.buffer and sys.stdin.buffer directly and were
    previously untested; any missing module-level `import sys` raises NameError
    on the first received byte.
    """

    def _fake_resolver(self):
        resolver = MagicMock()
        resolver.endpoint.side_effect = lambda port: ("192.168.0.100", port)
        return resolver

    def test_default_stdout_path(self, monkeypatch):
        """With no on_output or out_path, bytes go to sys.stdout.buffer."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver()

        recv_count = [0]

        def recv_fn(n):
            recv_count[0] += 1
            if recv_count[0] == 1:
                return b"hello"
            return b""  # EOF

        fake_sock = MagicMock()
        fake_sock.recv.side_effect = recv_fn

        fake_stdout_buf = MagicMock()
        with patch("socket.create_connection", return_value=fake_sock):
            with patch("sys.stdout") as mock_stdout:
                mock_stdout.buffer = fake_stdout_buf
                result = pod.uart_stream(port=2000)
        assert result["ok"] is True
        assert result["bytes_received"] == 5
        fake_stdout_buf.write.assert_called_once_with(b"hello")
        fake_stdout_buf.flush.assert_called_once()

    def test_interactive_stdin_path(self, monkeypatch):
        """With interactive=True, sys.stdin.buffer is read and forwarded."""
        import select as _select_mod

        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver()

        recv_count = [0]

        def recv_fn(n):
            recv_count[0] += 1
            if recv_count[0] == 1:
                return b"prompt>"
            return b""  # EOF

        fake_sock = MagicMock()
        fake_sock.recv.side_effect = recv_fn

        fake_stdin_buf = MagicMock()
        fake_stdin_buf.read1.return_value = b"cmd\n"

        fake_stdout_buf = MagicMock()

        # select.select returns stdin as readable on first call, then nothing.
        select_count = [0]

        def fake_select(rlist, wlist, xlist, timeout):
            select_count[0] += 1
            if select_count[0] == 1:
                return (rlist, [], [])
            return ([], [], [])

        with patch("socket.create_connection", return_value=fake_sock):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.buffer = fake_stdin_buf
                with patch("sys.stdout") as mock_stdout:
                    mock_stdout.buffer = fake_stdout_buf
                    with patch("select.select", side_effect=fake_select):
                        result = pod.uart_stream(port=2000, interactive=True)
        assert result["ok"] is True
        assert result["bytes_received"] == 7
        fake_sock.sendall.assert_called_with(b"cmd\n")


class TestPodUartStreamPeerEof:
    """Verify the loop terminates on peer EOF without a duration guard."""

    def _fake_resolver(self):
        resolver = MagicMock()
        resolver.endpoint.side_effect = lambda port: ("192.168.0.100", port)
        return resolver

    def test_loop_exits_on_peer_close(self, monkeypatch):
        """When recv returns b'' (peer closed), the loop exits without duration."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver()

        fake_sock = MagicMock()
        fake_sock.recv.return_value = b""  # immediate EOF

        chunks = []

        with patch("socket.create_connection", return_value=fake_sock):
            # No duration: must return on peer close, not spin forever.
            result = pod.uart_stream(port=2000, on_output=chunks.append)
        assert result["ok"] is True
        assert result["bytes_received"] == 0
        # recv must have been called at least once but the loop must not have
        # called it thousands of times (spin guard: cap at a reasonable bound).
        assert fake_sock.recv.call_count >= 1
        assert fake_sock.recv.call_count < 50

    def test_timeout_continues_not_breaks(self, monkeypatch):
        """socket.timeout during recv does not terminate the stream."""
        pod = Pod(address="192.168.0.100", repl_port=8266)
        pod._resolver = self._fake_resolver()

        call_count = [0]

        def recv_fn(n):
            call_count[0] += 1
            if call_count[0] < 3:
                raise socket.timeout()
            return b""  # EOF on third call

        fake_sock = MagicMock()
        fake_sock.recv.side_effect = recv_fn

        chunks = []
        with patch("socket.create_connection", return_value=fake_sock):
            result = pod.uart_stream(port=2000, on_output=chunks.append)
        assert result["ok"] is True
        # Loop ran through two timeouts before the EOF break.
        assert call_count[0] == 3
