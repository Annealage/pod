"""Pod control client built on ampremote.

One Pod instance per pod. All transport goes through ampremote using the
socket://HOST:PORT connect form; no soft reset is performed by default.

The subprocess runner is injected (default subprocess.run) so tests can
pass a fake without invoking the real ampremote.

Stubbed methods raise NotImplementedError with the phase they're pending:
  usbip_attach          - pending Phase 4 (DUT USB host + USB/IP)
  telemetry             - pending Phase 5 (INA228; custom carrier hardware)
"""

import ast
import ipaddress
import os
import socket
import struct
import sys
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


def _classify_exec_failure(stderr, stdout):
    """Best-effort label for why a pod exec failed, from ampremote output."""
    blob = ((stderr or "") + "\n" + (stdout or "")).lower()
    if "timed out" in blob or "timeout" in blob:
        return "timeout reaching the pod"
    if any(s in blob for s in ("could not enter raw repl", "failed to access",
                               "no device", "could not connect",
                               "no serial device")):
        return "raw-REPL entry / connection failed"
    if "syntaxerror" in blob:
        return "syntax error in the submitted code"
    if "traceback" in blob or "error:" in blob:
        return "exception on the pod"
    return "exec error"


# Core register selector names -> regsel, mirroring swd_dap.CortexM numbering
# (and the host gdbserver REG_MAP). Accepted by read_reg/write_reg in place of
# the bare integer so an agent can say "pc" instead of 15.
_REG_NAMES = {
    "r0": 0, "r1": 1, "r2": 2, "r3": 3, "r4": 4, "r5": 5, "r6": 6, "r7": 7,
    "r8": 8, "r9": 9, "r10": 10, "r11": 11, "r12": 12,
    "sp": 13, "lr": 14, "pc": 15, "xpsr": 16, "msp": 17, "psp": 18,
}


def _resolve_regsel(reg) -> int:
    """Resolve a register name or number to a regsel int (0..18).

    Accepts an int, a name (pc/sp/lr/r0..), or a numeric string (the CLI passes
    its positional arg as a string, so "15"/"0x0f" must work too). Raises
    ValueError for an unknown name or an out-of-range number, so a bad regsel
    fails locally instead of after a pod round-trip.
    """
    hi = max(_REG_NAMES.values())
    if isinstance(reg, str):
        key = reg.strip().lower()
        if key in _REG_NAMES:
            return _REG_NAMES[key]
        try:
            regsel = int(key, 0)
        except ValueError:
            raise ValueError(
                "unknown register %r (use 0..%d or %s)"
                % (reg, hi, "/".join(_REG_NAMES)))
    else:
        regsel = int(reg)
    if not 0 <= regsel <= hi:
        raise ValueError("regsel %d out of range 0..%d" % (regsel, hi))
    return regsel


