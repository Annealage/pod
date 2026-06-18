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
import ipaddress
import os
import socket
import struct
import threading
import time
import subprocess as _subprocess
from typing import Callable, List, Optional

from pod.target import TargetResolver


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
        address: Optional[str] = None,
        repl_port: int = 8266,
        runner: Optional[Callable] = None,
        *,
        hostname: Optional[str] = None,
        addr6=None,
        addr4: Optional[str] = None,
        fingerprint: Optional[str] = None,
        resolver: Optional[TargetResolver] = None,
    ):
        """Create a Pod client.

        The connect target is chosen by a TargetResolver from the pod's stable
        handles (mDNS hostname + IPv6) and DHCP IPv4, verifying identity before
        trusting an address that drifts (see pod.target). Every transport - the
        ampremote socket REPL and the raw flash/read/LA/gdb sockets - shares the
        one resolution.

        Args:
            address: a single literal/hostname (back-compat). Classified into
                the right handle when no explicit handle is given; a bare IPv4
                stays network-free (the resolver trusts the lone address).
            repl_port: TCP port of the ampremote socket REPL.
            runner: Callable with the same signature as subprocess.run.
                    Defaults to subprocess.run. Inject a fake for testing.
            hostname/addr6/addr4/fingerprint: the registry handles.
            resolver: inject a pre-built TargetResolver (tests / reuse).
        """
        self.repl_port = repl_port
        self._runner = runner if runner is not None else _subprocess.run
        self._seed_address = address
        if resolver is not None:
            self._resolver = resolver
        else:
            if address is not None and not (hostname or addr6 or addr4):
                try:
                    if ipaddress.ip_address(address).version == 6:
                        addr6 = [address]
                    else:
                        addr4 = address
                except ValueError:
                    hostname = address
            self._resolver = TargetResolver(
                hostname=hostname, addr6=addr6, addr4=addr4,
                fingerprint=fingerprint, repl_port=repl_port)

    @classmethod
    def from_entry(cls, entry: dict, runner: Optional[Callable] = None) -> "Pod":
        """Build a Pod from a registry entry, wiring the full handle set."""
        return cls(
            address=entry.get("address"),
            repl_port=entry.get("repl_port", 8266),
            runner=runner,
            hostname=entry.get("hostname"),
            addr6=entry.get("addr6"),
            addr4=entry.get("addr4"),
            fingerprint=entry.get("fingerprint"),
        )

    @property
    def resolver(self) -> TargetResolver:
        return self._resolver

    @property
    def address(self):
        """The resolved connect host if known, else a best-effort handle (no walk)."""
        cached = self._resolver.cached
        if cached is not None:
            return cached
        return (self._seed_address or self._resolver.addr4
                or (self._resolver.addr6[0] if self._resolver.addr6 else None)
                or self._resolver.hostname)

    # ── argv construction (pure, testable) ───────────────────────────────

    def _argv(self, verb: str, *args: str) -> List[str]:
        """Build the ampremote argv list for a given verb and arguments.

        Returns a list starting with ['ampremote', 'connect',
        'socket://HOST:PORT', verb, *args] where HOST is the resolved connect
        target (IPv6 literals bracketed). No subprocess is invoked.
        """
        connect_target = self._resolver.ampremote_target(self.repl_port)
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
                sock = socket.create_connection(
                    self._resolver.endpoint(port), timeout=5)
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

    def pinmap(self) -> dict:
        """Report the pod's own DUT-facing GPIO assignments (SWD/nRST/I2C-target).

        The pod side of the interconnect; pair with the registry dut.wiring for
        the DUT-side pins.
        """
        out = self.exec(
            "import annealage_pod._rp2_pinmap as p; print(p.pinmap())")
        return _last_dict(out)

    def discover_dut(self) -> dict:
        """Read the connected DUT's generic ADIv5/Cortex-M identity over SWD.

        Returns the on-pod ops.discover() dict: architecturally-generic IDs
        (dpidr, ap_idr, cpuid, rom_base) the host decodes/compares - no
        family-specific reads, no core halt. {"ok": False, "err": ...} if SWD
        does not connect (DUT unpowered / not wired).
        """
        out = self.exec(
            "import annealage_pod.debug.ops as o; print(o.discover())")
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
                sock = socket.create_connection(
                    self._resolver.endpoint(port), timeout=5)
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
                resolver=self._resolver,
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

    # ── DUT-facing peripherals (curated machine helpers, workstream E) ────

    def i2c_target(self, addr: int = 0x42, regs=None, bus: int = 1,
                   scl: int = 11, sda: int = 10, size: int = 256,
                   name: str = "i2c_target") -> dict:
        """Bring up a persistent hardware I2C target (register file) on the pod.

        The pod becomes an I2C device at `addr` on hardware I2C `bus` (pins
        scl/sda), backing a `size`-byte register file the DUT controller reads
        and writes (e.g. readfrom_mem(addr, off, n)). `regs` seeds the file from
        offset 0. The target persists until released. Bench default: I2C1,
        SCL=GP11, SDA=GP10.
        """
        regs_arg = "None" if regs is None else repr([int(b) & 0xFF for b in regs])
        code = (
            "import annealage_pod.peripherals as p;"
            "print(p.i2c_target(addr=%d, regs=%s, bus=%d, scl=%d, sda=%d,"
            " size=%d, name=%r))"
            % (addr, regs_arg, bus, scl, sda, size, name)
        )
        return _last_dict(self.exec(code))

    def i2c_target_regs(self, off: int = 0, length: Optional[int] = None,
                        write=None, name: str = "i2c_target") -> dict:
        """Read or write the pod I2C target's register file from the host.

        With `write` set (iterable of bytes), write it at `off` first; returns
        the window [off:off+length] (length defaults to the rest of the file).
        """
        write_arg = "None" if write is None else repr([int(b) & 0xFF for b in write])
        len_arg = "None" if length is None else str(int(length))
        code = (
            "import annealage_pod.peripherals as p;"
            "print(p.i2c_target_regs(off=%d, length=%s, write=%s, name=%r))"
            % (off, len_arg, write_arg, name)
        )
        return _last_dict(self.exec(code))

    def peripheral_release(self, name: str = "*") -> dict:
        """Release one named pod peripheral instance, or all of them with '*'."""
        code = ("import annealage_pod.peripherals as p; print(p.release(%r))"
                % name)
        return _last_dict(self.exec(code))

    def peripheral_list(self) -> dict:
        """List the live named pod peripheral instances."""
        code = "import annealage_pod.peripherals as p; print(p.instances())"
        return _last_dict(self.exec(code))

    def gpio(self, pin: int, value: Optional[int] = None, mode: str = "out",
             pull: Optional[str] = None) -> dict:
        """Read (value=None) or drive a pod GPIO; returns the resulting level."""
        val_arg = "None" if value is None else str(int(bool(value)))
        code = (
            "import annealage_pod.peripherals as p;"
            "print(p.gpio(%d, value=%s, mode=%r, pull=%r))"
            % (pin, val_arg, mode, pull)
        )
        return _last_dict(self.exec(code))

    def adc(self, pin: int) -> dict:
        """Sample a pod ADC channel; returns raw u16 and a 3.3V-ref voltage."""
        code = ("import annealage_pod.peripherals as p; print(p.adc(%d))" % pin)
        return _last_dict(self.exec(code))

    # ── DUT logic analyser (PIO capture on the pod, workstream E / Track 2) ─

    SYS_HZ = 150_000_000   # pod PIO clock; rate = SYS_HZ / clkdiv

    @staticmethod
    def _la_stream_cmd(base_pin: int, width: int, rate: int, depth: int,
                       trigger, port: int, sm_id: int) -> str:
        """Build the on-pod la_stream invocation (pure, for testability)."""
        trig = "None"
        if trigger is not None:
            trig = "(%d, %r)" % (int(trigger[0]), str(trigger[1]))
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.la_stream(%d, width=%d, rate=%d, depth=%d, trigger=%s,"
            " port=%d, sm_id=%d))"
            % (base_pin, width, int(rate), depth, trig, port, sm_id)
        )

    @staticmethod
    def _recv_exact(sock, n: int) -> bytes:
        """Read exactly n bytes from sock or raise EOFError."""
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError(
                    "pod closed mid-capture (%d/%d bytes)" % (len(buf), n))
            buf += chunk
        return bytes(buf)

    def logic_analyse(self, base_pin: int, width: int = 1, rate: int = 1000000,
                      depth: int = 8000, trigger=None, out_path: str = "capture.vcd",
                      port: int = 3336, sm_id: int = 0, names=None) -> dict:
        """Capture DUT pins with the pod logic analyser and write VCD to out_path.

        Swaps SWD out on the pod, captures `depth` samples of `width` contiguous
        pins from `base_pin` at ~`rate` Hz (optional trigger=(pin, cond) with cond
        in 'rise'/'fall'/'high'/'low'), streams the raw capture over TCP, and
        decodes it to VCD. SWD is restored lazily on the pod's next debug op.
        Returns {ok, out_path, width, rate, clkdiv, words, complete, samples}.
        """
        from pod import vcd

        result: dict = {}

        def _run():
            try:
                result["out"] = self.exec(self._la_stream_cmd(
                    base_pin, width, rate, depth, trigger, port, sm_id))
            except Exception as exc:  # noqa: BLE001 - surfaced to caller
                result["exc"] = exc

        worker = threading.Thread(target=_run)
        worker.start()
        sock = None
        for _ in range(150):
            if "exc" in result:
                break
            try:
                sock = socket.create_connection(
                    self._resolver.endpoint(port), timeout=5)
                break
            except OSError:
                time.sleep(0.1)
        if sock is None:
            worker.join()
            exc = result.get("exc")
            raise RuntimeError(
                "could not connect to pod LA port %d: %r\n%s"
                % (port, exc, getattr(exc, "stderr", "") or ""))
        # Bound the wait so a pod that accepts but never sends (capture error,
        # flaky link) raises instead of hanging the caller forever; generous
        # enough to cover a trigger wait plus a large capture.
        sock.settimeout(60)
        try:
            words, w, clkdiv, complete = struct.unpack(
                "<IIII", self._recv_exact(sock, 16))
            raw = self._recv_exact(sock, words * 4)
        finally:
            sock.close()
        worker.join()
        if "exc" in result:
            raise result["exc"]
        rate_actual = self.SYS_HZ / clkdiv
        samples = vcd.decode_to_vcd(raw, words, w, rate_actual, out_path,
                                    names=names)
        return {"ok": True, "out_path": out_path, "width": w,
                "rate": rate_actual, "clkdiv": clkdiv, "words": words,
                "complete": bool(complete), "samples": samples}

    # ── stubbed methods (pending future phases) ──────────────────────────

    def _usbip_host(self):
        """A usbip-friendly host string (prefer hostname/IPv4; usbip IPv6 is spotty)."""
        r = self._resolver
        return (r.hostname or r.addr4 or (r.addr6[0] if r.addr6 else None)
                or self.address)

    def usbip_list(self) -> list:
        """The DUT USB devices the pod exports over USB/IP (live VID:PID + busid)."""
        from pod import usbip as _u
        return _u.list_remote(self._usbip_host())

    def usbip_attach(self, ensure: bool = True) -> dict:
        """Export the DUT over USB/IP and attach it on this host.

        With ensure=True, first brings the pod's USB host + usbip server up over
        the REPL (see pod.usbip.ensure_server - note the Wi-Fi risk). Lists the
        exported device for its live VID:PID + busid, attaches it (sudo), and
        returns {busid, vid, pid, tty}; tty is the DUT's CDC device, or None if
        it did not enumerate in time.
        """
        from pod import usbip as _u
        if ensure:
            _u.ensure_server(self)
        host = self._usbip_host()
        devs = _u.list_remote(host)
        if not devs:
            raise RuntimeError(
                "pod %s exports no USB device - is the DUT on the pod USB host "
                "port and enumerated?" % host)
        dev = dict(devs[0])
        before = _u.serial_devices()
        _u.attach(host, dev["busid"])
        dev["tty"] = _u.wait_for_new_tty(before)
        return dev

    def usbip_detach(self, port: int) -> bool:
        """Detach a vhci port previously attached (see `usbip port`)."""
        from pod import usbip as _u
        return _u.detach(port)

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
