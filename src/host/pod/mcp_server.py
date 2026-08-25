"""Pod MCP server (stdio transport).

Exposes pod control as MCP tools so an agent can drive the hardware
iteration loop: discover -> dut_flash -> dut_reset -> observe (dut_exec,
dut_open) -> repeat.

27 tools across three namespaces:
  pod_    the pod as a managed device: pod_discover, pod_register, pod_info,
          pod_exec, pod_mount, pod_open
  dut_    the device under test, reached by any route: dut_open,
          session_send, session_read, session_close, dut_exec (its REPL);
          dut_identify, dut_halt, dut_resume, dut_reg, dut_mem, dut_gdb (its
          SWD debug port); dut_flash, dut_erase, dut_reset (its flash);
          dut_link (its USB/IP link)
  bench_  the pod's instruments pointed at the DUT: bench_gpio, bench_adc,
          bench_la, bench_device, bench_device_regs, bench_uart

The mcp import is guarded so this module can be imported and tested even if
the mcp package is absent. build_server() is only called from main().
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from pod.discovery import discover_pods as _discover_pods
from pod.registry import (get_pod, update_pod, reconcile_dut,
                          dut_protect_ranges)
from pod.client import Pod, PodExecError, DEFAULT_SWD_CLKDIV
from pod.target import PodUnreachable
from pod import enroll

import mcp.server.stdio
import mcp.server
from mcp.server import Server
from mcp.types import Tool, TextContent


# ── tool handler functions (pure logic, no mcp types in their signatures) ──


def _entry_for(label: str) -> dict:
    """Resolve a registry label to its registry entry, or raise KeyError."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    return entry


def _pod_for(label: str) -> Pod:
    """Resolve a registry label to a Pod client, or raise KeyError."""
    return Pod.from_entry(_entry_for(label))


# ── pod: the managed device itself ─────────────────────────────────────────


def handle_pod_discover(timeout: float = 5.0) -> list:
    """Browse mDNS and return a list of pod info dicts."""
    pods = _discover_pods(timeout=timeout)
    return [p.to_dict() for p in pods]


def handle_pod_register(label: str, match: str = None,
                        timeout: float = 5.0) -> dict:
    """Discover a pod via mDNS and register its stable handles under `label`.

    The agent counterpart to `pod register` with no address: enrolls a
    freshly-flashed pod by name, storing hostname + IPv6 + IPv4 and reading the
    identity fingerprint. Overwrites an existing label.
    """
    entry = enroll.register_discovered(label, match=match, timeout=timeout,
                                       probe=True, force=True)
    return {"label": label, **entry}


def _open_sessions() -> list:
    """List open sessions in this process (label, target, log_path, running)."""
    return [{"label": label, "target": s["session"].target,
             "log_path": s["log_path"], "running": s["session"].running}
            for label, s in _REPL_SESSIONS.items()]


def handle_pod_info(label: str) -> dict:
    """Return registry info for a pod label plus this process's open sessions.

    Raises KeyError if the label is not registered. `sessions` lists every
    session open in this process, not only sessions opened against `label` -
    a session can be held against any pod this process has touched.
    """
    entry = _entry_for(label)
    return {"label": label, **entry, "sessions": _open_sessions()}


def handle_pod_exec(label: str, code: str) -> str:
    """Run MicroPython on the POD's own interpreter. Returns stdout."""
    return _pod_for(label).exec(code)


def handle_pod_mount(label: str, directory: str) -> str:
    """Mount a host directory on a pod. Returns status message."""
    entry = _entry_for(label)
    pod = Pod.from_entry(entry)
    pod.mount(directory)
    return f"Mounted {directory} on {label}."


# ── dut: the device under test, over its own CDC REPL ─────────────────────
# _REPL_SESSIONS holds every persistent streaming session open in this
# (long-running) process, keyed by pod label, so an agent opens a session,
# tails the target's stdout, injects REPL commands, and closes it across
# separate tool calls. The session streams to a log file (the lossless
# record) plus an in-memory tail the agent reads by cursor. pod_open and
# dut_open share this store: only one session can be held per label at a
# time, whichever target (pod socket REPL or DUT tty) it was opened
# against - each entry records its `device` (None for a pod_open session,
# the tty string for a dut_open one) so a request naming a different
# target is refused rather than silently handed the wrong session.
_REPL_SESSIONS: dict = {}


def _default_repl_log(label: str) -> str:
    return os.path.join(tempfile.gettempdir(), "pod-repl-%s.log" % label)


def _open_session(label: str, log_path: str = None, device: str = None,
                     mount: str = None, exec: str = None, cp=None,
                     soft_reset: bool = False, unsafe_links: bool = False,
                     reconnect: bool = True) -> dict:
    """Open (or return the existing) persistent streaming REPL session.

    The shared core behind pod_open (device omitted, targets the pod's
    socket REPL) and dut_open (device given, targets a DUT tty). Streams the
    target's stdout to log_path and an in-memory tail buffer, and accepts
    injected stdin via session_send.

    Chained setup before connecting (mirrors `mpremote <cmd>... repl`):
    soft_reset, then cp, then exec (each a one-shot verb), then mount kept on
    the session connection. `exec` is a string or list of code strings; `cp`
    is a [src, dst] pair or a list of pairs; `mount` is a host dir kept
    mounted for the session's lifetime (the reason to use pod_open/dut_open
    over pod_mount, which is a one-shot that unmounts on return).

    Refuses if `label` already holds a running session opened against a
    different target (a pod_open session when `device` is given, a
    dut_open session on a different device, or a dut_open session when
    `device` is omitted) - it never returns a session for the wrong
    subject.
    """
    sess = _REPL_SESSIONS.get(label)
    if sess is not None and sess["session"].running:
        if sess.get("device") != device:
            held = ("the pod's own socket REPL" if sess.get("device") is None
                     else "DUT tty %s" % sess["device"])
            wanted = ("the pod's own socket REPL" if device is None
                      else "DUT tty %s" % device)
            raise ValueError(
                "'%s' already holds a session on %s; requested %s - "
                "session_close it first to switch targets"
                % (label, held, wanted))
        s = sess["session"]
        return {"label": label, "target": s.target, "log_path": sess["log_path"],
                "running": True, "mounted": s.mounted, "already_open": True,
                "note": "session already open; mount/exec/cp/soft_reset args "
                        "were ignored - session_close first to change them"}
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
    _REPL_SESSIONS[label] = {"session": s, "log_path": log_path, "device": device}
    return {"label": label, "target": s.target, "log_path": log_path,
            "running": s.running, "mounted": s.mounted}


