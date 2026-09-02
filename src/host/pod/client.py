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
import base64 as _b64
import getpass
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


# Default SWD clock divisor the host asks the pod to run at. Mirrors the pod's
# own default (annealage_pod.debug.swd_pio.DEFAULT_CLKDIV) so a host command that
# does not override clkdiv lands on the same spec-compliant clock the pod would
# pick on its own; the two live in separate codebases (host Python vs on-pod
# MicroPython) and cannot share the literal, so both name a documented constant.
# clkdiv=16 keeps the nRF52840 SWDCLK inside its 8 MHz maximum (see swd_pio.py).
DEFAULT_SWD_CLKDIV = 16


def _iid6(addr):
    """The 64-bit EUI-64 interface identifier (low 64 bits) of an IPv6 address,
    or None if `addr` is not an IPv6 literal.

    The pod's IPv6 addresses are SLAAC/EUI-64 derived, so its ULA, any global,
    and its link-local all share one interface id built from the CYW43 MAC. That
    id is stable across a ULA-prefix change or a newly-advertised global prefix,
    and is unique per NIC (a MAC cannot collide), so it identifies the pod's
    interface regardless of which prefix a vhci port happened to attach over.
    Strips a %zone and surrounding brackets first.
    """
    a = (addr or "").split("%")[0].strip().rstrip(".")
    if a.startswith("[") and a.endswith("]"):
        a = a[1:-1]
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        return None
    if ip.version != 6:
        return None
    return int(ip) & 0xFFFFFFFFFFFFFFFF


_AMPREMOTE_EXE = None


def _ampremote_exe() -> str:
    """Path to the ampremote CLI belonging to this interpreter.

    pod.client shells out to the ampremote CLI while pod.session imports
    mpremote in-process. Those must be the same distribution: ampremote ships
    both, so a single install satisfies both, but only if the subprocess
    resolves to the install this interpreter came from. A bare name would go
    through PATH instead and can pick up an unrelated copy, which silently
    puts the two transports on different code. Looking next to sys.executable
    makes them agree by construction in a venv, a uv tool install or a system
    install; the bare name remains as a fallback for layouts that put the
    script elsewhere.
    """
    global _AMPREMOTE_EXE
    if _AMPREMOTE_EXE is None:
        candidate = os.path.join(os.path.dirname(sys.executable), "ampremote")
        _AMPREMOTE_EXE = (candidate if os.access(candidate, os.X_OK)
                          else "ampremote")
    return _AMPREMOTE_EXE


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