class PodExecError(RuntimeError):
    """A pod exec failed. Carries the classified reason plus the ampremote
    stdout/stderr, so callers see WHY (raw-REPL entry vs device exception vs
    timeout) rather than a bare non-zero exit."""

    def __init__(self, verb, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = (stdout or "").strip()
        self.stderr = (stderr or "").strip()
        self.reason = _classify_exec_failure(self.stderr, self.stdout)
        detail = self.stderr or self.stdout
        last = detail.splitlines()[-1] if detail else ""
        super().__init__(
            "pod %s failed: %s (exit %s)%s"
            % (verb, self.reason, returncode, (": " + last) if last else ""))


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
        """Build a Pod from a registry entry, wiring the full handle set.

        Also captures DUT flash geometry (flash_base + flash_size from the
        declared dut block) so the ELF flash path can derive flash_ranges
        without hardcoding target addresses.
        """
        pod = cls(
            address=entry.get("address"),
            repl_port=entry.get("repl_port", 8266),
            runner=runner,
            hostname=entry.get("hostname"),
            addr6=entry.get("addr6"),
            addr4=entry.get("addr4"),
            fingerprint=entry.get("fingerprint"),
        )
        dut = entry.get("dut") or {}
        flash_base = dut.get("flash_base")
        flash_size = dut.get("flash_size")
        if flash_base is not None and flash_size is not None:
            # Accept int or hex/dec string (mirrors cli.py's int(addr, 0)).
            fb = int(flash_base, 0) if isinstance(flash_base, str) else int(flash_base)
            fs = int(flash_size, 0) if isinstance(flash_size, str) else int(flash_size)
            pod._elf_flash_ranges = [(fb, fb + fs)]
        else:
            pod._elf_flash_ranges = None
        return pod

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

        Runs the ampremote exec verb. Raises PodExecError (classified reason +
        ampremote stderr) on non-zero exit, instead of a bare CalledProcessError,
        so the failure mode is legible.
        """
        argv = self._argv("exec", code)
        result = self._runner(argv, capture_output=True, text=True)
        if getattr(result, "returncode", 0):
            raise PodExecError("exec", result.returncode, result.stdout,
                               getattr(result, "stderr", ""))
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
    def _flash_stream_cmd(addr: int, total: int, port: int, verify: bool,
                          loader: str = "native") -> str:
        """Build the on-pod flash_stream invocation (pure, for testability)."""
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.flash_stream(%d, %d, port=%d, verify=%s, loader=%r))"
            % (addr, total, port, bool(verify), loader)
        )

    @staticmethod
    def _write_mem_stream_cmd(addr: int, total: int, port: int,
                              protect) -> str:
        """Build the on-pod write_mem_stream invocation (pure, for testability)."""
        prot_arg = ("None" if not protect
                    else repr([[int(lo), int(hi)] for lo, hi in protect]))
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.write_mem_stream(%d, %d, port=%d, protect=%s))"
            % (addr, total, port, prot_arg)
        )

    @staticmethod
    def _erase_all_cmd(clkdiv: int = 8, loader: str = "flm") -> str:
        """Build the on-pod erase_all invocation (pure, for testability)."""
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.erase_all(clkdiv=%d, loader=%r))"
            % (clkdiv, loader)
        )

    def _stream_region(self, cmd: str, payload, size: int, port: int) -> dict:
        """Start the on-pod streaming receiver, then stream payload over TCP.

        cmd is executed on the pod (starts a TCP listener on port). payload is
        either an open binary file object or a bytes-like object. size is the
        total byte count the pod expects. Returns the on-pod result dict.
        """
        result: dict = {}

        def _run():
            try:
                result["out"] = self.exec(cmd)
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
                "could not connect to pod stream port %d: %r\n%s"
                % (port, exc, detail))
        # Connect used a short timeout; the transfer itself is paced by the pod
        # (it erases the whole region before reading the socket, ~85 ms/page, so
        # the initial quiet can be tens of seconds for a large image). Block for
        # the data phase rather than timing out mid-erase.
        sock.settimeout(None)
        try:
            if hasattr(payload, "read"):
                # file-like object
                while True:
                    block = payload.read(65536)
                    if not block:
                        break
                    sock.sendall(block)
            else:
                # bytes / bytearray
                mv = memoryview(payload)
                offset = 0
                while offset < len(mv):
                    sock.sendall(mv[offset:offset + 65536])
                    offset += 65536
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

    def erase_dut(self, clkdiv: int = 8, loader: str = "flm") -> dict:
        """Erase the entire DUT flash via the on-pod debug stack.

        Runs ops.erase_all() on the pod over the REPL. loader selects the
        flash algorithm: "flm" for the generic CMSIS-FLM path (works for any
        pack target), "native" for the nRF NVMC mass-erase fast-path.
        Returns the on-pod result dict {ok, ms, loader, err}.
        """
        out = self.exec(self._erase_all_cmd(clkdiv=clkdiv, loader=loader))
        return _last_dict(out)

    def flash_dut(self, image: str, target: Optional[str] = None,
                  addr: int = 0, verify: bool = True, port: int = 3333,
                  keep_attached: bool = False,
                  mass_erase: bool = False,
                  loader: Optional[str] = None) -> dict:
        """Flash a firmware image to the DUT, streamed into pod RAM (no pod FS).

        The pod runs a TCP receiver that double-buffers the image into two RAM
        buffers (Wi-Fi fills one while SWD programs the other) and never writes
        the image to its filesystem or reads the prior DUT contents. The host
        starts that receiver over the REPL and streams the file straight to it.

        For ELF images (detected by the 4-byte magic, not extension):
            - flash_ranges are derived from the DUT registry entry geometry
              (flash_base and flash_size); geometry must be declared for an ELF.
            - If mass_erase=True, ops.erase_all() is issued first (one REPL
              round-trip), before any segment is streamed.
            - Each flash segment is programmed via flash_stream (erase+program),
              which erases ONLY that segment's covered pages: inter-segment gaps
              and any region outside the segments (e.g. a settings/NVS partition)
              are NOT erased. Use mass_erase=True for a clean-chip flash.
            - Each RAM segment is written via write_mem_stream (MEM-AP, no erase).
            - Returns {ok, segments:[...], bytes, err}; stops at the first failed
              segment (err names it), leaving the earlier segments applied.
            addr is ignored for ELF images.
            loader selects the flash algorithm for both erase (if mass_erase=True)
            and per-segment programming. Defaults to "flm" for the ELF path (the
            generic CMSIS algorithm, which works for any pack target).

        For flat binaries (non-ELF): single-segment flash at addr. mass_erase
        issues ops.erase_all() before streaming. Defaults to "native" for
        backward compatibility. Returns {ok, addr, bytes, err}.

        Detaches a live USB/IP session first (re-enumerating the DUT mid-forward
        wedges the pod); pass keep_attached=True to override.
        """
        from pod.elf_loader import is_elf

        self._guard_live_attach(keep_attached)

        if is_elf(image):
            flash_ranges = getattr(self, "_elf_flash_ranges", None)
            if flash_ranges is None:
                raise ValueError(
                    "DUT flash geometry (flash_base + flash_size) must be "
                    "declared in the registry dut block to flash an ELF image")
            # Default to "flm" for ELF: the generic CMSIS algorithm works for
            # any pack target, whereas "native" is nRF-only. Both erase and
            # program use the same backend so they agree on the flash layout.
            elf_loader = loader if loader is not None else "flm"
            return self._flash_dut_elf(
                image, flash_ranges=flash_ranges, port=port, verify=verify,
                mass_erase=mass_erase, loader=elf_loader)

        # Flat binary path - default "native" preserves prior behaviour.
        bin_loader = loader if loader is not None else "native"
        if mass_erase:
            erase_result = self.erase_dut(loader=bin_loader)
            if not erase_result.get("ok"):
                return erase_result

        total = os.path.getsize(image)
        cmd = self._flash_stream_cmd(addr, total, port, verify, loader=bin_loader)
        with open(image, "rb") as f:
            return self._stream_region(cmd, f, total, port)

    def _flash_dut_elf(self, image: str, flash_ranges: list,
                       port: int = 3333, verify: bool = True,
                       mass_erase: bool = False,
                       loader: str = "flm") -> dict:
        """Flash an ELF image segment-by-segment via flash_stream / write_mem_stream.

        loader is used for both the mass_erase (if requested) and every flash
        segment, so erase and program always use the same algorithm backend.
        RAM segments use write_mem_stream regardless of loader (no flash algo
        involved). Returns an aggregated dict {ok, segments, bytes}.
        """
        from pod.elf_loader import parse_load_segments

        segments = parse_load_segments(image, flash_ranges)
        if not segments:
            raise ValueError("ELF has no PT_LOAD segments with data to program")

        if mass_erase:
            erase_result = self.erase_dut(loader=loader)
            if not erase_result.get("ok"):
                return {"ok": False, "segments": [], "bytes": 0,
                        "err": erase_result.get("err", "erase_all failed")}

        seg_results = []
        all_ok = True
        total_bytes = 0

        err = None
        for lma, data, region in segments:
            size = len(data)
            if region == "flash":
                cmd = self._flash_stream_cmd(lma, size, port, verify,
                                             loader=loader)
            else:
                # Guard the RAM write with the DUT's DECLARED flash geometry, not
                # the pod's nRF-hardcoded FLASH_TOP, so "no MEM-AP write into a
                # flash region" is correct for the actual target.
                cmd = self._write_mem_stream_cmd(lma, size, port,
                                                 protect=flash_ranges)
            seg_dict = self._stream_region(cmd, data, size, port)
            seg_dict["lma"] = lma
            seg_dict["region"] = region
            seg_results.append(seg_dict)
            if not seg_dict.get("ok"):
                # Fail fast: a partial flash is bad, so stop before writing more.
                # seg_results carries what was applied; err names the failure.
                all_ok = False
                err = "segment at 0x%08x (%s) failed: %s" % (
                    lma, region, seg_dict.get("err"))
                break
            total_bytes += size

        return {"ok": all_ok, "segments": seg_results, "bytes": total_bytes,
                "err": err}

    def _guard_live_attach(self, keep_attached: bool) -> list:
        """Before an SWD op, detach any live usbip session to this pod.

        Resetting/reflashing the DUT while it is attached over USB/IP wedges the
        forwarder (it submits to the vanished endpoint and starves Wi-Fi). So by
        default detach first; keep_attached=True overrides (you accept the risk).
        Returns the ports that were attached.
        """
        ports = self.attached_ports()
        if ports and not keep_attached:
            print("pod: detaching live USB/IP attach (ports %s) before the SWD "
                  "op - a DUT reset/flash while attached can wedge the "
                  "forwarder. Pass keep_attached=True to override."
                  % ports, file=sys.stderr)
            self.usbip_detach()
        return ports

    def reset_dut(self, mode: str = "sysreset", keep_attached: bool = False) -> dict:
        """Reset the DUT via the on-pod debug probe.

        mode: 'sysreset' (reset and run) or 'halt' (reset and halt at the
        vector). nRST and power-cycle reset need carrier hardware not present.

        Detaches a live USB/IP session first (resetting the DUT mid-forward
        wedges the pod); pass keep_attached=True to override.
        """
        self._guard_live_attach(keep_attached)
        out = self.exec(
            "import annealage_pod.debug.ops as o; print(o.reset(%r))" % mode
        )
        return _last_dict(out)

    def dut_exec(self, code: str) -> dict:
        """Run MicroPython on the DUT (turnkey) and return its stdout.

        Ensures the pod USB host + usbip server are up (unless already exporting),
        re-attaches the DUT so the host tty is known, then runs `mpremote connect
        <tty> resume exec <code>` on the DUT's own CDC REPL. Returns {tty,
        returncode, stdout, stderr}. The host-side re-attach does not touch the
        DUT (no physical re-enumeration), so it does not trip the forwarder wedge.
        Distinct from exec(), which runs on the POD.
        """
        from pod import usbip as _u
        try:
            exported = _u.list_remote(self._usbip_host())
        except Exception:  # noqa: BLE001 - server not up yet
            exported = []
        if not exported:
            _u.ensure_server(self)
        self.usbip_detach()                       # clear any stale host attachment
        # The pod's usbip server is single-import; give it a moment to release
        # the prior attachment (on TCP teardown) before the new OP_IMPORT, else
        # it answers "Request Failed".
        time.sleep(1.5)
        dev = self.usbip_attach(ensure=False)     # fresh attach -> known tty
        tty = dev.get("tty")
        if not tty:
            raise RuntimeError("DUT attached but no CDC tty appeared")
        # A freshly-enumerated CDC tty needs a moment before it answers the
        # raw-REPL handshake; settle, and retry the transient "could not enter
        # raw repl" that occurs if mpremote races the cdc_acm bind.
        out = None
        last_err = ""
        for _ in range(3):
            time.sleep(1.0)
            try:
                out = self._runner(
                    ["mpremote", "connect", tty, "resume", "exec", code],
                    capture_output=True, text=True, timeout=20)
            except _subprocess.TimeoutExpired:
                last_err = "mpremote timed out talking to %s" % tty
                out = None
                continue
            if getattr(out, "returncode", 0) == 0 or \
                    "raw repl" not in (getattr(out, "stderr", "") or "").lower():
                break
        if out is None:
            return {"tty": tty, "returncode": 1, "stdout": "", "stderr": last_err}
        return {"tty": tty, "returncode": getattr(out, "returncode", 0),
                "stdout": getattr(out, "stdout", ""),
                "stderr": getattr(out, "stderr", "")}

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

    # ── DUT register / memory peek-poke over SWD (on-pod debug stack) ──────
    # Single-shot SWD debug-interface ops, distinct from the gdb session and
    # from the USB/IP forward. Registers need the core halted (halt_dut /
    # reset_dut mode='halt'); memory is a live MEM-AP access.

    def halt_dut(self, keep_attached: bool = False) -> dict:
        """Halt the DUT core over SWD and hold it (no auto-resume).

        Required before read_reg/write_reg. Freezes the target, including its
        USB - any active USB/IP forward stalls. Detaches a live USB/IP session
        first (a frozen DUT mid-forward wedges the pod, same as reset/flash);
        keep_attached=True overrides. Returns {ok, halted, dhcsr}.
        """
        self._guard_live_attach(keep_attached)
        return _last_dict(self.exec(
            "import annealage_pod.debug.ops as o; print(o.halt())"))

    def resume_dut(self) -> dict:
        """Resume the DUT core over SWD after halt_dut / reset_dut mode='halt'."""
        return _last_dict(self.exec(
            "import annealage_pod.debug.ops as o; print(o.resume())"))

    def read_reg(self, reg) -> dict:
        """Read one DUT core register over SWD (core must be halted first).

        reg is a regsel int 0..18 or a name (r0..r12, sp, lr, pc, xpsr, msp,
        psp). Returns {ok, regsel, value} or {ok: False, err} if the core is
        running. Registers are read via the debug DCRSR/DCRDR, which require a
        halted core.
        """
        regsel = _resolve_regsel(reg)
        return _last_dict(self.exec(
            "import annealage_pod.debug.ops as o; print(o.read_reg(%d))" % regsel))

    def write_reg(self, reg, value: int) -> dict:
        """Write one DUT core register over SWD (core must be halted first)."""
        regsel = _resolve_regsel(reg)
        return _last_dict(self.exec(
            "import annealage_pod.debug.ops as o; print(o.write_reg(%d, %d))"
            % (regsel, value & 0xFFFFFFFF)))

    # MEM-AP single-transfer cap (mirrors dbgsrv.MAX_DATA); larger reads/writes
    # belong on the streaming read_dut/flash_dut paths.
    _MAX_MEM = 4096

    def read_mem(self, addr: int, length: int) -> dict:
        """Read DUT memory over SWD, returned inline as hex (<= 4096 bytes).

        A live MEM-AP read - works whether the core runs or is halted (halt_dut
        first for a coherent snapshot). For bulk dumps to a file use read_dut.
        Returns {ok, addr, length, hex}. Raises ValueError for an out-of-range
        length (fails locally instead of after a pod round-trip).
        """
        if not 0 <= length <= self._MAX_MEM:
            raise ValueError("length %d out of range 0..%d (use read_dut for bulk)"
                             % (length, self._MAX_MEM))
        return _last_dict(self.exec(
            "import annealage_pod.debug.ops as o; print(o.read_mem(%d, %d))"
            % (addr, length)))

    def write_mem(self, addr: int, data, protect=None) -> dict:
        """Write DUT memory over SWD (RAM/peripherals only, <= 4096 bytes).

        data is bytes or a hex string. `protect` is a list of [lo, hi)
        write-protected address ranges (the declared DUT flash geometry + the
        Cortex-M code-region floor, via registry.dut_protect_ranges); a write
        landing in one is refused here before the round-trip and re-checked
        on-pod as a backstop. Without `protect`, the on-pod side still applies
        the code-region backstop (addr < 0x20000000). A live MEM-AP write.
        Returns {ok, addr, length}.
        """
        import binascii as _b
        data_hex = data if isinstance(data, str) else _b.hexlify(bytes(data)).decode()
        nbytes = len(data_hex) // 2
        if nbytes > self._MAX_MEM:
            raise ValueError("data %d bytes exceeds %d" % (nbytes, self._MAX_MEM))
        end = addr + nbytes
        for lo, hi in (protect or []):
            if addr < hi and end > lo:
                return {"ok": False, "addr": addr,
                        "err": "addr 0x%08x..0x%08x overlaps write-protected "
                        "0x%08x..0x%08x" % (addr, end, int(lo), int(hi))}
        prot_arg = ("None" if not protect
                    else repr([[int(lo), int(hi)] for lo, hi in protect]))
        return _last_dict(self.exec(
            "import annealage_pod.debug.ops as o;"
            " print(o.write_mem(%d, %r, protect=%s))"
            % (addr, data_hex, prot_arg)))

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

    # ── persistent streaming REPL session (ampremote transport) ───────────

    def open_session(self, log_path=None, device=None, on_output=None,
                     buffer_bytes=None, *, mount=None, pre_exec=None,
                     pre_cp=None, soft_reset=False, unsafe_links=False,
                     reconnect=True):
        """Open a persistent streaming REPL session and return it (opened).

        Streams the target's stdout to log_path + an in-memory tail buffer and
        accepts injected stdin (session.send / .interrupt). The default target
        is the pod's own socket REPL at the resolved IPv6-first endpoint (where
        its asyncio app + aiorepl live); pass `device` to point the same session
        at any mpremote device string instead (e.g. a DUT CDC tty). Holds the
        pod's single socket-REPL slot for its lifetime - call .close() when done
        (the target keeps running). Auto-reconnects across drops / target
        reboots (reconnect=False to disable). See pod.session.ReplSession.

        Chained setup before the persistent connect (mirrors `mpremote <cmd>...
        repl`), applied in this order:
          soft_reset -> pre_cp -> pre_exec  (each a normal one-shot ampremote
              verb in its own connection; they are stateless and persist), then
          mount      (kept on the SESSION's connection - the mount fs hook RPCs
              back over it, so it cannot live in a throwaway process).
        pre_exec is a list of code strings; pre_cp a list of (src, dst) pairs;
        mount a host directory (stays mounted for the session's lifetime).
        pre_exec code must RETURN - it runs as a one-shot exec in its own
        connection before the session opens, so a non-returning snippet (e.g. a
        bare loop) hangs that exec; start long-running work via session.send
        after connecting instead.
        """
        from pod import session as _session
        if soft_reset:
            # The ampremote soft-reset verb (own connection); best-effort.
            self._runner(self._argv("soft-reset"), capture_output=True, text=True)
        for src, dst in (pre_cp or []):
            self.cp(src, dst)
        for code in (pre_exec or []):
            self.exec(code)
        target = device or self._resolver.ampremote_target(self.repl_port)
        kwargs = {"log_path": log_path, "on_output": on_output,
                  "unsafe_links": unsafe_links, "reconnect": reconnect}
        if mount is not None:
            kwargs["mount"] = os.path.abspath(mount)
        if buffer_bytes is not None:
            kwargs["buffer_bytes"] = buffer_bytes
        return _session.ReplSession(target, **kwargs).open()

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

    def spi_target(self, mode: int = 0, bits: int = 8, miso: int = 16,
                   mosi: int = 19, sck: int = 18, cs: int = 17, size: int = 1024,
                   personality: str = "stream", table_size: int = 256,
                   name: str = "spi_target") -> dict:
        """Bring up a persistent PIO SPI target on the pod.

        The pod becomes the SPI peripheral, SPI mode 0-3, 8-bit only. With
        `personality='stream'` (default), MISO replays a 0..255 counter for
        any transfer length, MOSI is captured into a `size`-byte overwrite
        ring. With `personality='regfile'`, the pod is a [reg_ptr][data...]
        register-file responder over `table_size` bytes each way (see
        spi_target_regs). Bench default pins: MISO=GP16, MOSI=GP19, SCK=GP18,
        CS=GP17.
        """
        code = (
            "import annealage_pod.peripherals as p;"
            "print(p.spi_target(mode=%d, bits=%d, miso=%d, mosi=%d, sck=%d,"
            " cs=%d, size=%d, personality=%r, table_size=%d, name=%r))"
            % (mode, bits, miso, mosi, sck, cs, size, personality, table_size, name)
        )
        return _last_dict(self.exec(code))

    def spi_target_status(self, name: str = "spi_target") -> dict:
        """Read the pod SPI target's status: byte count, transfer count, captured ring."""
        code = (
            "import annealage_pod.peripherals as p;"
            "print(p.spi_target_status(name=%r))" % name
        )
        return _last_dict(self.exec(code))

    def spi_target_regs(self, off: int = 0, length: Optional[int] = None,
                        write=None, table: str = "read",
                        name: str = "spi_target") -> dict:
        """Read or write the pod SPI target's regfile backing table from the host.

        `table` is 'read' (served on MISO) or 'write' (filled from MOSI). With
        `write` set (iterable of bytes), write it at `off` first; returns the
        window [off:off+length] (length defaults to the rest of the table).
        Only valid for a `personality='regfile'` instance.
        """
        write_arg = "None" if write is None else repr([int(b) & 0xFF for b in write])
        len_arg = "None" if length is None else str(int(length))
        code = (
            "import annealage_pod.peripherals as p;"
            "print(p.spi_target_regs(off=%d, length=%s, write=%s, table=%r,"
            " name=%r))"
            % (off, len_arg, write_arg, table, name)
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
        """The host string for the usbip client.

        Use the resolver's chosen target (IPv6-first, reachable, identity-checked)
        so the usbip path follows the same connect strategy as every other
        transport instead of a hostname/IPv4 that can be stale or unroutable.
        usbip-utils handles a v6 literal (ULA) and link-local-with-zone. Falls
        back to a static handle only if resolution fails entirely.
        """
        try:
            return self._resolver.resolve()
        except Exception:  # noqa: BLE001 - fall back to a static handle
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

    def attached_ports(self) -> list:
        """Host vhci ports currently attached to THIS pod (matched by address).

        Matches `usbip port`'s remote against this pod's static handles
        (hostname/addr4/addr6), the seed address, and the resolver's cached
        connect address. Normalises an IP literal to its canonical compressed
        form (stripping surrounding brackets, any %zone, and casing) via
        ipaddress, so a v4, ULA, global, or link-local attachment matches
        regardless of how each side spelled the address; a hostname falls back to
        a lowercased, trailing-dot-stripped compare. Uses no network.

        The cached connect address is included because usbip_attach connects to
        resolver.resolve(), which on the mDNS-fallback tier returns a live-resolved
        address that need not be one of the static handles; without it the vhci
        port that attach created would not match here and detach would no-op.
        """
        from pod import usbip as _u

        def _norm(a):
            a = (a or "").split("%")[0].strip().rstrip(".")
            if a.startswith("[") and a.endswith("]"):
                a = a[1:-1]
            try:
                return ipaddress.ip_address(a).compressed
            except ValueError:
                return a.lower()
        r = self._resolver
        mine = {_norm(a) for a in [r.hostname, r.addr4, r.cached] + list(r.addr6)
                if a}
        if self._seed_address:
            mine.add(_norm(self._seed_address))
        return [p["port"] for p in _u.ports() if _norm(p.get("remote")) in mine]

    def usbip_detach(self, port=None):
        """Detach a vhci port, or (port=None) every port attached to this pod.

        Returns True for an explicit port, or {"detached": [ports]} for the
        detach-all form.
        """
        from pod import usbip as _u
        if port is not None:
            return _u.detach(port)
        detached = []
        for p in self.attached_ports():
            try:
                _u.detach(p)
                detached.append(p)
            except Exception:  # noqa: BLE001 - continue detaching the rest
                pass
        return {"detached": detached}

    def uart_stream(self, port: int = 2000, duration: float = None,
                    on_output=None, interactive: bool = False,
                    out_path: str = None) -> dict:
        """Stream DUT UART output over TCP to stdout / on_output / file.

        Connects a TCP socket to the pod's UART listener (always-bound at boot,
        port advertised as uart_port in mDNS TXT). Two modes:
          - tail (default): stream pod->host bytes until duration or Ctrl-C;
          - interactive (interactive=True): also forward host stdin to the socket.
        Uses self._resolver.endpoint(port) for IPv6-first connect (same strategy
        as logic_analyse / flash_dut). Raw bytes, no framing. Returns
        {ok, bytes_received}.
        """
        import select as _select

        sock = None
        for _ in range(150):
            try:
                sock = socket.create_connection(
                    self._resolver.endpoint(port), timeout=5)
                break
            except OSError:
                time.sleep(0.1)
        if sock is None:
            raise RuntimeError(
                "pod UART port %d refused connection - bridge may not be running"
                % port)
        sock.settimeout(0.1)

        out_file = None
        if out_path is not None:
            out_file = open(out_path, "wb")  # noqa: SIM115 - lifetime spans loop

        bytes_received = 0
        t0 = time.monotonic()
        try:
            while True:
                try:
                    chunk = sock.recv(4096)
                    if chunk == b"":
                        break  # peer closed
                except socket.timeout:
                    chunk = b""
                if chunk:
                    bytes_received += len(chunk)
                    if on_output is not None:
                        on_output(chunk)
                    elif out_file is not None:
                        out_file.write(chunk)
                    else:
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                if interactive:
                    r, _, _ = _select.select([sys.stdin.buffer], [], [], 0)
                    if r:
                        data = sys.stdin.buffer.read1(4096)
                        if data:
                            sock.sendall(data)
                if duration is not None and (time.monotonic() - t0) >= duration:
                    break
        finally:
            sock.close()
            if out_file is not None:
                out_file.close()

        return {"ok": True, "bytes_received": bytes_received}

    def telemetry(self) -> None:
        """Read INA228 power telemetry from the pod carrier.

        Pending Phase 5: requires INA228 driver and custom carrier hardware.
        """
        raise NotImplementedError(
            "telemetry is not yet implemented - pending Phase 5 (INA228 telemetry, custom carrier)"
        )