def handle_pod_open(label: str, log_path: str = None, mount: str = None,
                    exec: str = None, cp=None, soft_reset: bool = False,
                    unsafe_links: bool = False, reconnect: bool = True) -> dict:
    """Open (or return the existing) persistent session on the pod's own
    socket REPL. Holds the pod's single REPL slot for the session's
    lifetime, excluding every other agent from pod_exec and from opening
    their own pod_open/dut_open session against this pod, until
    session_close - use session_send (not pod_exec) to run code while open.
    See _open_session for the setup chain and the already-open behaviour.
    """
    return _open_session(label, log_path=log_path, device=None, mount=mount,
                            exec=exec, cp=cp, soft_reset=soft_reset,
                            unsafe_links=unsafe_links, reconnect=reconnect)


def handle_dut_open(label: str, device: str, log_path: str = None,
                    mount: str = None, exec: str = None, cp=None,
                    soft_reset: bool = False, unsafe_links: bool = False,
                    reconnect: bool = True, recover: bool = False) -> dict:
    """Open (or return the existing) persistent session on the DUT's own CDC
    tty. `device` is the tty from dut_link(action="up") - the USB/IP link
    must already be attached; dut_open does not bring it up itself.

    recover=True runs Pod.recover_dut_repl on `device` before connecting
    (Ctrl-C then Ctrl-B, to leave a DUT latched in raw REPL mode for the
    friendly one) and folds its {ok, device, recovered, prompt_seen,
    was_raw, output} verdict into the returned dict under `recover`. See
    _open_session for the setup chain and the already-open behaviour.
    """
    result = {}
    if recover:
        result["recover"] = _pod_for(label).recover_dut_repl(device)
    result.update(_open_session(
        label, log_path=log_path, device=device, mount=mount, exec=exec,
        cp=cp, soft_reset=soft_reset, unsafe_links=unsafe_links,
        reconnect=reconnect))
    return result


def _require_repl(label: str):
    sess = _REPL_SESSIONS.get(label)
    if sess is None:
        raise KeyError(
            "No open session for '%s' - call pod_open or dut_open first."
            % label)
    return sess["session"]


def handle_session_read(label: str, since: int = None) -> dict:
    """Tail the session's buffered stdout after `since` (cursor from a prior read)."""
    return _require_repl(label).read_since(since)


def _session_write(label: str, data: str, newline: bool = True,
                     wait: float = 0.3) -> dict:
    """Inject a command into the target's stdin; return output captured in `wait`.

    Marks the stream cursor, writes `data` (a trailing newline submits a REPL
    line unless newline=False), waits `wait` seconds, and returns the output
    produced since - so a single call runs a command and reads its reply. Set
    wait=0 to send without reading (poll later with session_read).
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


def _session_interrupt(label: str, wait: float = 0.3) -> dict:
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


def handle_session_send(label: str, data: str = None, newline: bool = True,
                        wait: float = 0.3, control: str = None) -> dict:
    """Write to the session's stdin, or send a control character, and return
    the output captured within `wait` seconds.

    `control` selects a control character instead of `data`: "c" sends
    Ctrl-C (interrupt a running command or loop), reproducing
    _session_interrupt's result exactly (no `sent` key in the return);
    "b" sends Ctrl-B (leave a stuck raw REPL for the friendly one); "d"
    sends Ctrl-D (soft reset). Without `control`, `data` is required and is
    written verbatim, with a trailing newline submitting it as a REPL line
    unless newline=False. `data` and `control` are mutually exclusive.
    """
    if control is not None and data is not None:
        raise ValueError(
            "session_send: pass data or control, not both - control sends "
            "a control character in place of data")
    if control == "c":
        return _session_interrupt(label, wait=wait)
    if control in ("b", "d"):
        return _session_write(label, "\x02" if control == "b" else "\x04",
                                newline=False, wait=wait)
    if control is not None:
        raise ValueError("control must be 'c', 'b', or 'd'")
    if data is None:
        raise ValueError("data is required when control is not set")
    return _session_write(label, data, newline=newline, wait=wait)


def handle_session_close(label: str) -> dict:
    """Close the session (the target keeps running) and drop it from the registry."""
    sess = _REPL_SESSIONS.pop(label, None)
    if sess is None:
        return {"ok": True, "note": "no open session"}
    result = sess["session"].close()
    result["label"] = label
    return result


def handle_dut_exec(label: str, code: str) -> dict:
    """Run MicroPython on the DUT (turnkey): ensure the USB/IP link, attach, and
    exec over the DUT's own CDC REPL. Returns {tty, returncode, stdout, stderr}."""
    return _pod_for(label).dut_exec(code)


# ── dut: identity + SWD debug port ─────────────────────────────────────────


def handle_dut_identify(label: str, adopt: bool = False) -> dict:
    """Probe the live DUT identity over SWD and reconcile it with the declared block.

    Returns the reconcile_dut verdict (MATCH/MISMATCH/UNDECLARED/NO_DECLARED/
    NO_LIVE) with the per-field declared-vs-live ids. With adopt=True, snapshots
    the live ids into the declared expected{} block first.
    """
    entry = _entry_for(label)
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
            entry = _entry_for(label)
    return reconcile_dut(entry.get("dut"), live)


def handle_dut_halt(label: str, keep_attached: bool = False) -> dict:
    """Halt the DUT core over SWD (no auto-resume). Freezes the DUT incl. USB;
    detaches a live USB/IP session first unless keep_attached."""
    return _pod_for(label).halt_dut(keep_attached=keep_attached)


def handle_dut_resume(label: str) -> dict:
    """Resume the DUT core over SWD after a dut_halt / dut_reset mode='halt'."""
    return _pod_for(label).resume_dut()


def _read_reg(label: str, reg) -> dict:
    """Read one DUT core register over SWD (core must be halted first)."""
    return _pod_for(label).read_reg(reg)


