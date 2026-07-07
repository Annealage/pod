"""Pod MCP server (stdio transport).

Exposes pod control as MCP tools so an agent can drive the
hardware iteration loop: discover -> flash_dut -> reset_dut ->
observe (dut_exec, mount_dir) -> repeat.

Tools:
  discover_pods   browse mDNS for live pods
  pod_info        show registry info for a named pod
  dut_exec        execute MicroPython code on a pod
  mount_dir       mount a local directory on a pod
  flash_dut       flash a DUT image (streamed into pod RAM, no pod FS)
  reset_dut       reset the DUT (sysreset to run, halt to catch the vector)
  read_dut        read DUT memory to a host file (streamed, no pod FS)
  gdb_dut         start a local GDB RSP server to the DUT and return its endpoint
  tail_uart       stream DUT UART output (tail, bounded duration) over TCP

The mcp import is guarded so this module can be imported and tested
even if the mcp package is absent. build_server() is only called
from main().
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from pod.discovery import discover_pods as _discover_pods
from pod.registry import (get_pod, load_registry, update_pod, reconcile_dut,
                          dut_protect_ranges)
from pod.client import Pod, PodExecError
from pod.target import PodUnreachable
from pod import enroll

# Optional mcp import
try:
    import mcp.server.stdio
    import mcp.server
    from mcp.server import Server
    from mcp.types import Tool, TextContent
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    Server = None
    Tool = None
    TextContent = None


# ── tool handler functions (pure logic, testable without mcp) ─────────────


def handle_discover_pods(timeout: float = 5.0) -> list:
    """Browse mDNS and return a list of pod info dicts."""
    pods = _discover_pods(timeout=timeout)
    return [p.to_dict() for p in pods]


def handle_pod_info(label: str) -> dict:
    """Return registry entry for a pod label, or raise KeyError if not found."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    return {"label": label, **entry}


def handle_register_pod(label: str, match: str = None,
                        timeout: float = 5.0) -> dict:
    """Discover a pod via mDNS and register its stable handles under `label`.

    The agent counterpart to `pod register` with no address: enrolls a
    freshly-flashed pod by name, storing hostname + IPv6 + IPv4 and reading the
    identity fingerprint. Overwrites an existing label.
    """
    entry = enroll.register_discovered(label, match=match, timeout=timeout,
                                       probe=True, force=True)
    return {"label": label, **entry}


def handle_dut(label: str, adopt: bool = False) -> dict:
    """Probe the live DUT identity over SWD and reconcile it with the declared block.

    Returns the reconcile_dut verdict (MATCH/MISMATCH/UNDECLARED/NO_DECLARED/
    NO_LIVE) with the per-field declared-vs-live ids. With adopt=True, snapshots
    the live ids into the declared expected{} block first.
    """
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod.from_entry(entry)
    try:
        live = pod.discover_dut()
    except Exception as exc:  # noqa: BLE001 - surfaced in the verdict
        live = {"ok": False, "err": repr(exc)}
    if adopt and live and live.get("ok", True):
        expected = {k: live[k] for k in ("dpidr", "ap_idr", "cpuid", "rom_base")
                    if k in live}
        if expected:
            dut = dict(entry.get("dut") or {})
            dut["expected"] = expected
            update_pod(label, dut=dut)
            entry = get_pod(label)
    return reconcile_dut(entry.get("dut"), live)


def handle_dut_usb(label: str) -> list:
    """List the DUT USB devices the pod exports over USB/IP (live VID:PID + busid)."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    return Pod.from_entry(entry).usbip_list()


def handle_attach_dut(label: str, ensure: bool = True) -> dict:
    """Attach the pod's DUT USB over USB/IP; returns {busid, vid, pid, tty}.

    Brings the pod USB host + usbip server up first (unless ensure=False) and
    attaches on this host (needs passwordless sudo for usbip). The returned tty
    is the DUT's own CDC REPL - connect to it with mpremote. NB: activating the
    pod USB host can disturb the pod's Wi-Fi link.
    """
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    return Pod.from_entry(entry).usbip_attach(ensure=ensure)


def handle_detach_dut(label: str) -> dict:
    """Detach every host vhci port currently attached to this pod's DUT."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    return Pod.from_entry(entry).usbip_detach()


def handle_ensure_dut_link(label: str) -> dict:
    """Bring the pod USB host + usbip server up (idempotent); list what it exports.

    Safe to call repeatedly. USB host + usbip are NOT auto-started on pod boot;
    this is the lazy bring-up that attach_dut also runs.
    """
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod.from_entry(entry)
    from pod import usbip as _u
    _u.ensure_server(pod)
    return {"label": label, "exported": pod.usbip_list()}


def handle_pod_exec(label: str, code: str) -> str:
    """Run MicroPython on the POD's own interpreter. Returns stdout."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    return Pod.from_entry(entry).exec(code)


def handle_dut_exec(label: str, code: str) -> dict:
    """Run MicroPython on the DUT (turnkey): ensure the USB/IP link, attach, and
    exec over the DUT's own CDC REPL. Returns {tty, returncode, stdout, stderr}."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    return Pod.from_entry(entry).dut_exec(code)