def _last_int(stdout: str, default: int = 0) -> int:
    """Parse the last printed integer from on-pod stdout."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        try:
            return int(line)
        except ValueError:
            pass
    return default


# What the pod says when its single REPL slot is already taken, and what the
# host sees when the pod drops the connection for the same reason. Checked
# before the transport signatures, because a busy pod IS a refused connection
# and would otherwise be labelled a fault: the documented response to a broken
# pod is reset and power-cycle, which is exactly the wrong move against a pod
# that is merely in use, and destroys another agent's session.
BUSY_MARKERS = ("busy - repl in use", "repl in use by another client",
                "annealage-pod: busy")
REFUSED_MARKERS = ("connection reset by peer", "connection refused")

# ops.SwdBusy (the phase-7 re-entrancy guard) raises this on the pod when a
# guarded op is refused; it surfaces here as a plain traceback line in the
# exec's own output, not a closed connection, so it needs its own marker
# rather than falling into the generic "exception on the pod" bucket.
SWD_BUSY_MARKERS = ("annealage-pod: swd busy",)


def _classify_exec_failure(stderr, stdout):
    """Best-effort label for why a pod exec failed, from ampremote output.

    Distinguishes a pod whose REPL is held by someone else, or whose shared
    SWD session is held by someone else, from a pod that cannot be reached at
    all. Those need opposite responses, and only one of them is a fault.
    """
    blob = ((stderr or "") + "\n" + (stdout or "")).lower()
    if any(s in blob for s in SWD_BUSY_MARKERS):
        return "pod SWD busy (held by another caller)"
    if any(s in blob for s in BUSY_MARKERS):
        return "pod REPL busy (held by another client)"
    if any(s in blob for s in REFUSED_MARKERS):
        # The pod closes the connection on a second REPL client, so this is
        # most often contention rather than a dead pod. Hedged deliberately:
        # the host cannot tell this apart from a genuine network drop.
        return "pod closed the connection (usually its REPL is already in use)"
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


def resolve_caller():
    """A name for whoever is driving this client, for refusals to quote.

    A label, not a credential: nothing checks it and it grants nothing. Its only
    job is to let a collision say who the other party is instead of presenting as
    a broken pod. Resolution order, first hit wins: the claude-net agent name
    (agents already carry session:user@host), the POD_CALLER environment
    variable, then user@host/pid.
    """
    for var in ("CLAUDE_NET_AGENT", "POD_CALLER"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in some containers
        user = os.environ.get("USER") or "unknown"
    return "%s@%s/%d" % (user, socket.gethostname().split(".")[0], os.getpid())


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

    def __init__(self, verb, returncode, stdout, stderr, caller=None):
        self.returncode = returncode
        self.stdout = (stdout or "").strip()
        self.stderr = (stderr or "").strip()
        self.reason = _classify_exec_failure(self.stderr, self.stdout)
        self.caller = caller
        self.busy = self.reason.startswith("pod REPL busy") or \
            self.reason.startswith("pod SWD busy") or \
            self.reason.startswith("pod closed the connection")
        detail = self.stderr or self.stdout
        last = detail.splitlines()[-1] if detail else ""
        hint = ""
        if self.busy:
            # Say what to do instead, because the reflex for an unreachable pod
            # is a reset, and that would take another agent's session with it.
            hint = (" - this is contention, not a fault: wait and retry, or "
                    "check who holds it. Do NOT reset or power-cycle the pod")
            if caller:
                hint += " (this client is %s)" % caller
        super().__init__(
            "pod %s failed: %s (exit %s)%s%s"
            % (verb, self.reason, returncode,
               (": " + last) if last else "", hint))


class PodConflictError(RuntimeError):
    """A displacing operation was refused because another caller holds the
    resource it would tear down. Raised host-side, before any pod round trip
    that would do the damage - distinct from PodExecError.busy, which is a
    REPL contention error surfaced only after the pod has already refused a
    connection. Carries the pod's holder record for `resource` so a caller
    can decide whether to ask before retrying with force=True."""

    def __init__(self, resource, holder, caller=None):
        self.resource = resource
        self.holder = holder or {}
        self.caller = caller
        who = self.holder.get("caller") or "another caller"
        super().__init__(
            "refused: %s is held by %s - this would displace a live session; "
            "pass force=True to bump it (this client is %s)"
            % (resource, who, caller or "unknown"))


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
        # A label for whoever is driving this client, resolved once. It grants
        # nothing and nothing checks it; its job is to let a refusal name a
        # party instead of reading as a broken pod.
        self.caller = resolve_caller()
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
        without hardcoding target addresses, and the declared target_family,
        which is the CMSIS device name the generic FLM path resolves a pack
        with.
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
        pod._cmsis_device = dut.get("target_family")
        # The registry keeps whatever mDNS advertised. control_port is absent on
        # a pod whose firmware predates the holder listener, and that absence is
        # what tells the gate it has no record to consult.
        pod._entry = entry
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

    def repl_holder(self, timeout: float = 3.0) -> Optional[str]:
        """The pod's own account of who holds its REPL, or None.

        The pod refuses a second REPL client with a one-line BUSY notice naming
        the holder, but the transport reports only the closed socket, so that
        line never reaches the caller. This opens a bare connection to read it.

        Only call this once a failure has already been classified as contention.
        If the slot happens to be free the pod will attach this connection
        instead of refusing it, and although it is closed immediately, that is a
        moment of holding a slot that belongs to someone else.
        """
        import socket as _socket
        host, _port = self._resolver.endpoint(self.repl_port)
        try:
            infos = _socket.getaddrinfo(host, self.repl_port, 0,
                                        _socket.SOCK_STREAM)
        except Exception:  # noqa: BLE001 - unresolvable is simply "unknown"
            return None
        for family, socktype, proto, _c, sockaddr in infos:
            sock = None
            try:
                sock = _socket.socket(family, socktype, proto)
                sock.settimeout(timeout)
                sock.connect(sockaddr)
                data = sock.recv(400).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - try the next address
                continue
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            line = (data or "").strip().splitlines()
            if line and "BUSY" in line[0]:
                return line[0]
        return None

    # ── who holds what (the pod's control listener, not its REPL) ─────────

    def who(self, resource=None, timeout: float = 3.0) -> dict:
        """The pod's holder record, or {} when it cannot be reached.

        Asks the control listener, NOT the REPL: the REPL is the contended thing,
        so a caller that cannot get in is exactly the one that needs the answer.
        Returns None when the answer could not be obtained, and {} only when
        the pod genuinely reports nobody holding anything. A gate deciding
        whether to displace someone must not read "I could not find out" as
        "the bench is free", so the two are different values rather than
        different shades of empty.
        """
        import json as _json
        import socket as _socket
        port = self._control_port()
        if port is None:
            return None
        host, _p = self._resolver.endpoint(port)
        req = ("who %s" % resource if resource else "who") + "\n"
        try:
            infos = _socket.getaddrinfo(host, port, 0, _socket.SOCK_STREAM)
        except Exception:  # noqa: BLE001
            return None
        for family, socktype, proto, _c, sockaddr in infos:
            sock = None
            try:
                sock = _socket.socket(family, socktype, proto)
                sock.settimeout(timeout)
                sock.connect(sockaddr)
                sock.send(req.encode())
                data = b""
                while b"\n" not in data:
                    chunk = sock.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                body = _json.loads(data.decode().strip())
            except Exception:  # noqa: BLE001 - try the next address
                continue
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
            if body.get("ok"):
                return body.get("holders") or {}
        return None

    def who_available(self) -> bool:
        """Whether this pod advertises a control listener at all.

        False means the pod's firmware predates it, so the gate has no holder
        record to consult and says so instead of assuming the bench is free.
        """
        return self._control_port() is not None

    def _control_port(self):
        entry = getattr(self, "_entry", None) or {}
        return entry.get("control_port")

    def usbip_held_by_other(self):
        """Whether the pod's DUT export is imported by someone that is not us.

        The pod's usbip server knows a busid is imported but not which host did
        it, so this pairs that with our own vhci table: imported, and no port of
        ours attached to this pod, means another host holds it. Returns None when
        the pod cannot say, which is not the same as False.
        """
        holders = self.who()
        if holders is None:
            return None                      # could not find out
        if "usbip" not in holders:
            return False                     # the pod says nobody imported it
        return not self.attached_ports()

    def _argv(self, verb: str, *args: str) -> List[str]:
        """Build the ampremote argv list for a given verb and arguments.

        Returns a list starting with [<ampremote>, 'connect',
        'socket://HOST:PORT', verb, *args] where HOST is the resolved connect
        target (IPv6 literals bracketed). No subprocess is invoked.
        """
        connect_target = self._resolver.ampremote_target(self.repl_port)
        return [_ampremote_exe(), "connect", connect_target, verb] + list(args)

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
                               getattr(result, "stderr", ""), caller=self.caller)
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
                          loader: str = "native", caller=None) -> str:
        """Build the on-pod flash_stream invocation (pure, for testability).

        caller feeds the pod-side SWD re-entrancy guard (ops._guarded); None
        (the default, e.g. a direct call with no client wrapper) is never
        gated, matching every other guarded-op command builder below.
        """
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.flash_stream(%d, %d, port=%d, verify=%s, loader=%r, caller=%r))"
            % (addr, total, port, bool(verify), loader, caller)
        )

    @staticmethod
    def _write_mem_stream_cmd(addr: int, total: int, port: int,
                              protect, caller=None) -> str:
        """Build the on-pod write_mem_stream invocation (pure, for testability)."""
        prot_arg = ("None" if not protect
                    else repr([[int(lo), int(hi)] for lo, hi in protect]))
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.write_mem_stream(%d, %d, port=%d, protect=%s, caller=%r))"
            % (addr, total, port, prot_arg, caller)
        )

    @staticmethod
    def _erase_all_cmd(clkdiv: int = DEFAULT_SWD_CLKDIV, loader: str = "native",
                       caller=None) -> str:
        """Build the on-pod erase_all invocation (pure, for testability)."""
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.erase_all(clkdiv=%d, loader=%r, caller=%r))"
            % (clkdiv, loader, caller)
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

    # ── generic CMSIS flash algorithm (loader="flm") ─────────────────────
    # The pod carries no flash algorithms. One is extracted here from the
    # target's CMSIS Device Family Pack and installed over the REPL, in base64
    # chunks so a large vendor algorithm is never one huge source literal on
    # the pod. It stays installed for the pod's VM lifetime.

    _FLM_B64_CHUNK = 6144

    @staticmethod
    def _stage_flm_cmd(b64: Optional[str] = None) -> str:
        """Build the on-pod stage_flm_blob invocation (pure, for testability)."""
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.stage_flm_blob(%s))" % ("" if b64 is None else repr(b64))
        )

    @staticmethod
    def _set_flm_algo_cmd(meta: dict) -> str:
        """Build the on-pod set_flm_algo invocation from metadata (no image).

        The image is not in meta: it was staged in chunks beforehand, and
        set_flm_algo takes the staged bytes when "instructions" is absent.
        """
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.set_flm_algo(%r))" % (meta,)
        )

    def install_flm_algo(self, algo: dict) -> dict:
        """Install a CMSIS flash algorithm on the pod for loader="flm".

        Stages the algorithm image in base64 chunks, checks the pod received
        every byte, then installs it with the rest of the algo dict. Returns
        the pod's summary {installed, name, blob_bytes, ...}.

        Raises:
            RuntimeError: if the staged byte count does not match what was sent.
        """
        image = algo["instructions"]
        b64 = _b64.b64encode(image).decode("ascii")

        self.exec(self._stage_flm_cmd())          # start a fresh image
        staged = 0
        for i in range(0, len(b64), self._FLM_B64_CHUNK):
            out = self.exec(self._stage_flm_cmd(b64[i:i + self._FLM_B64_CHUNK]))
            staged = _last_int(out, staged)
        if staged != len(image):
            raise RuntimeError(
                "pod staged %d of %d algorithm bytes; transfer incomplete"
                % (staged, len(image)))

        meta = {k: v for k, v in algo.items() if k != "instructions"}
        info = _last_dict(self.exec(self._set_flm_algo_cmd(meta)))
        self._flm_installed = info if info.get("installed") else None
        return info

    def flm_algo_info(self) -> dict:
        """What algorithm the pod currently has installed, if any."""
        return _last_dict(self.exec(
            "import annealage_pod.debug.ops as o;print(o.flm_algo_info())"))

    def resolve_flm_algo(self, device: Optional[str] = None, addr=None,
                         pack=None, allow_download: bool = False,
                         **kwargs) -> dict:
        """Build the algo dict for this DUT from its CMSIS pack.

        device defaults to the registry dut block's declared target_family,
        which is the CMSIS device name (e.g. "nRF52840_xxAA"). addr picks the
        algorithm covering that flash address when a device has several.
        Downloading a pack is opt-in; see pod.cmsis_pack.
        """
        from pod import cmsis_pack

        device = device or getattr(self, "_cmsis_device", None)
        if not device:
            raise ValueError(
                "no CMSIS device name: declare the DUT's target_family in the "
                "registry (pod dut identify --dut-family <name>) or pass device=")
        return cmsis_pack.algo_for_device(
            device, addr=addr, pack=pack, allow_download=allow_download,
            **kwargs)

    def ensure_flm_algo(self, addr=None, force: bool = False, **kwargs) -> dict:
        """Make sure the pod has an algorithm installed for loader="flm".

        Free once this client has installed one, so a mass-erase followed by
        several flashes does not re-ship the image or even re-query the pod.
        Falls back to asking the pod (a pod may already carry one from an
        earlier session) before resolving a pack. force=True reinstalls: use it
        after changing DUT, or to select an algorithm for a different flash
        region.
        """
        installed = getattr(self, "_flm_installed", None)
        if installed and not force:
            return installed
        if not force:
            info = self.flm_algo_info()
            if info.get("installed"):
                self._flm_installed = info
                return info
        info = self.install_flm_algo(self.resolve_flm_algo(addr=addr, **kwargs))
        self._flm_installed = info
        return info

    def _erase_all_on_pod(self, clkdiv: int = DEFAULT_SWD_CLKDIV,
                          loader: str = "native") -> dict:
        """Run ops.erase_all() on the pod. No usbip guard - the caller's job,
        so an erase nested inside flash_dut's mass_erase does not re-gate and
        re-log an eviction that the enclosing call already settled."""
        if loader == "flm":
            self.ensure_flm_algo()
        out = self.exec(self._erase_all_cmd(clkdiv=clkdiv, loader=loader,
                                            caller=self.caller))
        return _last_dict(out)

    def erase_dut(self, clkdiv: int = DEFAULT_SWD_CLKDIV, loader: str = "native",
                  keep_attached: bool = False, force: bool = False) -> dict:
        """Erase the entire DUT flash via the on-pod debug stack.

        Runs ops.erase_all() on the pod over the REPL. loader selects the
        flash algorithm: "native" (default) for the nRF NVMC mass-erase
        fast-path, "flm" for the generic CMSIS path, which resolves and
        installs the target's CMSIS-pack algorithm first if the pod has none.
        Returns the on-pod result dict {ok, ms, loader, err[, stole_from]}.

        erase_all() halts the core to run, the same DUT-freeze reset and flash
        already guard against, so it goes through the same _guard_live_attach:
        detaches a live USB/IP session first (keep_attached=True overrides),
        and refuses when another host holds it unless force=True bumps it.
        """
        stolen = self._guard_live_attach(keep_attached, force=force)
        result = self._erase_all_on_pod(clkdiv, loader)
        if stolen:
            result["stole_from"] = stolen
        return result

    def flash_crc(self, addr: int, length: int, clkdiv: int = DEFAULT_SWD_CLKDIV) -> dict:
        """CRC32 of a DUT flash region, read back over SWD (the pod-side
        ops.flash_crc). Returns {ok, crc, addr, length, err}."""
        out = self.exec(
            "import annealage_pod.debug.ops as o; "
            "print(o.flash_crc(%d, %d, clkdiv=%d, caller=%r))"
            % (addr, length, clkdiv, self.caller)
        )
        return _last_dict(out)

    # #35: a single flash_stream erase+program of a large region blocks the pod's
    # single-core loop for seconds, starving cyw43 Wi-Fi RX until the TCP transfer
    # resets (a ~420KB flash reproduces it; 64KB survives). Split a large region
    # into <=64KB flashes - each a short op the loop recovers between - ending on
    # absolute 64KB boundaries so no two sub-flashes share a flash page (their
    # erases cannot wipe a neighbour's just-programmed data; 64KB is a multiple of
    # every common flash page size).
    _FLASH_STREAM_CHUNK = 64 * 1024

    def _flash_region_chunked(self, addr: int, data: bytes, port: int,
                              verify: bool, loader: str) -> dict:
        """Flash a flash region in <=64KB page-aligned sub-flashes (#35), then
        end-to-end verify the whole region once. Returns {ok, addr, bytes[,
        verify, err]}. A sub-flash failure stops and names its address."""
        total = len(data)
        off = 0
        while off < total:
            sub_start = addr + off
            boundary = ((sub_start // self._FLASH_STREAM_CHUNK) + 1) \
                * self._FLASH_STREAM_CHUNK
            sub_end = min(addr + total, boundary)
            n = sub_end - sub_start
            cmd = self._flash_stream_cmd(sub_start, n, port, verify, loader=loader,
                                         caller=self.caller)
            sub = self._stream_region(cmd, data[off:off + n], n, port)
            if not sub.get("ok"):
                return {"ok": False, "addr": addr, "bytes": off,
                        "err": "flash sub-chunk at 0x%08x failed: %s"
                               % (sub_start, sub.get("err"))}
            off += n
        result = {"ok": True, "addr": addr, "bytes": total}
        if verify:
            v = self._verify_flashed(addr, data)
            result["verify"] = v
            if not v.get("ok"):
                result["ok"] = False
                result["err"] = "end-to-end verify: %s" % v.get("err")
        return result

    def _verify_flashed(self, lma: int, source: bytes, clkdiv: int = DEFAULT_SWD_CLKDIV,
                        retries: int = 3) -> dict:
        """End-to-end verify a just-flashed region: CRC32 the source bytes and
        compare to a pod-side read-back CRC over SWD.

        The streaming program path only verifies bytes it programs per chunk, so
        a chunk lost mid-stream (Wi-Fi reset) or a flaky per-chunk verify leaves a
        silent hole. This re-reads the whole region and compares.

        Retries the read-back to absorb the flaky-bulk-read pattern: it PASSES as
        soon as one read-back CRC matches the source, and FAILS only if none of
        `retries` reads match - a genuine hole never matches, while a transient
        read glitch is corrected by a clean retry. Returns {ok, crc[, err]}.
        """
        import zlib
        want = zlib.crc32(source) & 0xFFFFFFFF
        last = "no read-back attempted"
        for _ in range(max(1, retries)):
            r = self.flash_crc(lma, len(source), clkdiv=clkdiv)
            if not r.get("ok"):
                last = "read-back error: %s" % r.get("err")
                continue
            got = int(r.get("crc", -1)) & 0xFFFFFFFF
            if got == want:
                return {"ok": True, "crc": want}
            last = "crc mismatch: flash=0x%08x source=0x%08x" % (got, want)
        return {"ok": False, "err": last, "source_crc": want}

    def flash_dut(self, image: str, target: Optional[str] = None,
                  addr: int = 0, verify: bool = True, port: int = 3333,
                  keep_attached: bool = False,
                  mass_erase: bool = False,
                  loader: Optional[str] = None,
                  force: bool = False) -> dict:
        """Flash a firmware image to the DUT, streamed into pod RAM (no pod FS).

        The pod runs a TCP receiver that double-buffers the image into two RAM
        buffers (Wi-Fi fills one while SWD programs the other) and never writes
        the image to its filesystem. The host starts that receiver over the REPL
        and streams the file straight to it.

        With verify=True (default) each flash region is checked end-to-end after
        programming: the region is re-read over SWD (ops.flash_crc) and its CRC32
        compared to the source. The streaming program path verifies only the
        bytes it programs per chunk, so a chunk lost mid-stream (e.g. a Wi-Fi
        reset) would otherwise leave a silent hole; the read-back CRC catches it
        (and retries absorb the occasional flaky bulk read). A verify failure
        makes the result ok=False and names the region.

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
            and per-segment programming. Defaults to "native" (nRF NVMC);
            "flm" runs the target's CMSIS-pack algorithm, resolved and installed
            on the pod first (see ensure_flm_algo) if it has none.

        For flat binaries (non-ELF): single-segment flash at addr. mass_erase
        issues ops.erase_all() before streaming. Defaults to "native" for
        backward compatibility. Returns {ok, addr, bytes, err}.

        Detaches a live USB/IP session first (re-enumerating the DUT mid-forward
        wedges the pod); pass keep_attached=True to override. Refuses when
        another host holds that attachment, naming it, unless force=True bumps
        it; a forced bump adds stole_from to the result.
        """
        from pod.elf_loader import is_elf

        stolen = self._guard_live_attach(keep_attached, force=force)

        if is_elf(image):
            flash_ranges = getattr(self, "_elf_flash_ranges", None)
            if flash_ranges is None:
                raise ValueError(
                    "DUT flash geometry (flash_base + flash_size) must be "
                    "declared in the registry dut block to flash an ELF image")
            # Default to "native" (nRF NVMC), the validated per-family path that
            # needs no algorithm install. Both erase and program use the same
            # backend so they agree on the flash layout.
            elf_loader = loader if loader is not None else "native"
            if elf_loader == "flm":
                self.ensure_flm_algo(addr=flash_ranges[0][0])
            result = self._flash_dut_elf(
                image, flash_ranges=flash_ranges, port=port, verify=verify,
                mass_erase=mass_erase, loader=elf_loader)
        else:
            # Flat binary path - default "native" preserves prior behaviour.
            bin_loader = loader if loader is not None else "native"
            if bin_loader == "flm":
                self.ensure_flm_algo(addr=addr)
            if mass_erase:
                # No further gate: the guard above already settled whether this
                # call may proceed at all, and re-checking here would refuse a
                # forced bump the caller already paid for.
                erase_result = self._erase_all_on_pod(loader=bin_loader)
                if not erase_result.get("ok"):
                    result = erase_result
                else:
                    result = None
            else:
                result = None
            if result is None:
                # Flash in <=64KB page-aligned sub-flashes (#35 loop-starvation)
                # and end-to-end verify the whole image (a mid-stream drop the
                # per-chunk verify misses). Read the image whole - flash images
                # are small, and the verify reads it anyway.
                with open(image, "rb") as f:
                    data = f.read()
                result = self._flash_region_chunked(addr, data, port, verify,
                                                    bin_loader)

        if stolen:
            result = dict(result, stole_from=stolen)
        return result

    def _flash_dut_elf(self, image: str, flash_ranges: list,
                       port: int = 3333, verify: bool = True,
                       mass_erase: bool = False,
                       loader: str = "native") -> dict:
        """Flash an ELF image segment-by-segment via flash_stream / write_mem_stream.

        loader is used for both the mass_erase (if requested) and every flash
        segment, so erase and program always use the same algorithm backend.
        RAM segments use write_mem_stream regardless of loader (no flash algo
        involved). Returns an aggregated dict {ok, segments, bytes}. Assumes
        the caller (flash_dut) already ran the usbip guard; does not re-gate.
        """
        from pod.elf_loader import parse_load_segments

        segments = parse_load_segments(image, flash_ranges)
        if not segments:
            raise ValueError("ELF has no PT_LOAD segments with data to program")

        if mass_erase:
            erase_result = self._erase_all_on_pod(loader=loader)
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
                # <=64KB sub-flashes (#35) + end-to-end CRC verify of the segment:
                # the per-chunk verify in the program path cannot catch a chunk
                # lost mid-stream, so the whole segment is re-read and compared.
                seg_dict = self._flash_region_chunked(lma, data, port, verify,
                                                      loader)
            else:
                # Guard the RAM write with the DUT's DECLARED flash geometry, not
                # the pod's nRF-hardcoded FLASH_TOP, so "no MEM-AP write into a
                # flash region" is correct for the actual target. RAM segments are
                # MEM-AP writes (no erase/stream-drop risk) and are not CRC-checked.
                cmd = self._write_mem_stream_cmd(lma, size, port,
                                                 protect=flash_ranges,
                                                 caller=self.caller)
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

    def _gate_usbip(self, force: bool) -> Optional[dict]:
        """Refuse a displacing SWD op when another host holds the USB/IP import.

        Raises PodConflictError naming the holder unless force=True. A pod that
        cannot say who holds it (predates the control port, or is otherwise
        unreachable) fails open, matching behaviour from before this gate
        existed - the gate adds a precaution on top of the pre-existing detach,
        not a new requirement for using the pod.

        force does not add a pod-side eviction capability: the pod's usbip
        server already enforces single-import on its own (see
        conflict-legibility.md "Current state, verified"), so a genuinely live
        cross-host import that this host cannot reach still refuses there
        regardless of this check. force's effect is local: it skips this
        host's own pre-emptive refusal and logs the bump on the pod for
        whoever is watching its console, so operators who know better are not
        blocked by a check that is advisory rather than authoritative.

        Returns the pod's holder record for "usbip" when force actually
        bypassed a real conflict, so the caller can report stole_from; None
        when nobody else holds it, or the pod could not say.
        """
        if not self.usbip_held_by_other():
            return None
        info = (self.who() or {}).get("usbip") or {}
        if not force:
            raise PodConflictError("usbip", info, caller=self.caller)
        victim = info.get("caller") or "unknown"
        try:
            self.exec("import annealage_pod.holders as h; "
                      "h.evict(%r, %r, %r)" % ("usbip", self.caller, victim))
        except Exception:  # noqa: BLE001 - best-effort audit log, never fatal
            pass
        return info

    def _guard_live_attach(self, keep_attached: bool, force: bool = False):
        """Before an SWD op, refuse or detach a live usbip session to this pod.

        Resetting/reflashing/erasing the DUT while it is attached over USB/IP
        wedges the forwarder (it submits to the vanished endpoint and starves
        Wi-Fi), so by default detach first; keep_attached=True overrides (you
        accept the risk on your own attachment). First checks whether another
        host holds it (see _gate_usbip) and refuses naming them rather than
        silently tearing their session down; force=True bumps it. Returns the
        pod's holder record if a forced bump displaced someone, else None.
        """
        stolen = self._gate_usbip(force)
        ports = self.attached_ports()
        if ports and not keep_attached:
            print("pod: detaching live USB/IP attach (ports %s) before the SWD "
                  "op - a DUT reset/flash while attached can wedge the "
                  "forwarder. Pass keep_attached=True to override."
                  % ports, file=sys.stderr)
            self.usbip_detach()
        return stolen

    def reset_dut(self, mode: str = "sysreset", keep_attached: bool = False,
                  force: bool = False) -> dict:
        """Reset the DUT: 'sysreset'/'halt' over the on-pod debug probe, or
        'nrst' over the dedicated reset wire.

        mode: 'sysreset' (reset and run) or 'halt' (reset and halt at the
        vector), both over SWD; or 'nrst' to pulse the dedicated DUT reset wire
        (pod GP13, see hardware-setup.md section 5g). 'nrst' needs that one wire
        but no carrier hardware, and is the path of last resort because it does
        not need a working SWD session. Its result carries "level", the reset
        line after release: 0 means the line did not come back up, so the DUT
        has no reset pull-up or is holding its own reset. Power-cycle reset
        ('power') does need carrier hardware and is not available here.

        Detaches a live USB/IP session first (resetting the DUT mid-forward
        wedges the pod); pass keep_attached=True to override. Refuses when
        another host holds that attachment, naming it, unless force=True bumps
        it; a forced bump adds stole_from to the result.
        """
        stolen = self._guard_live_attach(keep_attached, force=force)
        out = self.exec(
            "import annealage_pod.debug.ops as o; print(o.reset(%r, caller=%r))"
            % (mode, self.caller)
        )
        result = _last_dict(out)
        if stolen:
            result["stole_from"] = stolen
        return result

    def recover_dut_repl(self, device: str, *, settle: float = 0.4,
                         read_wait: float = 0.6) -> dict:
        """Un-stick a forwarded MicroPython DUT REPL over its CDC tty.

        Build-agnostic (drives the tty; no SWD, no symbols, no firmware
        assumptions): sends Ctrl-C to break any running program, then Ctrl-B to
        leave a stuck raw REPL for the friendly one - a failed mpremote raw-entry
        (its "raw REPL" banner gets gated when DTR is low) can latch the DUT in
        raw mode, where CR-terminated lines never execute and nothing echoes, so
        it looks dead. Then it nudges a fresh prompt and reports whether the
        friendly '>>>' came back. Holding the tty open keeps DTR asserted so the
        recovery output is not itself gated.

        device: the DUT CDC tty from dut_link(action="up") (e.g. '/dev/ttyACM0').
        Returns {ok, device, recovered, prompt_seen, was_raw, output}; on a busy
        or absent tty returns {ok: False, err}. If it does not recover, escalate:
        reset_dut (SWD reset -> fresh FRIENDLY REPL), then a power-cycle for a
        truly wedged DUT.
        """
        from pod import session as _session
        try:
            sess = _session.ReplSession(
                device, reconnect=False, read_timeout=0.2).open()
        except Exception as exc:  # noqa: BLE001 - busy / absent tty
            return {"ok": False, "device": device,
                    "err": "could not open DUT tty %s: %r" % (device, exc)}
        try:
            cursor = sess.tell()
            sess.interrupt()                    # Ctrl-C: break a running program
            time.sleep(settle)
            # Probe before the Ctrl-B: a CR draws '>>>' from the friendly REPL,
            # while a raw REPL neither echoes nor executes CR-terminated lines.
            # The probe is what tells the two apart, because Ctrl-B answers with
            # the friendly banner either way and so cannot discriminate.
            probe_cursor = sess.tell()
            sess.send(b"\r", newline=False)
            time.sleep(read_wait)
            probe = sess.read_since(probe_cursor).get("text", "")
            probe_silent = ">>>" not in probe
            sess.send(b"\x02", newline=False)    # Ctrl-B: raw REPL -> friendly
            time.sleep(settle)
            sess.send(b"\r", newline=False)      # nudge a fresh prompt
            time.sleep(read_wait)
            text = sess.read_since(cursor).get("text", "")
        finally:
            sess.close()
        prompt = ">>>" in text
        # Two independent signs of raw mode: the DUT printed the raw-REPL banner
        # (direct evidence), or the probe drew nothing and the Ctrl-B then
        # produced a prompt (the transition observed). Requiring the transition
        # alone would misreport a DUT that answers neither as latched when it is
        # simply unresponsive, which sends the caller down the wrong branch of
        # the troubleshooting tree.
        was_raw = "raw REPL" in text or (probe_silent and prompt)
        return {"ok": True, "device": device, "recovered": prompt,
                "prompt_seen": prompt, "was_raw": was_raw,
                "output": text}

    def _rebuild_dut_link(self) -> Optional[str]:
        """Tear the host attachment down and build a fresh one; return its tty.

        Ensures the pod USB host + usbip server if the pod exports nothing, then
        detaches and re-attaches so the tty is known to be live. The re-attach is
        host-side only (no physical re-enumeration), so it does not trip the
        forwarder wedge. This is also the recovery for a stale export: the DUT
        re-enumerated while the vhci port stayed attached, leaving a device node
        that no longer answers.
        """
        from pod import usbip as _u
        try:
            exported = _u.list_remote(self._usbip_host())
        except Exception:  # noqa: BLE001 - server not up yet
            exported = []
        if not exported:
            _u.ensure_server(self)
        self.usbip_detach()                       # clear any stale attachment
        # The pod's usbip server is single-import; give it a moment to release
        # the prior attachment (on TCP teardown) before the new OP_IMPORT, else
        # it answers "Request Failed".
        time.sleep(1.5)
        return self.usbip_attach(ensure=False).get("tty")

    # Stderr signatures that mean mpremote could not TALK to the device, as
    # opposed to the device running the code and raising. Only the former says
    # the tty is stale and worth rebuilding the link for.
    _TRANSPORT_ERRORS = ("could not enter raw repl", "failed to access",
                         "no such file or directory", "could not open port",
                         "device reports readiness", "permission denied")

    @classmethod
    def _is_transport_error(cls, stderr: str) -> bool:
        """True when stderr says the link failed rather than the DUT's code did.

        A DUT-side exception comes back as a traceback, which is a successful
        conversation with a working device, so it must never be mistaken for a
        dead tty: acting on that would re-run the caller's code.
        """
        low = (stderr or "").lower()
        if "traceback" in low:
            return False
        return any(sig in low for sig in cls._TRANSPORT_ERRORS)

    def _dut_exec_on(self, tty: str, code: str, settle: bool) -> dict:
        """Run `code` over mpremote on `tty`.

        Returns {returncode, stdout, stderr, transport_error}, the last saying
        whether the failure was in reaching the device rather than in the code.

        settle=True waits before the first attempt, which a freshly-enumerated CDC
        tty needs before it answers the raw-REPL handshake; a tty already up does
        not. Either way the transient "could not enter raw repl" that occurs when
        mpremote races the cdc_acm bind is retried.
        """
        last = None
        for attempt in range(3):
            if settle or attempt:
                time.sleep(1.0)
            try:
                out = self._runner(
                    ["mpremote", "connect", tty, "resume", "exec", code],
                    capture_output=True, text=True, timeout=20)
            except _subprocess.TimeoutExpired:
                last = {"returncode": 1, "stdout": "",
                        "stderr": "mpremote timed out talking to %s" % tty,
                        "transport_error": True}
                continue
            rc = getattr(out, "returncode", 0)
            stderr = getattr(out, "stderr", "") or ""
            last = {"returncode": rc, "stdout": getattr(out, "stdout", ""),
                    "stderr": stderr,
                    "transport_error": rc != 0 and self._is_transport_error(stderr)}
            # Retry only the raw-REPL race; anything else is the device's answer.
            if rc == 0 or "raw repl" not in stderr.lower():
                return last
        return last

    def dut_tty(self, ensure: bool = True) -> str:
        """The DUT's CDC tty, bringing the USB/IP link up if it is not already.

        The single place that answers "where is the DUT", so the CLI and the MCP
        handler cannot drift into two behaviours or two error messages. Raises
        RuntimeError when the link comes up but no tty appears, which is a real
        state (the export exists, the CDC interface has not enumerated) and not
        something a caller can act on by retrying immediately.
        """
        from pod import usbip as _u
        tty = _u.forwarded_tty(self.attached_ports())
        if tty:
            return tty
        dev = self.usbip_attach(ensure=ensure)
        tty = dev.get("tty")
        if not tty:
            raise RuntimeError(
                "the pod's DUT attached (busid %s) but no CDC tty appeared; "
                "check `usbip port` and dmesg, then pass the device explicitly"
                % dev.get("busid"))
        return tty

    def dut_exec(self, code: str) -> dict:
        """Run MicroPython on the DUT (turnkey) and return its stdout.

        Runs `mpremote connect <tty> resume exec <code>` on the DUT's own CDC
        REPL. Returns {tty, returncode, stdout, stderr, reattached}. Distinct
        from exec(), which runs on the POD.

        An attachment THIS pod already holds is used as-is, found by matching
        its vhci port against the pod's own; otherwise a fresh one is built,
        which is the only route that pays the settle delays and so the difference
        between a roughly one-second call and a ten-second one.

        A reused attachment that turns out not to answer is rebuilt once and
        retried, since a device node surviving a DUT re-enumeration is exactly
        the stale-export case rebuilding recovers. A DUT-side exception is not
        that: it is a working link running failing code, so it is returned as-is.
        """
        from pod import usbip as _u
        tty = _u.forwarded_tty(self.attached_ports())
        reused = tty is not None
        reattached = tty is None
        if tty is None:
            tty = self._rebuild_dut_link()
        if not tty:
            raise RuntimeError("DUT attached but no CDC tty appeared")

        res = self._dut_exec_on(tty, code, settle=reattached)
        if res.get("transport_error") and reused:
            # The reused node could not be reached, which is the stale-export
            # case: rebuild and try once. Gated on the transport flag rather than
            # on returncode, because a DUT-side exception is a working link
            # running failing code and re-running it would repeat its effects.
            fresh = self._rebuild_dut_link()
            if fresh:
                tty, reattached = fresh, True
                res = self._dut_exec_on(tty, code, settle=True)
        res.pop("transport_error", None)
        return dict(res, tty=tty, reattached=reattached)

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
            "import annealage_pod.debug.ops as o; print(o.discover(caller=%r))"
            % (self.caller,))
        return _last_dict(out)

    # ── DUT register / memory peek-poke over SWD (on-pod debug stack) ──────
    # Single-shot SWD debug-interface ops, distinct from the gdb session and
    # from the USB/IP forward. Registers need the core halted (halt_dut /
    # reset_dut mode='halt'); memory is a live MEM-AP access.

    def halt_dut(self, keep_attached: bool = False, force: bool = False) -> dict:
        """Halt the DUT core over SWD and hold it (no auto-resume).

        Required before read_reg/write_reg. Freezes the target, including its
        USB - any active USB/IP forward stalls. Detaches a live USB/IP session
        first (a frozen DUT mid-forward wedges the pod, same as reset/flash);
        keep_attached=True overrides. Shares _guard_live_attach with the other
        SWD ops, so it also refuses when another host holds that attachment
        unless force=True bumps it. Returns {ok, halted, dhcsr[, stole_from]}.
        """
        stolen = self._guard_live_attach(keep_attached, force=force)
        result = _last_dict(self.exec(
            "import annealage_pod.debug.ops as o; print(o.halt(caller=%r))"
            % (self.caller,)))
        if stolen:
            result["stole_from"] = stolen
        return result

    def resume_dut(self) -> dict:
        """Resume the DUT core over SWD after halt_dut / reset_dut mode='halt'."""
        return _last_dict(self.exec(
            "import annealage_pod.debug.ops as o; print(o.resume(caller=%r))"
            % (self.caller,)))

    def read_reg(self, reg) -> dict:
        """Read one DUT core register over SWD (core must be halted first).

        reg is a regsel int 0..18 or a name (r0..r12, sp, lr, pc, xpsr, msp,
        psp). Returns {ok, regsel, value} or {ok: False, err} if the core is
        running. Registers are read via the debug DCRSR/DCRDR, which require a
        halted core.
        """
        regsel = _resolve_regsel(reg)
        return _last_dict(self.exec(
            "import annealage_pod.debug.ops as o; print(o.read_reg(%d, caller=%r))"
            % (regsel, self.caller)))

    def write_reg(self, reg, value: int) -> dict:
        """Write one DUT core register over SWD (core must be halted first)."""
        regsel = _resolve_regsel(reg)
        return _last_dict(self.exec(
            "import annealage_pod.debug.ops as o; "
            "print(o.write_reg(%d, %d, caller=%r))"
            % (regsel, value & 0xFFFFFFFF, self.caller)))

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
            "import annealage_pod.debug.ops as o; "
            "print(o.read_mem(%d, %d, caller=%r))"
            % (addr, length, self.caller)))

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
            " print(o.write_mem(%d, %r, protect=%s, caller=%r))"
            % (addr, data_hex, prot_arg, self.caller)))

    @staticmethod
    def _dump_stream_cmd(addr: int, length: int, port: int, caller=None) -> str:
        """Build the on-pod dump_stream invocation (pure, for testability)."""
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.dump_stream(%d, %d, port=%d, caller=%r))"
            % (addr, length, port, caller)
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
                    self._dump_stream_cmd(addr, length, port, caller=self.caller))
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
    def _gdb_serve_cmd(port: int, reset_halt: bool, caller=None) -> str:
        """Build the on-pod gdb_serve invocation (pure, for testability)."""
        return (
            "import annealage_pod.debug.ops as o;"
            "print(o.gdb_serve(port=%d, reset_halt=%s, caller=%r))"
            % (port, bool(reset_halt), caller)
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
                    self._gdb_serve_cmd(gdb_port, reset_halt, caller=self.caller))
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
        target = device or self._resolver.ampremote_target(self.repl_port)

        # The setup chain runs against the SESSION'S target, so a DUT session
        # sets up the DUT. Routing it through self.exec()/self.cp() would send
        # it to the pod regardless of which target the session then connects to.
        def _setup(*argv, **kw):
            out = self._runner([_ampremote_exe(), "connect", target] + list(argv),
                               capture_output=True, text=True)
            if kw.get("check") and getattr(out, "returncode", 0) != 0:
                raise PodExecError(argv[0], out.returncode,
                                   getattr(out, "stdout", ""),
                                   getattr(out, "stderr", ""),
                                   caller=self.caller)
            return out

        if soft_reset:
            _setup("soft-reset")            # best-effort, own connection
        # cp and exec are checked: a setup step that failed silently would leave
        # the session connected to a target that is not in the state asked for.
        for src, dst in (pre_cp or []):
            _setup("fs", "cp", src, dst, check=True)
        for code in (pre_exec or []):
            _setup("exec", code, check=True)
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

    def reprobe_dut(self, force: bool = False) -> dict:
        """Recover a DUT the pod host is not exporting, without a cold power cycle.

        Runs usbhost.reprobe() on the pod. Two cases it covers:
          - mounted-but-unexportable: the DUT enumerated (tuh_mounted) but the
            USB/IP export slot was never populated because no rescan ran after it
            mounted (mount-after-initial-rescan / hot-plug-after-start). reprobe
            re-seeds the slot from the already-valid descriptor cache.
          - warm-reset connect-edge miss: the DUT re-presented D+ with no 0->1
            edge, so the host never enumerated it; reprobe synthesizes the attach
            to re-enumerate, then re-seeds.
        Non-destructive to a working forward held by THIS host. The synthesized
        re-enumeration in the second case would break one held by another host,
        so this refuses when another host holds the usbip import, naming it,
        unless force=True bumps it; a forced bump adds stole_from to the result.
        Returns {ok, mounted[, err, stole_from]} where mounted is the
        tuh_mounted address bitmask. ok=False if the pod firmware predates the
        reprobe verb.
        """
        stolen = self._gate_usbip(force)
        out = self.exec(
            "import usbhost\n"
            "if hasattr(usbhost, 'reprobe'):\n"
            "    usbhost.reprobe()\n"
            "    print({'ok': True, 'mounted': usbhost.mounted()})\n"
            "else:\n"
            "    print({'ok': False, 'err': 'pod firmware has no usbhost.reprobe'})\n"
        )
        result = _last_dict(out)
        if stolen:
            result["stole_from"] = stolen
        return result

    def usbip_attach(self, ensure: bool = True) -> dict:
        """Export the DUT over USB/IP and attach it on this host.

        With ensure=True, first brings the pod's USB host + usbip server up over
        the REPL (see pod.usbip.ensure_server - note the Wi-Fi risk). Lists the
        exported device for its live VID:PID + busid, attaches it (sudo), and
        returns {busid, vid, pid, tty}; tty is the DUT's CDC device, or None if
        it did not enumerate in time.

        If the pod exports nothing on the first list, reprobe_dut() is tried once
        (the DUT may be mounted-but-unexportable or a warm-reset edge-miss) and
        the list retried, so a consumer recovers without a manual step.
        """
        from pod import usbip as _u
        # Idempotent: the pod's usbip server allows one import per busid, so a
        # second attach while this host already holds one is refused with
        # "Request Failed". Reuse the attachment instead, which also avoids
        # needing the pod's REPL for ensure_server when there is nothing to do.
        held = self.attached_ports()
        if held:
            tty = _u.forwarded_tty(held)
            if tty:
                busid = next((p["busid"] for p in _u.ports()
                              if p["port"] in held), None)
                # Same keys as the attach path below, so a caller never has to
                # ask which route produced the result before reading it. Read
                # locally from sysfs: querying the pod would put a round trip on
                # the one route whose purpose is to touch nothing.
                vid, pid = _u.tty_usb_ids(tty)
                return {"busid": busid, "vid": vid, "pid": pid, "tty": tty,
                        "port": held[0], "already_attached": True}
        if ensure:
            _u.ensure_server(self)
        host = self._usbip_host()
        devs = _u.list_remote(host)
        if not devs:
            self.reprobe_dut()
            devs = _u.list_remote(host)
        if not devs:
            raise RuntimeError(
                "pod %s exports no USB device - is the DUT on the pod USB host "
                "port and enumerated? (reprobe did not recover it)" % host)
        dev = dict(devs[0])
        before = _u.serial_devices()
        _u.attach(host, dev["busid"])
        dev["tty"] = _u.wait_for_new_tty(before)
        return dev

    def attached_ports(self) -> list:
        """Host vhci ports currently attached to THIS pod.

        A port matches if its `usbip port` remote either

          - equals one of this pod's handles (hostname / addr4 / addr6 / seed /
            the resolver's cached connect address), each normalised to a
            canonical compressed IP or a lowercased, dot-stripped hostname
            (surrounding brackets and any %zone stripped) so a v4, ULA, global,
            or link-local attach matches however each side spelled it; or
          - is an IPv6 address whose EUI-64 interface id equals one of the pod's
            (see _iid6). This is the drift-proof match: if the pod's ULA prefix
            changed or it attached over a global/link-local not among the stored
            handles, the exact-string compare misses it but the interface id
            still identifies the same NIC. Without it detach silently no-ops and
            an SWD reset/flash runs under a live forward (the task-#1 hazard).

        The cached connect address is included because usbip_attach connects to
        resolver.resolve(), which on the mDNS-fallback tier returns a live-resolved
        address that need not be one of the static handles. Uses no network.
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
        handles = [r.hostname, r.addr4, r.cached] + list(r.addr6)
        if self._seed_address:
            handles.append(self._seed_address)
        mine = {_norm(a) for a in handles if a}
        my_iids = {i for i in (_iid6(a) for a in handles) if i is not None}
        matched = []
        for p in _u.ports():
            remote = p.get("remote")
            if _norm(remote) in mine or _iid6(remote) in my_iids:
                matched.append(p["port"])
        return matched

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