def _write_reg(label: str, reg, value: int) -> dict:
    """Write one DUT core register over SWD (core must be halted first)."""
    return _pod_for(label).write_reg(reg, value)


def handle_dut_reg(label: str, reg, value: int = None) -> dict:
    """Read (value omitted) or write (value given) one DUT core register over
    SWD. The core must be halted first (dut_halt, or dut_reset mode='halt')."""
    if value is None:
        return _read_reg(label, reg)
    return _write_reg(label, reg, value)


def _read_mem_inline(label: str, addr: int, length: int) -> dict:
    """Read DUT memory over SWD, returned inline as hex (<= 4096 bytes)."""
    return _pod_for(label).read_mem(addr, length)


def _write_mem(label: str, addr: int, data_hex: str) -> dict:
    """Write DUT memory over SWD; refuses the declared flash + code region."""
    entry = _entry_for(label)
    return Pod.from_entry(entry).write_mem(
        addr, data_hex, protect=dut_protect_ranges(entry))


def _read_mem_to_file(label: str, addr: int, length: int, out_path: str) -> str:
    """Read DUT memory to a host file via the pod (streamed, no pod FS)."""
    pod = _pod_for(label)
    return pod.read_dut(addr, length, out_path)


def handle_dut_mem(label: str, addr: int, length: int = None,
                   data: str = None, out_path: str = None):
    """Read or write DUT memory over SWD.

    `data` given writes it (a hex string, refused inside the declared flash
    and code region); otherwise reads `length` bytes, inline as hex
    (<= 4096 bytes) by default or streamed to a host file at `out_path` when
    given (unbounded, unlike the inline cap). `data` and `out_path` are
    mutually exclusive - one names a write payload, the other a read
    destination.
    """
    if data is not None and out_path is not None:
        raise ValueError(
            "dut_mem: data (write) and out_path (read destination) are "
            "mutually exclusive - pass one or the other")
    if data is not None:
        return _write_mem(label, addr, data)
    if length is None:
        raise ValueError("length is required for a DUT memory read")
    if out_path:
        return _read_mem_to_file(label, addr, length, out_path)
    return _read_mem_inline(label, addr, length)


# Running GDB sessions keyed by label, so an agent can start a session and
# spawn its own gdb against the returned endpoint. The host GdbServer runs in a
# background thread (a blocking RSP session does not fit a request/response
# tool call).
_GDB_SESSIONS: dict = {}


def handle_dut_gdb(label: str, listen_port: int = 0) -> dict:
    """Start an on-pod GDB server and a background host RSP translator.

    Non-interactive shape: returns {"endpoint": "127.0.0.1:<port>",
    "gdb_port": <pod_port>, "label": label} once the local listener is bound,
    so the agent spawns arm-none-eabi-gdb itself. The host RSP session runs in
    a background thread until gdb detaches.
    """
    entry = _entry_for(label)
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


# ── dut: flash image (over SWD via the on-pod debug stack) ─────────────────


def handle_dut_flash(label: str, image: str, target: str = None,
                     addr: int = 0, keep_attached: bool = False,
                     mass_erase: bool = False, loader: str = None) -> dict:
    """Flash a firmware image to the DUT via the pod (streamed, no pod FS).

    Accepts a flat binary or an ELF file (detected by magic, not extension).
    For ELF images the DUT flash geometry must be declared in the registry dut
    block (flash_base + flash_size); addr is ignored.

    loader selects the flash backend: "native" (default) is the per-family NVM
    path; "flm" runs the target's CMSIS-pack algorithm, installed on the pod
    first.

    Detaches a live USB/IP session first (reflashing the DUT mid-forward wedges
    the pod); keep_attached=True overrides.
    """
    pod = _pod_for(label)
    return pod.flash_dut(image, target=target, addr=addr,
                         keep_attached=keep_attached, mass_erase=mass_erase,
                         loader=loader)


def handle_dut_erase(label: str, clkdiv: int = DEFAULT_SWD_CLKDIV,
                     loader: str = "native") -> dict:
    """Erase the entire DUT flash via the on-pod debug stack.

    loader: "native" (default) = nRF NVMC mass-erase fast-path; "flm" = the
    generic CMSIS path, which resolves the target's CMSIS-pack algorithm and
    installs it on the pod first.
    Returns {ok, ms, loader, err}.
    """
    return _pod_for(label).erase_dut(clkdiv=clkdiv, loader=loader)


def handle_dut_reset(label: str, mode: str = "sysreset",
                     keep_attached: bool = False) -> dict:
    """Reset the DUT via the pod ('sysreset' to run, 'halt' to catch reset).

    Also the first recovery step for an unresponsive/wedged DUT: a SWD system
    reset re-inits the core and peripherals (incl. USB), so a hung target
    re-enumerates cleanly without a physical power-cycle.
    """
    pod = _pod_for(label)
    return pod.reset_dut(mode=mode, keep_attached=keep_attached)


# ── dut: USB/IP link ────────────────────────────────────────────────────────


def handle_dut_link(label: str, action: str = "status",
                    ensure: bool = True) -> dict:
    """Inspect or drive the DUT's USB/IP link.

    action="status" (the default) is a pure read: it lists what the pod
    exports over USB/IP plus which host vhci ports are attached to it,
    starting nothing on the pod. "up" brings the pod USB host + usbip server
    up (unless ensure=False) and attaches the DUT on this host, which needs
    passwordless sudo for usbip, and returns {busid, vid, pid, tty} where tty
    is the DUT's own CDC REPL to hand to dut_open. "down" detaches every host
    vhci port attached to this pod's DUT. "reprobe" re-seeds a stale or
    unexported USB/IP slot (a DUT mounted-but-unexportable, or a warm-reset
    connect-edge miss) without a cold power cycle.

    Only "up" can activate the pod USB host, which has been observed to
    disturb the pod's Wi-Fi link, its only management channel.
    """
    if action not in ("status", "up", "down", "reprobe"):
        raise ValueError("action must be one of 'status', 'up', 'down', 'reprobe'")
    pod = _pod_for(label)
    if action == "up":
        return pod.usbip_attach(ensure=ensure)
    if action == "down":
        return pod.usbip_detach()
    if action == "reprobe":
        return pod.reprobe_dut()
    return {"label": label, "exported": pod.usbip_list(),
            "attached_ports": pod.attached_ports()}