def handle_mount_dir(label: str, directory: str) -> str:
    """Mount a host directory on a pod. Returns status message."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod.from_entry(entry)
    pod.mount(directory)
    return f"Mounted {directory} on {label}."


def handle_flash_dut(label: str, image: str, target: str = None,
                     addr: int = 0, keep_attached: bool = False) -> dict:
    """Flash a firmware image to the DUT via the pod (streamed, no pod FS).

    Detaches a live USB/IP session first (reflashing the DUT mid-forward wedges
    the pod); keep_attached=True overrides.
    """
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod.from_entry(entry)
    return pod.flash_dut(image, target=target, addr=addr,
                         keep_attached=keep_attached)


def handle_reset_dut(label: str, mode: str = "sysreset",
                     keep_attached: bool = False) -> dict:
    """Reset the DUT via the pod ('sysreset' to run, 'halt' to catch reset).

    Also the first recovery step for an unresponsive/wedged DUT: a SWD system
    reset re-inits the core and peripherals (incl. USB), so a hung target
    re-enumerates cleanly without a physical power-cycle.
    """
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod.from_entry(entry)
    return pod.reset_dut(mode=mode, keep_attached=keep_attached)


def handle_read_dut(label: str, addr: int, length: int, out_path: str) -> str:
    """Read DUT memory to a host file via the pod (streamed, no pod FS)."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod.from_entry(entry)
    return pod.read_dut(addr, length, out_path)


# Running GDB sessions keyed by label, so an agent can start a session and
# spawn its own gdb against the returned endpoint. The host GdbServer runs in a
# background thread (a blocking RSP session does not fit a request/response
# tool call).
_GDB_SESSIONS: dict = {}


def handle_gdb_dut(label: str, listen_port: int = 0) -> dict:
    """Start an on-pod GDB server and a background host RSP translator.

    Non-interactive shape: returns {"endpoint": "127.0.0.1:<port>",
    "gdb_port": <pod_port>, "label": label} once the local listener is bound,
    so the agent spawns arm-none-eabi-gdb itself. The host RSP session runs in
    a background thread until gdb detaches.
    """
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    if label in _GDB_SESSIONS and _GDB_SESSIONS[label]["thread"].is_alive():
        sess = _GDB_SESSIONS[label]
        return {"endpoint": sess["endpoint"], "gdb_port": sess["gdb_port"],
                "label": label}

    pod = Pod.from_entry(entry)
    gdb_port = entry.get("gdb_port") or 3335
    ready = threading.Event()
    bound: dict = {}

    def _on_listen(host, port):
        bound["host"] = host
        bound["port"] = port
        ready.set()

    def _run():
        try:
            pod.gdb_endpoint(listen_port=listen_port, gdb_port=gdb_port,
                             on_listen=_on_listen)
        except Exception:  # noqa: BLE001 - background session, surfaced via state
            ready.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    if not ready.wait(timeout=30):
        raise RuntimeError("GDB server did not bind within 30s")
    if "port" not in bound:
        raise RuntimeError("GDB server failed to start")
    endpoint = f"{bound['host']}:{bound['port']}"
    _GDB_SESSIONS[label] = {
        "thread": thread, "endpoint": endpoint, "gdb_port": gdb_port}
    return {"endpoint": endpoint, "gdb_port": gdb_port, "label": label}


# ── DUT-facing peripherals (curated machine helpers) ──────────────────────


def _pod_for(label: str) -> Pod:
    """Resolve a registry label to a Pod client, or raise KeyError."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    return Pod.from_entry(entry)


# ── DUT register / memory over the SWD debug interface ────────────────────
# These reach the DUT only over the pod's SWD debug probe (the on-pod
# DP/AP/MEM-AP). They are unrelated to the USB/IP forward and the DUT's own CDC
# REPL; they need the DUT wired + powered for SWD. Registers require a halted
# core (dut_halt / reset_dut mode='halt').


def handle_dut_halt(label: str, keep_attached: bool = False) -> dict:
    """Halt the DUT core over SWD (no auto-resume). Freezes the DUT incl. USB;
    detaches a live USB/IP session first unless keep_attached."""
    return _pod_for(label).halt_dut(keep_attached=keep_attached)


def handle_dut_resume(label: str) -> dict:
    """Resume the DUT core over SWD after a dut_halt / reset_dut mode='halt'."""
    return _pod_for(label).resume_dut()


def handle_dut_read_reg(label: str, reg) -> dict:
    """Read one DUT core register over SWD (core must be halted first)."""
    return _pod_for(label).read_reg(reg)


def handle_dut_write_reg(label: str, reg, value: int) -> dict:
    """Write one DUT core register over SWD (core must be halted first)."""
    return _pod_for(label).write_reg(reg, value)


def handle_dut_read_mem(label: str, addr: int, length: int) -> dict:
    """Read DUT memory over SWD, returned inline as hex (<= 4096 bytes)."""
    return _pod_for(label).read_mem(addr, length)


def handle_dut_write_mem(label: str, addr: int, data_hex: str) -> dict:
    """Write DUT memory over SWD; refuses the declared flash + code region."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError("Pod '%s' not found in registry." % label)
    return Pod.from_entry(entry).write_mem(
        addr, data_hex, protect=dut_protect_ranges(entry))


# ── persistent streaming REPL sessions ────────────────────────────────────
# Long-lived connections held in this (long-running) server, keyed by label, so
# an agent opens a session, tails the target's stdout, injects REPL commands,
# and closes it across separate tool calls. The session streams to a log file
# (the lossless record) plus an in-memory tail the agent reads by cursor.
_REPL_SESSIONS: dict = {}


def _default_repl_log(label: str) -> str:
    return os.path.join(tempfile.gettempdir(), "pod-repl-%s.log" % label)


