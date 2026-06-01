"""Pod MCP server (stdio transport).

Exposes pod control as MCP tools so an agent can drive the
hardware iteration loop: discover -> flash_dut -> reset_dut ->
observe (dut_exec, mount_dir) -> repeat.

Tools:
  discover_pods   browse mDNS for live pods
  pod_info        show registry info for a named pod
  dut_exec        execute MicroPython code on a pod
  mount_dir       mount a local directory on a pod
  flash_dut       (stub) flash a DUT image - pending Phase 2/3
  reset_dut       (stub) reset the DUT - pending Phase 2/3

The mcp import is guarded so this module can be imported and tested
even if the mcp package is absent. build_server() is only called
from main().
"""

import sys
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


def handle_flash_dut(label: str, image: str, target: str = None) -> str:
    """Stub: flash a DUT image via the pod. Pending Phase 2/3."""
    raise NotImplementedError(
        "flash_dut is not yet implemented - pending Phase 2/3 (on-pod FLM loader)"
    )


def handle_reset_dut(label: str, mode: str = "swd") -> str:
    """Stub: reset the DUT via the pod. Pending Phase 2/3."""
    raise NotImplementedError(
        "reset_dut is not yet implemented - pending Phase 2/3 (on-pod reset control)"
    )


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
                    "Flash a firmware image to the DUT via the on-pod loader. "
                    "NOT YET IMPLEMENTED - pending Phase 2/3."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "image": {"type": "string", "description": "Firmware image path."},
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
                    "Reset the DUT via the on-pod debug probe. "
                    "NOT YET IMPLEMENTED - pending Phase 2/3."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Pod label."},
                        "mode": {
                            "type": "string",
                            "enum": ["swd", "nrst", "power"],
                            "description": "Reset method.",
                            "default": "swd",
                        },
                    },
                    "required": ["label"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            if name == "discover_pods":
                result = handle_discover_pods(
                    timeout=arguments.get("timeout", 5.0)
                )
                return [TextContent(type="text", text=str(result))]

            elif name == "pod_info":
                result = handle_pod_info(arguments["label"])
                return [TextContent(type="text", text=str(result))]

            elif name == "dut_exec":
                result = handle_dut_exec(arguments["label"], arguments["code"])
                return [TextContent(type="text", text=result)]

            elif name == "mount_dir":
                result = handle_mount_dir(arguments["label"], arguments["directory"])
                return [TextContent(type="text", text=result)]

            elif name == "flash_dut":
                result = handle_flash_dut(
                    arguments["label"],
                    arguments["image"],
                    arguments.get("target"),
                )
                return [TextContent(type="text", text=result)]

            elif name == "reset_dut":
                result = handle_reset_dut(
                    arguments["label"],
                    arguments.get("mode", "swd"),
                )
                return [TextContent(type="text", text=result)]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

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