# ── bench: pod instruments pointed at the DUT ───────────────────────────────


def _i2c_device_up(label: str, addr: int = 0x42, regs=None, bus: int = 1,
                      scl: int = 11, sda: int = 10, size: int = 256,
                      name: str = "i2c_target") -> dict:
    """Bring up a persistent hardware I2C target (register file) on the pod."""
    return _pod_for(label).i2c_target(addr=addr, regs=regs, bus=bus, scl=scl,
                                      sda=sda, size=size, name=name)


def _i2c_device_regs(label: str, off: int = 0, length=None, write=None,
                           name: str = "i2c_target") -> dict:
    """Read or write the pod I2C target's register file from the host."""
    return _pod_for(label).i2c_target_regs(off=off, length=length, write=write,
                                           name=name)


def _spi_device_up(label: str, mode: int = 0, bits: int = 8, miso: int = 16,
                      mosi: int = 19, sck: int = 18, cs: int = 17,
                      size: int = 1024, personality: str = "stream",
                      table_size: int = 256, name: str = "spi_target") -> dict:
    """Bring up a persistent PIO SPI target on the pod."""
    return _pod_for(label).spi_target(mode=mode, bits=bits, miso=miso, mosi=mosi,
                                      sck=sck, cs=cs, size=size,
                                      personality=personality,
                                      table_size=table_size, name=name)


def _spi_device_status(label: str, name: str = "spi_target") -> dict:
    """Read the pod SPI target's status: byte count, transfer count, captured ring."""
    return _pod_for(label).spi_target_status(name=name)


def _spi_device_regs(label: str, off: int = 0, length=None, write=None,
                           table: str = "read", name: str = "spi_target") -> dict:
    """Read or write the pod SPI target's regfile backing table from the host."""
    return _pod_for(label).spi_target_regs(off=off, length=length, write=write,
                                           table=table, name=name)


def _device_release(label: str, name: str = "*") -> dict:
    """Release one named pod peripheral instance, or all with '*'."""
    return _pod_for(label).peripheral_release(name=name)


def handle_bench_device(label: str, bus: str = None, action: str = "up",
                        name: str = None, addr: int = 0x42, regs=None,
                        i2c_bus: int = 1, scl: int = 11, sda: int = 10,
                        size: int = None, mode: int = 0, bits: int = 8,
                        miso: int = 16, mosi: int = 19, sck: int = 18,
                        cs: int = 17, personality: str = "stream",
                        table_size: int = 256) -> dict:
    """Bring up, inspect, or release the pod presenting itself as a device on
    the DUT's I2C or SPI bus.

    action="down" releases the named instance (name="*" sweeps every I2C and
    SPI instance together) and ignores `bus`, since release is not
    bus-scoped. action="up" and action="status" require bus="i2c" or
    bus="spi". I2C has no transfer counters, so bus="i2c" action="status"
    reports whether the named instance is up (from peripheral_list) instead
    of the byte/transfer counts bus="spi" action="status" returns. `i2c_bus`
    is the hardware I2C bus id (distinct from `bus`, the i2c/spi selector).
    """
    if action not in ("up", "status", "down"):
        raise ValueError("action must be one of 'up', 'status', 'down'")
    if action == "down":
        return _device_release(label, name=name or "*")
    if bus not in ("i2c", "spi"):
        raise ValueError("bus must be 'i2c' or 'spi' for action=%r" % action)
    if bus == "i2c":
        iname = name or "i2c_target"
        if action == "up":
            kwargs = {"addr": addr, "regs": regs, "bus": i2c_bus, "scl": scl,
                     "sda": sda, "name": iname}
            if size is not None:
                kwargs["size"] = size
            return _i2c_device_up(label, **kwargs)
        listed = _pod_for(label).peripheral_list()
        names = listed.get("instances") or []
        return {"label": label, "bus": "i2c", "name": iname,
                "present": iname in names,
                "note": "no transfer counters for I2C; presence is read "
                        "from peripheral_list's instance name list"}
    iname = name or "spi_target"
    if action == "up":
        kwargs = {"mode": mode, "bits": bits, "miso": miso, "mosi": mosi,
                 "sck": sck, "cs": cs, "personality": personality,
                 "table_size": table_size, "name": iname}
        if size is not None:
            kwargs["size"] = size
        return _spi_device_up(label, **kwargs)
    return _spi_device_status(label, name=iname)


def handle_bench_device_regs(label: str, bus: str, off: int = 0, length=None,
                             write=None, table: str = "read",
                             name: str = None) -> dict:
    """Read or write the pod's I2C or SPI target's register file from the host.

    `table` selects the SPI backing table ('read' served on MISO, 'write'
    filled from MOSI) and is ignored for bus="i2c".
    """
    if bus == "i2c":
        return _i2c_device_regs(label, off=off, length=length,
                                      write=write, name=name or "i2c_target")
    if bus == "spi":
        return _spi_device_regs(label, off=off, length=length,
                                      write=write, table=table,
                                      name=name or "spi_target")
    raise ValueError("bus must be 'i2c' or 'spi'")


def handle_bench_gpio(label: str, pin: int, value=None, mode: str = "out",
                pull=None) -> dict:
    """Read (value=None) or drive a pod GPIO."""
    return _pod_for(label).gpio(pin, value=value, mode=mode, pull=pull)


def handle_bench_adc(label: str, pin: int) -> dict:
    """Sample a pod ADC channel (raw u16 + 3.3V-ref volts)."""
    return _pod_for(label).adc(pin)


def handle_bench_la(label: str, base_pin: int, width: int = 1,
                         rate: int = 1000000, depth: int = 8000, trigger=None,
                         out_path: str = "capture.vcd", sm_id: int = 0,
                         names=None) -> dict:
    """Capture DUT pins with the pod logic analyser and write a VCD file."""
    trig = tuple(trigger) if trigger else None
    return _pod_for(label).logic_analyse(
        base_pin=base_pin, width=width, rate=rate, depth=depth, trigger=trig,
        out_path=out_path, sm_id=sm_id, names=names)