def handle_repl_open(label: str, log_path: str = None, device: str = None,
                     mount: str = None, exec: str = None, cp=None,
                     soft_reset: bool = False, unsafe_links: bool = False,
                     reconnect: bool = True) -> dict:
    """Open (or return the existing) persistent streaming REPL session.

    Streams the target's stdout to log_path and an in-memory tail buffer, and
    accepts injected stdin via repl_send. Default target is the pod's socket
    REPL; `device` points it at another mpremote device (e.g. a DUT tty).

    Chained setup before connecting (mirrors `mpremote <cmd>... repl`):
    soft_reset, then cp, then exec (each a one-shot verb), then mount kept on
    the session connection. `exec` is a string or list of code strings; `cp` is
    a [src, dst] pair or a list of pairs; `mount` is a host dir kept mounted for
    the session's lifetime (the reason to use repl_open over mount_dir, which is
    a one-shot that unmounts on return).
    """
    sess = _REPL_SESSIONS.get(label)
    if sess is not None and sess["session"].running:
        s = sess["session"]
        return {"label": label, "target": s.target, "log_path": sess["log_path"],
                "running": True, "mounted": s.mounted, "already_open": True,
                "note": "session already open; mount/exec/cp/soft_reset args "
                        "were ignored - repl_close first to change them"}
    pre_exec = [exec] if isinstance(exec, str) else (list(exec) if exec else None)
    pre_cp = None
    if cp:
        pre_cp = [tuple(cp)] if cp and not isinstance(cp[0], (list, tuple)) \
            else [tuple(p) for p in cp]
    pod = _pod_for(label)
    log_path = log_path or _default_repl_log(label)
    s = pod.open_session(log_path=log_path, device=device, mount=mount,
                         pre_exec=pre_exec, pre_cp=pre_cp, soft_reset=soft_reset,
                         unsafe_links=unsafe_links, reconnect=reconnect)
    _REPL_SESSIONS[label] = {"session": s, "log_path": log_path}
    return {"label": label, "target": s.target, "log_path": log_path,
            "running": s.running, "mounted": s.mounted}


def _require_repl(label: str):
    sess = _REPL_SESSIONS.get(label)
    if sess is None:
        raise KeyError(
            "No open REPL session for '%s' - call repl_open first." % label)
    return sess["session"]


def handle_repl_read(label: str, since: int = None) -> dict:
    """Tail the session's buffered stdout after `since` (cursor from a prior read)."""
    return _require_repl(label).read_since(since)


def handle_repl_send(label: str, data: str, newline: bool = True,
                     wait: float = 0.3) -> dict:
    """Inject a command into the target's stdin; return output captured in `wait`.

    Marks the stream cursor, writes `data` (a trailing newline submits a REPL
    line unless newline=False), waits `wait` seconds, and returns the output
    produced since - so a single call runs a command and reads its reply. Set
    wait=0 to send without reading (poll later with repl_read).
    """
    s = _require_repl(label)
    cursor = s.tell()
    try:
        sent = s.send(data, newline=newline)
    except ConnectionError as exc:
        return {"ok": False, "err": str(exc), "cursor": cursor}
    if wait and wait > 0:
        time.sleep(wait)
    out = s.read_since(cursor)
    out["sent"] = sent
    return out


def handle_repl_interrupt(label: str, wait: float = 0.3) -> dict:
    """Send Ctrl-C to the target and return output captured in `wait` seconds."""
    s = _require_repl(label)
    cursor = s.tell()
    try:
        s.interrupt()
    except ConnectionError as exc:
        return {"ok": False, "err": str(exc), "cursor": cursor}
    if wait and wait > 0:
        time.sleep(wait)
    return s.read_since(cursor)


def handle_repl_close(label: str) -> dict:
    """Close the session (the target keeps running) and drop it from the registry."""
    sess = _REPL_SESSIONS.pop(label, None)
    if sess is None:
        return {"ok": True, "note": "no open session"}
    result = sess["session"].close()
    result["label"] = label
    return result


def handle_repl_list() -> list:
    """List open REPL sessions."""
    return [{"label": label, "target": s["session"].target,
             "log_path": s["log_path"], "running": s["session"].running}
            for label, s in _REPL_SESSIONS.items()]


def handle_i2c_target(label: str, addr: int = 0x42, regs=None, bus: int = 1,
                      scl: int = 11, sda: int = 10, size: int = 256,
                      name: str = "i2c_target") -> dict:
    """Bring up a persistent hardware I2C target (register file) on the pod."""
    return _pod_for(label).i2c_target(addr=addr, regs=regs, bus=bus, scl=scl,
                                      sda=sda, size=size, name=name)


def handle_i2c_target_regs(label: str, off: int = 0, length=None, write=None,
                           name: str = "i2c_target") -> dict:
    """Read or write the pod I2C target's register file from the host."""
    return _pod_for(label).i2c_target_regs(off=off, length=length, write=write,
                                           name=name)


def handle_peripheral_release(label: str, name: str = "*") -> dict:
    """Release one named pod peripheral instance, or all with '*'."""
    return _pod_for(label).peripheral_release(name=name)


def handle_gpio(label: str, pin: int, value=None, mode: str = "out",
                pull=None) -> dict:
    """Read (value=None) or drive a pod GPIO."""
    return _pod_for(label).gpio(pin, value=value, mode=mode, pull=pull)


def handle_adc(label: str, pin: int) -> dict:
    """Sample a pod ADC channel (raw u16 + 3.3V-ref volts)."""
    return _pod_for(label).adc(pin)


