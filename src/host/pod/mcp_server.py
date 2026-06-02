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
from pod.registry import get_pod, load_registry
from pod.client import Pod

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


def handle_dut_exec(label: str, code: str) -> str:
    """Execute MicroPython code on a pod. Returns stdout."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    return pod.exec(code)


def handle_mount_dir(label: str, directory: str) -> str:
    """Mount a host directory on a pod. Returns status message."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    pod.mount(directory)
    return f"Mounted {directory} on {label}."


def handle_flash_dut(label: str, image: str, target: str = None,
                     addr: int = 0) -> dict:
    """Flash a firmware image to the DUT via the pod (streamed, no pod FS)."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    return pod.flash_dut(image, target=target, addr=addr)


def handle_reset_dut(label: str, mode: str = "sysreset") -> dict:
    """Reset the DUT via the pod ('sysreset' to run, 'halt' to catch reset)."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    return pod.reset_dut(mode=mode)


def handle_read_dut(label: str, addr: int, length: int, out_path: str) -> str:
    """Read DUT memory to a host file via the pod (streamed, no pod FS)."""
    entry = get_pod(label)
    if entry is None:
        raise KeyError(f"Pod '{label}' not found in registry.")
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
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

    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
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
                description="Return registry info for a named pod.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."}
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
                description="Reset the DUT via the on-pod debug probe.",
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
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]
            return [TextContent(type="text", text=str(result))]

        except NotImplementedError as exc:
            return [TextContent(type="text", text=f"Not implemented: {exc}")]
        except KeyError as exc:
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
