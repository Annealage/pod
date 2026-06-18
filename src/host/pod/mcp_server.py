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

The mcp import is guarded so this module can be imported and tested
even if the mcp package is absent. build_server() is only called
from main().
"""

import asyncio
import sys
import threading
from pod.discovery import discover_pods as _discover_pods
from pod.registry import get_pod, load_registry, update_pod, reconcile_dut
from pod.client import Pod
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


def handle_dut_exec(label: str, code: str) -> str:
    """Execute MicroPython code on a pod. Returns stdout."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod.from_entry(entry)
    return pod.exec(code)


def handle_mount_dir(label: str, directory: str) -> str:
    """Mount a host directory on a pod. Returns status message."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod.from_entry(entry)
    pod.mount(directory)
    return f"Mounted {directory} on {label}."


def handle_flash_dut(label: str, image: str, target: str = None,
                     addr: int = 0) -> dict:
    """Flash a firmware image to the DUT via the pod (streamed, no pod FS)."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod.from_entry(entry)
    return pod.flash_dut(image, target=target, addr=addr)


def handle_reset_dut(label: str, mode: str = "sysreset") -> dict:
    """Reset the DUT via the pod ('sysreset' to run, 'halt' to catch reset).

    Also the first recovery step for an unresponsive/wedged DUT: a SWD system
    reset re-inits the core and peripherals (incl. USB), so a hung target
    re-enumerates cleanly without a physical power-cycle.
    """
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod.from_entry(entry)
    return pod.reset_dut(mode=mode)


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
                name="dut_exec",
                description="Execute a MicroPython code string on the pod and return stdout.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "code": {"type": "string", "description": "MicroPython code to execute."},
                    },
                    "required": ["label", "code"],
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
                    "Capture DUT pins with the pod's PIO logic analyser and write "
                    "a VCD file. Swaps SWD out for the capture (mutually exclusive), "
                    "then restores it lazily."
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
            elif name == "dut_exec":
                result = await asyncio.to_thread(
                    handle_dut_exec, arguments["label"], arguments["code"])
            elif name == "mount_dir":
                result = await asyncio.to_thread(
                    handle_mount_dir, arguments["label"], arguments["directory"])
            elif name == "flash_dut":
                result = await asyncio.to_thread(
                    handle_flash_dut, arguments["label"], arguments["image"],
                    arguments.get("target"), arguments.get("addr", 0))
            elif name == "reset_dut":
                result = await asyncio.to_thread(
                    handle_reset_dut, arguments["label"],
                    arguments.get("mode", "sysreset"))
            elif name == "read_dut":
                result = await asyncio.to_thread(
                    handle_read_dut, arguments["label"], arguments["addr"],
                    arguments["length"], arguments["out_path"])
            elif name == "gdb_dut":
                result = await asyncio.to_thread(
                    handle_gdb_dut, arguments["label"],
                    arguments.get("listen_port", 0))
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
                    arguments.get("sm_id", 10), arguments.get("names"))
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]
            return [TextContent(type="text", text=str(result))]

        except NotImplementedError as exc:
            return [TextContent(type="text", text=f"Not implemented: {exc}")]
        except PodUnreachable as exc:
            # No tier yielded an identity-confirmed target - unreachable, or a
            # DHCP-moved IPv4 whose fingerprint did not match. Distinct from a
            # generic failure so the agent does not retry blindly.
            return [TextContent(
                type="text",
                text=f"Pod unreachable or identity mismatch: {exc}")]
        except (LookupError, KeyError) as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]

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
