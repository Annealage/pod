"""Pod control client built on ampremote.

One Pod instance per pod. All transport goes through ampremote using the
socket://HOST:PORT connect form; no soft reset is performed by default.

The subprocess runner is injected (default subprocess.run) so tests can
pass a fake without invoking the real ampremote.

Stubbed methods raise NotImplementedError with the phase they're pending:
  usbip_attach          - pending Phase 4 (DUT USB host + USB/IP)
  uart_stream           - pending Phase 5 (UART-over-TCP)
  telemetry             - pending Phase 5 (INA228; custom carrier hardware)
"""

import ast
import os
import socket
import threading
import time
import subprocess as _subprocess
from typing import Callable, List, Optional


def _last_dict(stdout: str) -> dict:
    """Parse the last printed dict literal from on-pod stdout."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return ast.literal_eval(line)
            except (ValueError, SyntaxError):
                pass
    return {"raw": stdout}


class Pod:
    """Control client for a single Annealage Pod over ampremote socket transport."""

    def __init__(
        self,
        address: str,
        repl_port: int = 8266,
        runner: Optional[Callable] = None,
    ):
        """Create a Pod client.

        Args:
            address: IPv4 address of the pod.
            repl_port: TCP port of the ampremote socket REPL.
            runner: Callable with the same signature as subprocess.run.
                    Defaults to subprocess.run. Inject a fake for testing.
        """
        self.address = address
        self.repl_port = repl_port
        self._runner = runner if runner is not None else _subprocess.run

    # ── argv construction (pure, testable) ───────────────────────────────

    def _argv(self, verb: str, *args: str) -> List[str]:
        """Build the ampremote argv list for a given verb and arguments.

        Returns a list starting with ['ampremote', 'connect',
        'socket://ADDRESS:PORT', verb, *args].
        No subprocess is invoked.
        """
        connect_target = f"socket://{self.address}:{self.repl_port}"
        return ["ampremote", "connect", connect_target, verb] + list(args)

    # ── live commands ────────────────────────────────────────────────────

    def exec(self, code: str) -> str:
        """Execute a MicroPython code string on the pod and return stdout.

        Uses ampremote exec verb. Raises subprocess.CalledProcessError on
        non-zero exit.
        """
        argv = self._argv("exec", code)
        result = self._runner(argv, capture_output=True, text=True, check=True)
        return result.stdout

    def eval(self, expr: str) -> str:
        """Evaluate a MicroPython expression on the pod and return the result string."""
        argv = self._argv("eval", expr)
        result = self._runner(argv, capture_output=True, text=True, check=True)
        return result.stdout

    def cp(self, src: str, dst: str) -> None:
        """Copy a file to or from the pod.

        Use ':path' prefix for pod-side paths, per ampremote convention.
        """
        argv = self._argv("fs", "cp", src, dst)
        self._runner(argv, check=True)

    def mount(self, directory: str) -> None:
        """Mount a local host directory on the pod over ampremote.

        Runs ampremote in the foreground; blocks until the user disconnects.
        """
        argv = self._argv("mount", directory)
        self._runner(argv)

    def repl(self) -> None:
        """Attach an interactive REPL to the pod. Blocks until disconnected."""
        argv = self._argv("repl")
        self._runner(argv)

    # ── DUT flash / reset / read (on-pod debug stack, workstream D) ───────

    @staticmethod
    def _flash_stream_cmd(addr: int, total: int, port: int, verify: bool) -> str:
        """Build the on-pod flash_stream invocation (pure, for testability)."""
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.flash_stream(%d, %d, port=%d, verify=%s))"
            % (addr, total, port, bool(verify))
        )

    def flash_dut(self, image: str, target: Optional[str] = None,
                  addr: int = 0, verify: bool = True, port: int = 3333) -> dict:
        """Flash a firmware image to the DUT, streamed into pod RAM (no pod FS).

        The pod runs a TCP receiver that double-buffers the image into two RAM
        buffers (Wi-Fi fills one while SWD programs the other) and never writes
        the image to its filesystem or reads the prior DUT contents. The host
        starts that receiver over the REPL and streams the file straight to it.
        Returns the on-pod result dict {ok, addr, bytes, err}.
        """
        total = os.path.getsize(image)
        result: dict = {}

        def _run():
            try:
                result["out"] = self.exec(
                    self._flash_stream_cmd(addr, total, port, verify))
            except Exception as exc:  # noqa: BLE001 - surfaced to caller
                result["exc"] = exc

        worker = threading.Thread(target=_run)
        worker.start()
        sock = None
        for _ in range(100):
            if "exc" in result:
                break
            try:
                sock = socket.create_connection((self.address, port), timeout=5)
                break
            except OSError:
                time.sleep(0.1)
        if sock is None:
            worker.join()
            exc = result.get("exc")
            detail = getattr(exc, "stderr", "") or ""
            raise RuntimeError(
                "could not connect to pod flash port %d: %r\n%s"
                % (port, exc, detail))
        # Connect used a short timeout; the transfer itself is paced by the pod
        # (it erases the whole region before reading the socket, ~85 ms/page, so
        # the initial quiet can be tens of seconds for a large image). Block for
        # the data phase rather than timing out mid-erase.
        sock.settimeout(None)
        try:
            with open(image, "rb") as f:
                while True:
                    block = f.read(65536)
                    if not block:
                        break
                    sock.sendall(block)
            try:
                sock.recv(1)   # status byte from the pod
            except OSError:
                pass
        finally:
            sock.close()
        worker.join()
        if "exc" in result:
            raise result["exc"]
        return _last_dict(result.get("out", ""))

    def reset_dut(self, mode: str = "sysreset") -> dict:
        """Reset the DUT via the on-pod debug probe.

        mode: 'sysreset' (reset and run) or 'halt' (reset and halt at the
        vector). nRST and power-cycle reset need carrier hardware not present.
        """
        out = self.exec(
            "import annealage_pod.debug.ops as o; print(o.reset(%r))" % mode
        )
        return _last_dict(out)

    @staticmethod
    def _dump_stream_cmd(addr: int, length: int, port: int) -> str:
        """Build the on-pod dump_stream invocation (pure, for testability)."""
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.dump_stream(%d, %d, port=%d))" % (addr, length, port)
        )

    def read_dut(self, addr: int, length: int, out_path: str,
                 port: int = 3334) -> str:
        """Explicitly read `length` bytes of DUT memory from `addr` to a host file.

        Streamed straight from pod RAM over TCP into the host file, no pod
        filesystem (the reverse of flash_dut). This is the only path that
        returns target contents, and only when called; flashing never reads the
        DUT back to the host.
        """
        result: dict = {}

        def _run():
            try:
                result["out"] = self.exec(
                    self._dump_stream_cmd(addr, length, port))
            except Exception as exc:  # noqa: BLE001 - surfaced to caller
                result["exc"] = exc

        worker = threading.Thread(target=_run)
        worker.start()
        sock = None
        for _ in range(100):
            if "exc" in result:
                break
            try:
                sock = socket.create_connection((self.address, port), timeout=5)
                break
            except OSError:
                time.sleep(0.1)
        if sock is None:
            worker.join()
            exc = result.get("exc")
            raise RuntimeError(
                "could not connect to pod dump port %d: %r\n%s"
                % (port, exc, getattr(exc, "stderr", "") or ""))
        sock.settimeout(None)   # transfer paced by the pod, do not time out
        got = 0
        try:
            with open(out_path, "wb") as f:
                while got < length:
                    block = sock.recv(min(65536, length - got))
                    if not block:
                        break
                    f.write(block)
                    got += len(block)
        finally:
            sock.close()
        worker.join()
        if "exc" in result:
            raise result["exc"]
        return out_path

    # ── DUT GDB endpoint (on-pod debug stack, workstream D3) ──────────────

    @staticmethod
    def _gdb_serve_cmd(port: int, reset_halt: bool) -> str:
        """Build the on-pod gdb_serve invocation (pure, for testability)."""
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.gdb_serve(port=%d, reset_halt=%s))"
            % (port, bool(reset_halt))
        )

    def gdb_endpoint(self, listen_port: int = 0, gdb_port: int = 3335,
                     reset_halt: bool = True,
                     resume_window_ms: int = 200,
                     on_listen: Optional[Callable] = None) -> tuple:
        """Start the on-pod GDB binary server and a local RSP translator.

        A worker thread runs the on-pod gdb_serve over the REPL; the main
        thread builds a GdbServer that poll-connects to the pod debug socket
        (port gdb_port), binds a local gdb-facing RSP listener, and serves one
        gdb session. on_listen, if given, is called with (host, port) once the
        listener is bound. Returns ('127.0.0.1', actual_listen_port) after gdb
        detaches; the worker is then joined.

        Mirrors flash_dut: the blocking on-pod server runs in a worker while
        the host serves over a direct TCP socket.
        """
        from pod.gdbserver import GdbServer

        result: dict = {}

        def _run():
            try:
                result["out"] = self.exec(
                    self._gdb_serve_cmd(gdb_port, reset_halt))
            except Exception as exc:  # noqa: BLE001 - surfaced to caller
                result["exc"] = exc

        worker = threading.Thread(target=_run)
        worker.start()
        try:
            server = GdbServer(
                pod_addr=self.address,
                pod_port=gdb_port,
                listen_port=listen_port,
                resume_window_ms=resume_window_ms,
            )
            bound = server.serve_forever(on_listen=on_listen)
        finally:
            worker.join()
        if "exc" in result:
            raise result["exc"]
        return bound

    # ── stubbed methods (pending future phases) ──────────────────────────

    def usbip_attach(self) -> None:
        """Attach the DUT USB device over USB/IP.

        Pending Phase 4: requires DUT USB host + USB/IP server on the pod.
        """
        raise NotImplementedError(
            "usbip_attach is not yet implemented - pending Phase 4 (DUT USB host + USB/IP)"
        )

    def uart_stream(self) -> None:
        """Stream DUT UART output over TCP.

        Pending Phase 5: requires UART-over-TCP on the pod.
        """
        raise NotImplementedError(
            "uart_stream is not yet implemented - pending Phase 5 (UART-over-TCP)"
        )

    def telemetry(self) -> None:
        """Read INA228 power telemetry from the pod carrier.

        Pending Phase 5: requires INA228 driver and custom carrier hardware.
        """
        raise NotImplementedError(
            "telemetry is not yet implemented - pending Phase 5 (INA228 telemetry, custom carrier)"
        )