def handle_bench_uart(label: str, port: int = None, duration: float = 30.0) -> dict:
    """Stream DUT UART output (tail) over the pod's TCP UART bridge.

    Bounded by duration (default 30s) so an agent cannot hold an open infinite
    stream. Read-only: the TX direction is CLI-only. Connects to the pod's
    always-bound UART listener on the advertised uart_port, or port if given.
    """
    entry = _entry_for(label)
    effective_port = port or (entry.get("uart_port") or 2000)
    return Pod.from_entry(entry).uart_stream(port=effective_port, duration=duration)


# ── MCP server construction ───────────────────────────────────────────────


def build_server():
    """Construct and return the MCP Server instance."""
    server = Server(
        "annealage-pod",
        instructions=(
            "One pod is typically shared across agents. Two usage contracts:\n"
            "1. SINGLE-CLIENT REPL: the pod's socket REPL has one slot. A "
            "pod_open or dut_open session (or a pod_exec/dut_exec) holds it, "
            "and a second concurrent connect is refused by design - agents "
            "must SERIALIZE pod REPL access (finish an exec, or "
            "session_close a session, before another agent connects). This "
            "is not a fault; it is the pod's contract.\n"
            "2. DUT RE-ENUM -> STALE EXPORT: if a forwarded DUT re-enumerates "
            "(reset / replug / re-flash), the usbip export slot can go stale "
            "- a fresh attach then floods 0xff-then-quiet, or the host logs "
            "'string descriptor 0 read error: -19'. Call "
            "dut_link(action=\"reprobe\") to refresh it (dut_link(action="
            "\"up\") / dut_exec auto-reprobe only on an EMPTY export, not a "
            "stale-but-present one). See docs/pod/troubleshooting.md."
        ),
    )

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name="pod_discover",
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
                name="pod_register",
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
                name="pod_info",
                description=(
                    "Return registry info for a named pod: its stable handles "
                    "(hostname, addr6 IPv6 list, addr4), identity fingerprint, "
                    "ports, and declared DUT block - plus every session open "
                    "in this process (`sessions`, process-wide, not scoped to "
                    "`label`)."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."}
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="pod_exec",
                description=(
                    "Run a MicroPython code string on the POD's own interpreter "
                    "and return stdout. This is pod-side (the pod's debug stack "
                    "/ peripherals), NOT the DUT. To run code on the DUT use "
                    "dut_exec for a single call, or dut_open + session_send for "
                    "more than one."),
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
                name="pod_mount",
                description=(
                    "Mount a local host directory on the pod over ampremote. "
                    "One-shot: unmounts on return. For a directory kept "
                    "mounted across a session, use pod_open/dut_open's "
                    "`mount` argument instead."),
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
                name="pod_open",
                description=(
                    "Open a persistent streaming REPL session on the pod's own "
                    "socket REPL (where its asyncio app + aiorepl live). "
                    "Streams stdout to a log file AND an in-memory tail "
                    "buffer; inject commands with session_send and tail with "
                    "session_read. Holds the pod's SINGLE REPL slot for the "
                    "session's lifetime, EXCLUDING every other agent from "
                    "pod_exec and from opening their own session against this "
                    "pod, until session_close - use session_send (not "
                    "pod_exec) to run code while open. Chain setup before "
                    "connecting (mpremote-style): `soft_reset`, `cp`, `exec`, "
                    "then `mount` (kept for the session lifetime - the reason "
                    "to use pod_open over pod_mount, which is one-shot and "
                    "unmounts on return). Returns {label, target, log_path, "
                    "running, mounted}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "log_path": {
                            "type": "string",
                            "description": "File to append all output to "
                                           "(default: a temp file, returned).",
                        },
                        "mount": {
                            "type": "string",
                            "description": "Host dir to mount on the pod for "
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
                            "description": "Soft-reset the pod before connecting.",
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
                name="dut_open",
                description=(
                    "Open a persistent streaming REPL session on the DUT's own "
                    "CDC tty. `device` is the tty from dut_link(action=\"up\") "
                    "- the USB/IP link must already be attached; dut_open does "
                    "not bring it up itself. recover=true runs the Ctrl-C / "
                    "Ctrl-B un-stick over `device` first, for a DUT latched in "
                    "raw REPL mode, and folds its verdict into the result under "
                    "`recover`. Streams stdout to a log file AND an in-memory "
                    "tail buffer; inject commands with session_send and tail "
                    "with session_read. Chain setup before connecting "
                    "(mpremote-style): `soft_reset`, `cp`, `exec`, then `mount` "
                    "(kept for the session lifetime). Refuses if `label` "
                    "already holds a session on a different target (e.g. a "
                    "pod_open session) - session_close it first. Returns "
                    "{label, target, log_path, running, mounted}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "device": {
                            "type": "string",
                            "description": "DUT CDC tty from "
                                           "dut_link(action=\"up\") (e.g. "
                                           "/dev/ttyACM0).",
                        },
                        "recover": {
                            "type": "boolean",
                            "description": "Run the Ctrl-C/Ctrl-B un-stick "
                                           "over `device` before connecting.",
                            "default": False,
                        },
                        "log_path": {
                            "type": "string",
                            "description": "File to append all output to "
                                           "(default: a temp file, returned).",
                        },
                        "mount": {
                            "type": "string",
                            "description": "Host dir to mount on the DUT for "
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
                            "description": "Soft-reset the DUT before connecting.",
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
                    "required": ["label", "device"],
                },
            ),
            Tool(
                name="session_send",
                description=(
                    "Write to an open session's stdin, or send a control "
                    "character, and return the output captured within `wait` "
                    "seconds. `control` sends a control character instead of "
                    "`data`: \"c\" = Ctrl-C (interrupt a running command or "
                    "loop); \"b\" = Ctrl-B (leave a stuck raw REPL for the "
                    "friendly one); \"d\" = Ctrl-D (soft reset). Without "
                    "`control`, `data` is required; a trailing newline "
                    "submits it as a REPL line unless newline=false. Set "
                    "wait=0 to fire-and-forget and poll with session_read."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "data": {
                            "type": "string",
                            "description": "Text to send (a REPL command "
                                           "line); omit when using control.",
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
                        "control": {
                            "type": "string",
                            "enum": ["c", "b", "d"],
                            "description": "Send Ctrl-C / Ctrl-B / Ctrl-D "
                                           "instead of `data`.",
                        },
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="session_read",
                description=(
                    "Tail an open session's buffered stdout. Pass the "
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
                            "description": "Cursor from a prior session_read.",
                        },
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="session_close",
                description=(
                    "Close an open session (the target keeps running) and "
                    "free the pod's REPL slot. Returns {ok, received, label}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="dut_exec",
                description=(
                    "Run MicroPython on the DUT (turnkey): ensure the pod "
                    "USB/IP link, attach the DUT, and exec the code over its "
                    "own CDC REPL. Returns {tty, returncode, stdout, stderr}. "
                    "Each call detaches, re-attaches, and retries the "
                    "connection, carrying a multi-second floor; prefer "
                    "dut_open + session_send over repeated dut_exec calls. "
                    "For pod-side code use pod_exec instead. The forwarded "
                    "REPL is reliable when the DUT is correctly flashed and "
                    "is not DTR-gated by host ModemManager (run `pod "
                    "install-udev` once). If it fails or floods 0xff / "
                    "'could not enter raw repl', do NOT assume a pod "
                    "limitation - walk docs/pod/troubleshooting.md (a 0xff "
                    "flood is usually an incomplete flash or a stale usbip "
                    "slot, not a pod bug)."),
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
                name="dut_identify",
                description=(
                    "Probe the connected DUT's identity over SWD (dpidr, "
                    "ap_idr, cpuid, rom_base) and reconcile it against the "
                    "pod's declared DUT block. Returns a verdict (MATCH / "
                    "MISMATCH / UNDECLARED / NO_DECLARED / NO_LIVE). "
                    "adopt=true snapshots the live ids into the declared "
                    "expected{} block."),
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
                name="dut_halt",
                description=(
                    "Halt the DUT core over the pod's SWD debug interface (the "
                    "on-pod probe) and hold it - no auto-resume. REQUIRED "
                    "before dut_reg (registers need a halted core). Freezes "
                    "the target where it is, including its USB, so any active "
                    "USB/IP forward stalls until dut_resume. SWD only: needs "
                    "the DUT wired + powered for SWD; unrelated to the USB/IP "
                    "forward and the DUT's CDC REPL. Returns {ok, halted, "
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
                    "Resume the DUT core over SWD after a dut_halt (or "
                    "dut_reset mode='halt'). SWD debug interface only. "
                    "Returns {ok, halted:false}."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="dut_reg",
                description=(
                    "Read (value omitted) or write (value given) one DUT core "
                    "register over the pod's SWD debug interface. The core "
                    "MUST be halted first (dut_halt, or dut_reset mode='halt') "
                    "- registers are read/written through the debug "
                    "DCRSR/DCRDR, which require a halted core; a running core "
                    "returns {ok:false}. reg is a number 0..18 or a name: "
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
                        "value": {
                            "type": "integer",
                            "description": "32-bit value to write; omit to read.",
                        },
                    },
                    "required": ["label", "reg"],
                },
            ),
            Tool(
                name="dut_mem",
                description=(
                    "Read or write DUT memory over the pod's SWD debug "
                    "interface. `data` given writes it (a hex string, e.g. "
                    "'deadbeef'; writes into the flash region "
                    "(addr < 0x20000000) are REFUSED - flash needs erase, use "
                    "dut_flash). Without `data`, reads `length` bytes: inline "
                    "as hex (<= 4096 bytes) by default, or streamed to a host "
                    "file at `out_path` when given (unbounded). A live MEM-AP "
                    "access - works whether the core runs or is halted (a "
                    "read of a location the running core is changing may be "
                    "non-coherent; dut_halt first for a coherent snapshot). "
                    "SWD only; needs the DUT wired for SWD."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "addr": {"type": "integer", "description": "Source/destination address."},
                        "length": {
                            "type": "integer",
                            "description": "Bytes to read (required for a "
                                           "read; <= 4096 unless out_path is "
                                           "given).",
                        },
                        "data": {
                            "type": "string",
                            "description": "Bytes to write as a hex string "
                                           "(e.g. 'deadbeef'); given = write.",
                        },
                        "out_path": {
                            "type": "string",
                            "description": "Stream a read to this host file "
                                           "instead of returning it inline "
                                           "(no size cap).",
                        },
                    },
                    "required": ["label", "addr"],
                },
            ),
            Tool(
                name="dut_gdb",
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
                name="dut_flash",
                description=(
                    "Flash a firmware image to the DUT over SWD via the pod, "
                    "streamed into pod RAM (no pod filesystem). Accepts a flat "
                    "binary or an ELF file (detected by magic, not extension); "
                    "for ELF the DUT flash geometry must be declared in the "
                    "registry dut block (flash_base + flash_size)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "image": {
                            "type": "string",
                            "description": "Firmware image path (raw binary or ELF).",
                        },
                        "addr": {
                            "type": "integer",
                            "description": "Flash base address (flat binary only; ignored for ELF).",
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
                        "mass_erase": {
                            "type": "boolean",
                            "description": "Erase the entire DUT flash before programming.",
                            "default": False,
                        },
                        "loader": {
                            "type": "string",
                            "enum": ["native", "flm"],
                            "description": "Flash algorithm: 'native' (per-family NVM, default) or 'flm' (the target's CMSIS-pack algorithm).",
                        },
                    },
                    "required": ["label", "image"],
                },
            ),
            Tool(
                name="dut_erase",
                description=(
                    "Erase the entire DUT flash via the on-pod debug stack. "
                    "loader 'native' (default) uses the nRF NVMC mass-erase "
                    "fast-path; 'flm' runs the target's CMSIS-pack algorithm, "
                    "resolved from the DUT's declared target_family and "
                    "installed on the pod first. Returns {ok, ms, loader, err}."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "loader": {
                            "type": "string",
                            "enum": ["native", "flm"],
                            "description": "Flash algorithm: 'native' (nRF NVMC fast-path, default) or 'flm' (generic CMSIS-pack algorithm, any target with a pack).",
                            "default": "native",
                        },
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="dut_reset",
                description=(
                    "Reset the DUT via the on-pod debug probe (SWD SYSRESETREQ). "
                    "FIRST thing to try when the DUT is unresponsive or suspected "
                    "wedged (hung firmware, a soft-reset that left its USB/serial "
                    "hung, a stuck peripheral): a system reset re-inits the core "
                    "AND peripherals (incl. USB), so a target whose USB-CDC/REPL "
                    "wedged re-enumerates cleanly - no physical replug/power-cycle "
                    "needed. Use mode 'sysreset' to reset and run, 'halt' to reset "
                    "and catch the reset vector for debugging. Use 'nrst' to pulse "
                    "the dedicated DUT reset wire instead of going over SWD - it "
                    "needs that wire but no working SWD session, so it is the path "
                    "to try when SWD itself is unavailable (target unpowered, "
                    "wedged, or access-port locked). Only fall back to a physical "
                    "power-cycle if the reset itself reports an error."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "mode": {
                            "type": "string",
                            "enum": ["sysreset", "halt", "nrst"],
                            "description": ("sysreset = reset and run; halt = reset and halt; "
                                            "nrst = pulse the dedicated reset wire (no SWD needed)."),
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
                name="dut_link",
                description=(
                    "Inspect or drive the DUT's USB/IP link. action=\"status\" "
                    "(the default) is a pure read: it reports what the pod "
                    "exports over USB/IP plus which host vhci ports are "
                    "attached to it, without starting anything on the pod; "
                    "\"up\" brings the pod USB host + usbip server up (unless "
                    "ensure=false) and attaches the DUT on this host, "
                    "returning {busid, vid, pid, tty} (needs passwordless "
                    "sudo for usbip; NB activating the pod USB host can "
                    "disturb the pod's Wi-Fi link); \"down\" detaches every "
                    "host vhci port attached to this pod's DUT; \"reprobe\" "
                    "re-seeds a stale or unexported USB/IP slot without a "
                    "cold power cycle (try this when action=\"up\" reports "
                    "the pod exports no USB device but the DUT is wired and "
                    "powered). dut_exec brings the link up itself; dut_open "
                    "needs the tty this returns from action=\"up\", so run "
                    "that first."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "action": {
                            "type": "string",
                            "enum": ["status", "up", "down", "reprobe"],
                            "description": "Link operation to perform.",
                            "default": "status",
                        },
                        "ensure": {
                            "type": "boolean",
                            "description": "action=up only: start the pod USB "
                                           "host + usbip server first.",
                            "default": True,
                        },
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="bench_gpio",
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
                name="bench_adc",
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
                name="bench_la",
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
                name="bench_device",
                description=(
                    "Bring up, inspect, or release the pod presenting itself "
                    "as a register-file device on the DUT's I2C or SPI bus. "
                    "bus=\"i2c\" or bus=\"spi\" selects which; action=\"up\" "
                    "(default) brings up a persistent instance, "
                    "action=\"status\" reads it back (SPI: byte/transfer "
                    "counters + captured ring; I2C: whether the named "
                    "instance is up - no transfer counters exist), "
                    "action=\"down\" releases the named instance (name=\"*\", "
                    "the default, sweeps every I2C and SPI instance together, "
                    "ignoring bus). i2c_bus is the hardware I2C bus id "
                    "(distinct from the bus=i2c/spi selector). Instances "
                    "persist until released."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "bus": {"type": "string", "enum": ["i2c", "spi"], "description": "Which personality (required for action up/status)."},
                        "action": {"type": "string", "enum": ["up", "status", "down"], "description": "Operation to perform.", "default": "up"},
                        "name": {"type": "string", "description": "Instance name (default: 'i2c_target'/'spi_target' by bus; '*' on action=down sweeps all)."},
                        "addr": {"type": "integer", "description": "i2c up: 7-bit I2C address.", "default": 66},
                        "regs": {"type": "array", "items": {"type": "integer"}, "description": "i2c up: initial register bytes from offset 0."},
                        "i2c_bus": {"type": "integer", "description": "i2c up: hardware I2C bus id.", "default": 1},
                        "scl": {"type": "integer", "description": "i2c up: SCL GPIO.", "default": 11},
                        "sda": {"type": "integer", "description": "i2c up: SDA GPIO.", "default": 10},
                        "size": {"type": "integer", "description": "up: register/capture size in bytes (default 256 for i2c, 1024 for spi).", "minimum": 1, "maximum": 8192},
                        "mode": {"type": "integer", "enum": [0, 1, 2, 3], "description": "spi up: SPI mode.", "default": 0},
                        "bits": {"type": "integer", "enum": [8], "description": "spi up: frame width in bits (8-bit only).", "default": 8},
                        "miso": {"type": "integer", "description": "spi up: MISO GPIO.", "default": 16},
                        "mosi": {"type": "integer", "description": "spi up: MOSI GPIO.", "default": 19},
                        "sck": {"type": "integer", "description": "spi up: SCK GPIO.", "default": 18},
                        "cs": {"type": "integer", "description": "spi up: CS GPIO, active low.", "default": 17},
                        "personality": {"type": "string", "enum": ["stream", "regfile"], "description": "spi up: responder personality.", "default": "stream"},
                        "table_size": {"type": "integer", "description": "spi up, personality=regfile: register table size per direction.", "default": 256, "minimum": 1, "maximum": 4096},
                    },
                    "required": ["label"],
                },
            ),
            Tool(
                name="bench_device_regs",
                description=(
                    "Read or write the pod's I2C or SPI target's register "
                    "file from the host. off/length select a read window "
                    "(length defaults to the rest of the table); write, "
                    "given, writes bytes at off first. table selects the SPI "
                    "backing table ('read', served on MISO, or 'write', "
                    "filled from MOSI - personality=regfile only) and is "
                    "ignored for bus=\"i2c\"."),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "bus": {"type": "string", "enum": ["i2c", "spi"], "description": "Which target's register file."},
                        "off": {"type": "integer", "description": "Register offset.", "default": 0},
                        "length": {"type": "integer", "description": "Bytes to read (default: to end)."},
                        "write": {"type": "array", "items": {"type": "integer"}, "description": "Bytes to write at off first."},
                        "table": {"type": "string", "enum": ["read", "write"], "description": "spi only: which backing table to access.", "default": "read"},
                        "name": {"type": "string", "description": "Instance name (default: 'i2c_target'/'spi_target' by bus)."},
                    },
                    "required": ["label", "bus"],
                },
            ),
            Tool(
                name="bench_uart",
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
            if name == "pod_discover":
                result = await asyncio.to_thread(
                    handle_pod_discover, arguments.get("timeout", 5.0))
            elif name == "pod_register":
                result = await asyncio.to_thread(
                    handle_pod_register, arguments["label"],
                    arguments.get("match"), arguments.get("timeout", 5.0))
            elif name == "pod_info":
                result = await asyncio.to_thread(
                    handle_pod_info, arguments["label"])
            elif name == "pod_exec":
                result = await asyncio.to_thread(
                    handle_pod_exec, arguments["label"], arguments["code"])
            elif name == "pod_mount":
                result = await asyncio.to_thread(
                    handle_pod_mount, arguments["label"], arguments["directory"])
            elif name == "pod_open":
                result = await asyncio.to_thread(
                    handle_pod_open, arguments["label"],
                    arguments.get("log_path"), arguments.get("mount"),
                    arguments.get("exec"), arguments.get("cp"),
                    arguments.get("soft_reset", False),
                    arguments.get("unsafe_links", False),
                    arguments.get("reconnect", True))
            elif name == "dut_open":
                result = await asyncio.to_thread(
                    handle_dut_open, arguments["label"], arguments["device"],
                    arguments.get("log_path"), arguments.get("mount"),
                    arguments.get("exec"), arguments.get("cp"),
                    arguments.get("soft_reset", False),
                    arguments.get("unsafe_links", False),
                    arguments.get("reconnect", True),
                    arguments.get("recover", False))
            elif name == "session_send":
                result = await asyncio.to_thread(
                    handle_session_send, arguments["label"],
                    arguments.get("data"), arguments.get("newline", True),
                    arguments.get("wait", 0.3), arguments.get("control"))
            elif name == "session_read":
                result = await asyncio.to_thread(
                    handle_session_read, arguments["label"],
                    arguments.get("since"))
            elif name == "session_close":
                result = await asyncio.to_thread(
                    handle_session_close, arguments["label"])
            elif name == "dut_exec":
                result = await asyncio.to_thread(
                    handle_dut_exec, arguments["label"], arguments["code"])
            elif name == "dut_identify":
                result = await asyncio.to_thread(
                    handle_dut_identify, arguments["label"], arguments.get("adopt", False))
            elif name == "dut_halt":
                result = await asyncio.to_thread(
                    handle_dut_halt, arguments["label"],
                    arguments.get("keep_attached", False))
            elif name == "dut_resume":
                result = await asyncio.to_thread(
                    handle_dut_resume, arguments["label"])
            elif name == "dut_reg":
                result = await asyncio.to_thread(
                    handle_dut_reg, arguments["label"], arguments["reg"],
                    arguments.get("value"))
            elif name == "dut_mem":
                result = await asyncio.to_thread(
                    handle_dut_mem, arguments["label"], arguments["addr"],
                    arguments.get("length"), arguments.get("data"),
                    arguments.get("out_path"))
            elif name == "dut_gdb":
                result = await asyncio.to_thread(
                    handle_dut_gdb, arguments["label"],
                    arguments.get("listen_port", 0))
            elif name == "dut_flash":
                result = await asyncio.to_thread(
                    handle_dut_flash, arguments["label"], arguments["image"],
                    arguments.get("target"), arguments.get("addr", 0),
                    arguments.get("keep_attached", False),
                    arguments.get("mass_erase", False),
                    arguments.get("loader"))
            elif name == "dut_erase":
                result = await asyncio.to_thread(
                    handle_dut_erase, arguments["label"],
                    arguments.get("clkdiv", DEFAULT_SWD_CLKDIV),
                    arguments.get("loader", "native"))
            elif name == "dut_reset":
                result = await asyncio.to_thread(
                    handle_dut_reset, arguments["label"],
                    arguments.get("mode", "sysreset"),
                    arguments.get("keep_attached", False))
            elif name == "dut_link":
                result = await asyncio.to_thread(
                    handle_dut_link, arguments["label"],
                    arguments.get("action", "status"),
                    arguments.get("ensure", True))
            elif name == "bench_gpio":
                result = await asyncio.to_thread(
                    handle_bench_gpio, arguments["label"], arguments["pin"],
                    arguments.get("value"), arguments.get("mode", "out"),
                    arguments.get("pull"))
            elif name == "bench_adc":
                result = await asyncio.to_thread(
                    handle_bench_adc, arguments["label"], arguments["pin"])
            elif name == "bench_la":
                result = await asyncio.to_thread(
                    handle_bench_la, arguments["label"], arguments["base_pin"],
                    arguments.get("width", 1), arguments.get("rate", 1000000),
                    arguments.get("depth", 8000), arguments.get("trigger"),
                    arguments.get("out_path", "capture.vcd"),
                    arguments.get("sm_id", 0), arguments.get("names"))
            elif name == "bench_device":
                result = await asyncio.to_thread(
                    handle_bench_device, arguments["label"],
                    arguments.get("bus"), arguments.get("action", "up"),
                    arguments.get("name"), arguments.get("addr", 0x42),
                    arguments.get("regs"), arguments.get("i2c_bus", 1),
                    arguments.get("scl", 11), arguments.get("sda", 10),
                    arguments.get("size"), arguments.get("mode", 0),
                    arguments.get("bits", 8), arguments.get("miso", 16),
                    arguments.get("mosi", 19), arguments.get("sck", 18),
                    arguments.get("cs", 17), arguments.get("personality", "stream"),
                    arguments.get("table_size", 256))
            elif name == "bench_device_regs":
                result = await asyncio.to_thread(
                    handle_bench_device_regs, arguments["label"],
                    arguments["bus"], arguments.get("off", 0),
                    arguments.get("length"), arguments.get("write"),
                    arguments.get("table", "read"), arguments.get("name"))
            elif name == "bench_uart":
                result = await asyncio.to_thread(
                    handle_bench_uart, arguments["label"],
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