def handle_logic_analyse(label: str, base_pin: int, width: int = 1,
                         rate: int = 1000000, depth: int = 8000, trigger=None,
                         out_path: str = "capture.vcd", sm_id: int = 0,
                         names=None) -> dict:
    """Capture DUT pins with the pod logic analyser and write a VCD file."""
    trig = tuple(trigger) if trigger else None
    return _pod_for(label).logic_analyse(
        base_pin=base_pin, width=width, rate=rate, depth=depth, trigger=trig,
        out_path=out_path, sm_id=sm_id, names=names)


def handle_tail_uart(label: str, port: int = None, duration: float = 30.0) -> dict:
    """Stream DUT UART output (tail) over the pod's TCP UART bridge.

    Bounded by duration (default 30s) so an agent cannot hold an open infinite
    stream. Read-only: the TX direction is CLI-only. Connects to the pod's
    always-bound UART listener on the advertised uart_port, or port if given.
    """
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    effective_port = port or (entry.get("uart_port") or 2000)
    return Pod.from_entry(entry).uart_stream(port=effective_port, duration=duration)


# ── MCP server construction ───────────────────────────────────────────────


def build_server():
    """Construct and return the MCP Server instance.

    Only call this if _MCP_AVAILABLE is True.
    """
    if not _MCP_AVAILABLE:
        raise RuntimeError("mcp package is not installed.")

    server = Server("annealage-pod")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name="discover_pods",
                description="Browse the local network for Annealage Pods via mDNS.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "timeout": {
                            "type": "number",
                            "description": "Browse duration in seconds.",
                            "default": 5.0,
                        }
                    },
                },
            ),
            Tool(
                name="pod_info",
                description=(
                    "Return registry info for a named pod: its stable handles "
                    "(hostname, addr6 IPv6 list, addr4), identity fingerprint, "
                    "ports, and declared DUT block."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."}
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="register_pod",
                description=(
                    "Discover a pod via mDNS and register it under a label, "
                    "storing its stable handles (hostname + IPv6 + IPv4) and "
                    "reading its identity fingerprint. Use to enroll a "
                    "freshly-flashed pod by name without a hand-copied address. "
                    "Overwrites an existing label."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Label to register under."},
                        "match": {"type": "string", "description": "Disambiguate the mDNS match (hostname/instance); default uses the label."},
                        "timeout": {"type": "number", "description": "mDNS browse duration in seconds.", "default": 5.0},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="dut",
                description=(
                    "Probe the connected DUT's identity over SWD (dpidr, ap_idr, "
                    "cpuid, rom_base) and reconcile it against the pod's declared "
                    "DUT block. Returns a verdict (MATCH / MISMATCH / UNDECLARED / "
                    "NO_DECLARED / NO_LIVE). adopt=true snapshots the live ids into "
                    "the declared expected{} block."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "adopt": {"type": "boolean", "description": "Snapshot live ids into the declared expected block.", "default": False},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="dut_usb",
                description=(
                    "List the DUT USB devices the pod exports over USB/IP - the "
                    "live VID:PID and busid. Read-only; the pod's usbip server "
                    "must already be running (use attach_dut to bring it up)."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="attach_dut",
                description=(
                    "Attach the pod's DUT USB over USB/IP to THIS host and return "
                    "{busid, vid, pid, tty}. tty is the DUT's own CDC REPL - "
                    "connect with mpremote. Brings the pod USB host + usbip "
                    "server up first unless ensure=false. Needs passwordless sudo "
                    "for usbip on the host. NB: activating the pod USB host can "
                    "disturb the pod's Wi-Fi link."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "ensure": {"type": "boolean", "description": "Start the pod USB host + usbip server first.", "default": True},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="pod_exec",
                description=(
                    "Run a MicroPython code string on the POD's own interpreter "
                    "and return stdout. This is pod-side (the pod's debug stack / "
                    "peripherals), NOT the DUT. To run code on the DUT, use "
                    "attach_dut and connect the returned tty (the DUT's own REPL)."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "code": {"type": "string", "description": "MicroPython code to run on the pod."},
                    },
                    "required": ["label", "code"],
                },
            ),
            Tool(
                name="dut_exec",
                description=(
                    "Run MicroPython on the DUT (turnkey): ensure the pod USB/IP "
                    "link, attach the DUT, and exec the code over its own CDC "
                    "REPL. Returns {tty, returncode, stdout, stderr}. For pod-side "
                    "code use pod_exec instead. Relies on the USB/IP-forwarded DUT "
                    "REPL, which is not yet reliable on RP2350 (intermittent), so "
                    "this may fail to produce a tty."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "code": {"type": "string", "description": "MicroPython code to run ON THE DUT."},
                    },
                    "required": ["label", "code"],
                },
            ),
            Tool(
                name="detach_dut",
                description=(
                    "Detach every host vhci port currently attached to this pod's "
                    "DUT (the inverse of attach_dut). Detach before reset/reflash "
                    "of the DUT to avoid wedging the forwarder."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="ensure_dut_link",
                description=(
                    "Bring the pod USB host + usbip server up (idempotent) and "
                    "return what it exports. USB host + usbip are NOT auto-started "
                    "on pod boot; call this (or attach_dut) first. Safe to repeat."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="mount_dir",
                description="Mount a local host directory on the pod over ampremote.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "directory": {"type": "string", "description": "Local directory path."},
                    },
                    "required": ["label", "directory"],
                },
            ),
            Tool(
                name="flash_dut",
                description=(
                    "Flash a firmware image to the DUT over SWD via the pod, "
                    "streamed into pod RAM (no pod filesystem)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "image": {"type": "string", "description": "Firmware image path (raw binary)."},
                        "addr": {
                            "type": "integer",
                            "description": "Flash base address.",
                            "default": 0,
                        },
                        "target": {
                            "type": "string",
                            "description": "Target MCU identifier (optional).",
                        },
                        "keep_attached": {
                            "type": "boolean",
                            "description": "Do not detach a live USB/IP session first (risks a forwarder wedge).",
                            "default": False,
                        },
                    },
                    "required": ["label", "image"],
                },
            ),
            Tool(
                name="reset_dut",
                description=(
                    "Reset the DUT via the on-pod debug probe (SWD SYSRESETREQ). "
                    "FIRST thing to try when the DUT is unresponsive or suspected "
                    "wedged (hung firmware, a soft-reset that left its USB/serial "
                    "hung, a stuck peripheral): a system reset re-inits the core "
                    "AND peripherals (incl. USB), so a target whose USB-CDC/REPL "
                    "wedged re-enumerates cleanly - no physical replug/power-cycle "
                    "needed. Use mode 'sysreset' to reset and run, 'halt' to reset "
                    "and catch the reset vector for debugging. Only fall back to a "
                    "physical power-cycle if the reset itself reports an error "
                    "(e.g. SWD not connected)."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "mode": {
                            "type": "string",
                            "enum": ["sysreset", "halt"],
                            "description": "sysreset = reset and run; halt = reset and halt.",
                            "default": "sysreset",
                        },
                        "keep_attached": {
                            "type": "boolean",
                            "description": "Do not detach a live USB/IP session first (risks a forwarder wedge).",
                            "default": False,
                        },
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="read_dut",
                description=(
                    "Read DUT memory to a host file via the pod, streamed from "
                    "pod RAM (no pod filesystem). The only path that returns "
                    "target contents."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "addr": {"type": "integer", "description": "Source address."},
                        "length": {"type": "integer", "description": "Bytes to read."},
                        "out_path": {"type": "string", "description": "Host file to write."},
                    },
                    "required": ["label", "addr", "length", "out_path"],
                },
            ),
            Tool(
                name="gdb_dut",
                description=(
                    "Start an on-pod GDB server and a local RSP translator, then "
                    "return its endpoint. Spawn arm-none-eabi-gdb yourself with "
                    "'target extended-remote <endpoint>'. The host session runs "
                    "in the background until gdb detaches."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "listen_port": {
                            "type": "integer",
                            "description": "Local gdb-facing port (0 = ephemeral).",
                            "default": 0,
                        },
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="dut_halt",
                description=(
                    "Halt the DUT core over the pod's SWD debug interface (the "
                    "on-pod probe) and hold it - no auto-resume. REQUIRED before "
                    "dut_read_reg/dut_write_reg (registers need a halted core). "
                    "Freezes the target where it is, including its USB, so any "
                    "active USB/IP forward stalls until dut_resume. SWD only: "
                    "needs the DUT wired + powered for SWD; unrelated to the "
                    "USB/IP forward and the DUT's CDC REPL. Returns {ok, halted, "
                    "dhcsr}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "keep_attached": {
                            "type": "boolean",
                            "description": "Do not detach a live USB/IP session "
                                           "first (risks a forwarder wedge).",
                            "default": False,
                        },
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="dut_resume",
                description=(
                    "Resume the DUT core over SWD after a dut_halt (or reset_dut "
                    "mode='halt'). SWD debug interface only. Returns "
                    "{ok, halted:false}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="dut_read_reg",
                description=(
                    "Read one DUT core register over the pod's SWD debug "
                    "interface. The core MUST be halted first (dut_halt, or "
                    "reset_dut mode='halt') - registers are read through the "
                    "debug DCRSR/DCRDR, which require a halted core; a running "
                    "core returns {ok:false}. reg is a number 0..18 or a name: "
                    "r0..r12, sp(13), lr(14), pc(15), xpsr(16), msp(17), "
                    "psp(18). SWD only; not the USB/IP or CDC path. Returns "
                    "{ok, regsel, value}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "reg": {
                            "type": ["integer", "string"],
                            "description": "regsel 0..18 or name (r0..r12, sp, "
                                           "lr, pc, xpsr, msp, psp).",
                        },
                    },
                    "required": ["label", "reg"],
                },
            ),
            Tool(
                name="dut_write_reg",
                description=(
                    "Write one DUT core register over the pod's SWD debug "
                    "interface. The core MUST be halted first (dut_halt, or "
                    "reset_dut mode='halt'); a running core returns {ok:false}. "
                    "reg is a number 0..18 or a name (r0..r12, sp, lr, pc, "
                    "xpsr, msp, psp). SWD only. Returns {ok, regsel, value}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "reg": {
                            "type": ["integer", "string"],
                            "description": "regsel 0..18 or name (r0..r12, sp, "
                                           "lr, pc, xpsr, msp, psp).",
                        },
                        "value": {
                            "type": "integer",
                            "description": "32-bit value to write.",
                        },
                    },
                    "required": ["label", "reg", "value"],
                },
            ),
            Tool(
                name="dut_read_mem",
                description=(
                    "Read DUT memory over the pod's SWD debug interface and "
                    "return it inline as hex. Small reads only (<= 4096 bytes); "
                    "for bulk dumps to a host file use read_dut. A live MEM-AP "
                    "read - works whether the core runs or is halted (a read of "
                    "a location the running core is changing may be "
                    "non-coherent; dut_halt first for a coherent snapshot). SWD "
                    "only; needs the DUT wired for SWD. Returns {ok, addr, "
                    "length, hex}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "addr": {"type": "integer", "description": "Source address."},
                        "length": {
                            "type": "integer",
                            "description": "Bytes to read (1..4096).",
                        },
                    },
                    "required": ["label", "addr", "length"],
                },
            ),
            Tool(
                name="dut_write_mem",
                description=(
                    "Write DUT memory over the pod's SWD debug interface "
                    "(RAM/peripherals only). data_hex is a hex string "
                    "(<= 4096 bytes). Writes into the flash region "
                    "(addr < 0x20000000) are REFUSED - flash needs erase, use "
                    "flash_dut. A live MEM-AP write. SWD only. Returns "
                    "{ok, addr, length}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "addr": {"type": "integer", "description": "Destination address."},
                        "data_hex": {
                            "type": "string",
                            "description": "Bytes to write as a hex string "
                                           "(e.g. 'deadbeef').",
                        },
                    },
                    "required": ["label", "addr", "data_hex"],
                },
            ),
            Tool(
                name="repl_open",
                description=(
                    "Open a persistent streaming REPL session to the pod (built "
                    "on ampremote). Streams the target's stdout to a log file "
                    "AND an in-memory tail buffer, and lets you inject commands "
                    "to its stdin with repl_send - the 'connect once, watch "
                    "output, run REPL commands' loop. Default target is the "
                    "pod's own socket REPL (where its asyncio app + aiorepl "
                    "live); pass `device` to attach another mpremote device "
                    "(e.g. a DUT CDC tty). Chain setup before connecting "
                    "(mpremote-style): `soft_reset`, `cp`, `exec`, then `mount` "
                    "(kept on the session connection - this is the reason to use "
                    "repl_open over mount_dir, which is one-shot and unmounts on "
                    "return). Holds the pod's single REPL slot until repl_close, "
                    "so use repl_send (not pod_exec) to run code while open. "
                    "Returns {label, target, log_path, running, mounted}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "log_path": {
                            "type": "string",
                            "description": "File to append all output to "
                                           "(default: a temp file, returned).",
                        },
                        "device": {
                            "type": "string",
                            "description": "mpremote device to attach instead of "
                                           "the pod's socket REPL (e.g. a DUT tty).",
                        },
                        "mount": {
                            "type": "string",
                            "description": "Host dir to mount on the target for "
                                           "the session lifetime.",
                        },
                        "exec": {
                            "type": ["string", "array"],
                            "items": {"type": "string"},
                            "description": "Setup code to run before connecting "
                                           "(string or list of strings).",
                        },
                        "cp": {
                            "type": "array",
                            "description": "File(s) to copy before connecting: "
                                           "[src, dst] or a list of such pairs "
                                           "(':path' = target side).",
                        },
                        "soft_reset": {
                            "type": "boolean",
                            "description": "Soft-reset the target before connecting.",
                            "default": False,
                        },
                        "unsafe_links": {
                            "type": "boolean",
                            "description": "With mount, follow symlinks outside the root.",
                            "default": False,
                        },
                        "reconnect": {
                            "type": "boolean",
                            "description": "Auto-reconnect across drops / target "
                                           "reboots (marked inline in the stream).",
                            "default": True,
                        },
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="repl_read",
                description=(
                    "Tail an open REPL session's buffered stdout. Pass the "
                    "`cursor` returned by a prior read to get only new output; "
                    "omit it to read from the oldest buffered byte. Returns "
                    "{text, cursor, dropped} - `dropped` counts bytes evicted "
                    "from the in-memory buffer before `since` (they remain in "
                    "the log file). The complete record is always in log_path."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "since": {
                            "type": "integer",
                            "description": "Cursor from a prior repl_read.",
                        },
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="repl_send",
                description=(
                    "Inject a command into an open REPL session's stdin and "
                    "return the output it produced. A trailing newline is added "
                    "(submitting a REPL line) unless newline=false. Waits `wait` "
                    "seconds then returns {text, cursor, dropped, sent} captured "
                    "since the send; set wait=0 to fire-and-forget and poll with "
                    "repl_read."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "data": {
                            "type": "string",
                            "description": "Text to send (a REPL command line).",
                        },
                        "newline": {
                            "type": "boolean",
                            "description": "Append CR-LF to submit the line.",
                            "default": True,
                        },
                        "wait": {
                            "type": "number",
                            "description": "Seconds to wait before reading the "
                                           "reply (0 = don't read).",
                            "default": 0.3,
                        },
                    },
                    "required": ["label", "data"],
                },
            ),
            Tool(
                name="repl_interrupt",
                description=(
                    "Send Ctrl-C to an open REPL session (interrupt a running "
                    "REPL command or loop) and return any output produced in "
                    "`wait` seconds."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "wait": {
                            "type": "number",
                            "description": "Seconds to wait before reading.",
                            "default": 0.3,
                        },
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="repl_close",
                description=(
                    "Close an open REPL session (the target keeps running) "
                    "and free the pod's REPL slot. Returns {ok, received, label}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="repl_list",
                description="List the open REPL sessions (label, target, log_path, running).",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="i2c_target",
                description=(
                    "Bring up a persistent hardware I2C target on the pod: a "
                    "register file the DUT controller reads/writes (e.g. "
                    "readfrom_mem). Persists until released."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "addr": {"type": "integer", "description": "7-bit I2C address.", "default": 66},
                        "regs": {"type": "array", "items": {"type": "integer"}, "description": "Initial register bytes from offset 0."},
                        "bus": {"type": "integer", "description": "Hardware I2C bus id.", "default": 1},
                        "scl": {"type": "integer", "description": "SCL GPIO.", "default": 11},
                        "sda": {"type": "integer", "description": "SDA GPIO.", "default": 10},
                        "size": {"type": "integer", "description": "Register file size in bytes.", "default": 256},
                        "name": {"type": "string", "description": "Instance name.", "default": "i2c_target"},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="i2c_target_regs",
                description="Read or write the pod I2C target's register file from the host.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "off": {"type": "integer", "description": "Register offset.", "default": 0},
                        "length": {"type": "integer", "description": "Bytes to read (default: to end)."},
                        "write": {"type": "array", "items": {"type": "integer"}, "description": "Bytes to write at off first."},
                        "name": {"type": "string", "description": "Instance name.", "default": "i2c_target"},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="peripheral_release",
                description="Release one named pod peripheral instance, or all with '*'.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "name": {"type": "string", "description": "Instance name or '*' for all.", "default": "*"},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="gpio",
                description="Read (omit value) or drive a pod GPIO; returns the resulting level.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "pin": {"type": "integer", "description": "GPIO number."},
                        "value": {"type": "integer", "description": "0/1 to drive; omit to read."},
                        "mode": {"type": "string", "description": "Pin mode when driving.", "default": "out"},
                        "pull": {"type": "string", "enum": ["up", "down"], "description": "Input pull (read only)."},
                    },
                    "required": ["label", "pin"],
                },
            ),
            Tool(
                name="adc",
                description="Sample a pod ADC channel; returns raw u16 and a 3.3V-ref voltage.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "pin": {"type": "integer", "description": "ADC-capable GPIO number."},
                    },
                    "required": ["label", "pin"],
                },
            ),
            Tool(
                name="logic_analyse",
                description=(
                    "Capture DUT pins with the pod's PIO logic analyser (PIO0) and "
                    "write a VCD file. Runs concurrently with SWD (PIO1) and Wi-Fi "
                    "(PIO2) on separate PIO blocks; it does not swap SWD out. "
                    "Fixed-depth timed snapshot, not an edge-counter or free-run "
                    "capture: the sample buffer caps at ~80 KB (20000 samples at "
                    "32-bit width, up to ~640000 at 1-bit), so the window is short "
                    "(sub-second at useful rates)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "base_pin": {"type": "integer", "description": "Lowest GPIO sampled."},
                        "width": {"type": "integer", "description": "Contiguous pins to sample (1..32).", "default": 1},
                        "rate": {"type": "integer", "description": "Sample rate in Hz.", "default": 1000000},
                        "depth": {"type": "integer", "description": "Samples to capture.", "default": 8000},
                        "trigger": {"type": "array", "items": {}, "description": "[pin, cond], cond in rise/fall/high/low; omit for immediate."},
                        "out_path": {"type": "string", "description": "Output VCD path.", "default": "capture.vcd"},
                        "sm_id": {"type": "integer", "description": "PIO0 state machine id (PIO2 is CYW43 Wi-Fi).", "default": 0},
                        "names": {"type": "array", "items": {"type": "string"}, "description": "Channel names, low pin first."},
                    },
                    "required": ["label", "base_pin"],
                },
            ),
            Tool(
                name="tail_uart",
                description=(
                    "Stream DUT UART output (tail) over the pod's TCP UART bridge. "
                    "Connects to the pod's always-bound UART listener and returns "
                    "bytes received within the duration window. Read-only: the TX "
                    "direction is available from the CLI only."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "port": {"type": "integer",
                                 "description": "Pod UART TCP port (default: registry uart_port or 2000)."},
                        "duration": {"type": "number",
                                     "description": "Seconds to capture (default: 30).",
                                     "default": 30.0},
                    },
                    "required": ["label"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        # The handlers are blocking (zeroconf browse, ampremote subprocess,
        # TCP streaming), so run them in a worker thread rather than on the
        # event loop. Running the sync zeroconf browse on the loop returns no
        # results (it needs its own thread to collect responses).
        try:
            if name == "discover_pods":
                result = await asyncio.to_thread(
                    handle_discover_pods, arguments.get("timeout", 5.0))
            elif name == "pod_info":
                result = await asyncio.to_thread(
                    handle_pod_info, arguments["label"])
            elif name == "register_pod":
                result = await asyncio.to_thread(
                    handle_register_pod, arguments["label"],
                    arguments.get("match"), arguments.get("timeout", 5.0))
            elif name == "dut":
                result = await asyncio.to_thread(
                    handle_dut, arguments["label"], arguments.get("adopt", False))
            elif name == "dut_usb":
                result = await asyncio.to_thread(
                    handle_dut_usb, arguments["label"])
            elif name == "attach_dut":
                result = await asyncio.to_thread(
                    handle_attach_dut, arguments["label"],
                    arguments.get("ensure", True))
            elif name == "detach_dut":
                result = await asyncio.to_thread(
                    handle_detach_dut, arguments["label"])
            elif name == "ensure_dut_link":
                result = await asyncio.to_thread(
                    handle_ensure_dut_link, arguments["label"])
            elif name == "pod_exec":
                result = await asyncio.to_thread(
                    handle_pod_exec, arguments["label"], arguments["code"])
            elif name == "dut_exec":
                result = await asyncio.to_thread(
                    handle_dut_exec, arguments["label"], arguments["code"])
            elif name == "mount_dir":
                result = await asyncio.to_thread(
                    handle_mount_dir, arguments["label"], arguments["directory"])
            elif name == "flash_dut":
                result = await asyncio.to_thread(
                    handle_flash_dut, arguments["label"], arguments["image"],
                    arguments.get("target"), arguments.get("addr", 0),
                    arguments.get("keep_attached", False))
            elif name == "reset_dut":
                result = await asyncio.to_thread(
                    handle_reset_dut, arguments["label"],
                    arguments.get("mode", "sysreset"),
                    arguments.get("keep_attached", False))
            elif name == "read_dut":
                result = await asyncio.to_thread(
                    handle_read_dut, arguments["label"], arguments["addr"],
                    arguments["length"], arguments["out_path"])
            elif name == "gdb_dut":
                result = await asyncio.to_thread(
                    handle_gdb_dut, arguments["label"],
                    arguments.get("listen_port", 0))
            elif name == "dut_halt":
                result = await asyncio.to_thread(
                    handle_dut_halt, arguments["label"],
                    arguments.get("keep_attached", False))
            elif name == "dut_resume":
                result = await asyncio.to_thread(
                    handle_dut_resume, arguments["label"])
            elif name == "dut_read_reg":
                result = await asyncio.to_thread(
                    handle_dut_read_reg, arguments["label"], arguments["reg"])
            elif name == "dut_write_reg":
                result = await asyncio.to_thread(
                    handle_dut_write_reg, arguments["label"], arguments["reg"],
                    arguments["value"])
            elif name == "dut_read_mem":
                result = await asyncio.to_thread(
                    handle_dut_read_mem, arguments["label"], arguments["addr"],
                    arguments["length"])
            elif name == "dut_write_mem":
                result = await asyncio.to_thread(
                    handle_dut_write_mem, arguments["label"], arguments["addr"],
                    arguments["data_hex"])
            elif name == "repl_open":
                result = await asyncio.to_thread(
                    handle_repl_open, arguments["label"],
                    arguments.get("log_path"), arguments.get("device"),
                    arguments.get("mount"), arguments.get("exec"),
                    arguments.get("cp"), arguments.get("soft_reset", False),
                    arguments.get("unsafe_links", False),
                    arguments.get("reconnect", True))
            elif name == "repl_read":
                result = await asyncio.to_thread(
                    handle_repl_read, arguments["label"],
                    arguments.get("since"))
            elif name == "repl_send":
                result = await asyncio.to_thread(
                    handle_repl_send, arguments["label"], arguments["data"],
                    arguments.get("newline", True), arguments.get("wait", 0.3))
            elif name == "repl_interrupt":
                result = await asyncio.to_thread(
                    handle_repl_interrupt, arguments["label"],
                    arguments.get("wait", 0.3))
            elif name == "repl_close":
                result = await asyncio.to_thread(
                    handle_repl_close, arguments["label"])
            elif name == "repl_list":
                result = await asyncio.to_thread(handle_repl_list)
            elif name == "i2c_target":
                result = await asyncio.to_thread(
                    handle_i2c_target, arguments["label"],
                    arguments.get("addr", 0x42), arguments.get("regs"),
                    arguments.get("bus", 1), arguments.get("scl", 11),
                    arguments.get("sda", 10), arguments.get("size", 256),
                    arguments.get("name", "i2c_target"))
            elif name == "i2c_target_regs":
                result = await asyncio.to_thread(
                    handle_i2c_target_regs, arguments["label"],
                    arguments.get("off", 0), arguments.get("length"),
                    arguments.get("write"), arguments.get("name", "i2c_target"))
            elif name == "peripheral_release":
                result = await asyncio.to_thread(
                    handle_peripheral_release, arguments["label"],
                    arguments.get("name", "*"))
            elif name == "gpio":
                result = await asyncio.to_thread(
                    handle_gpio, arguments["label"], arguments["pin"],
                    arguments.get("value"), arguments.get("mode", "out"),
                    arguments.get("pull"))
            elif name == "adc":
                result = await asyncio.to_thread(
                    handle_adc, arguments["label"], arguments["pin"])
            elif name == "logic_analyse":
                result = await asyncio.to_thread(
                    handle_logic_analyse, arguments["label"], arguments["base_pin"],
                    arguments.get("width", 1), arguments.get("rate", 1000000),
                    arguments.get("depth", 8000), arguments.get("trigger"),
                    arguments.get("out_path", "capture.vcd"),
                    arguments.get("sm_id", 0), arguments.get("names"))
            elif name == "tail_uart":
                result = await asyncio.to_thread(
                    handle_tail_uart, arguments["label"],
                    arguments.get("port"), arguments.get("duration", 30.0))
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]
            return [TextContent(type="text", text=str(result))]

        except NotImplementedError as exc:
            return [TextContent(type="text", text=f"Not implemented: {exc}")]
        except PodExecError as exc:
            # Classified pod-exec failure: surface the reason + the ampremote
            # stderr so the agent sees why, not a bare non-zero exit.
            text = str(exc)
            if exc.stderr:
                text += "\n--- pod stderr ---\n" + exc.stderr
            return [TextContent(type="text", text=text)]
        except PodUnreachable as exc:
            # No tier yielded an identity-confirmed target - unreachable, or a
            # DHCP-moved IPv4 whose fingerprint did not match. Distinct from a
            # generic failure so the agent does not retry blindly.
            return [TextContent(
                type="text",
                text=f"Pod unreachable or identity mismatch: {exc}")]
        except (LookupError, KeyError) as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]
        except ValueError as exc:
            # Bad argument surfaced locally (e.g. an out-of-range regsel/length)
            # before any pod round-trip.
            return [TextContent(type="text", text=f"Invalid argument: {exc}")]

    return server


def main():
    if not _MCP_AVAILABLE:
        print("mcp package is not installed. Install with: pip install mcp", file=sys.stderr)
        sys.exit(1)

    import asyncio

    server = build_server()

    async def _run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
