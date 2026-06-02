"""pod CLI: registry and control for Annealage Pods over Wi-Fi.

Thin frontend over the pod core library. Mirrors the mpy-dev CLI conventions.

Subcommands:
  discover            browse mDNS for live pods
  list                show registered pods
  register <label>    add a pod to the registry
  unregister <label>  remove a pod from the registry
  info <label>        show full details for a registered pod
  repl <label>        attach interactive REPL
  mount <label> <dir> mount a host directory on the pod
  exec <label> <code> execute MicroPython code on the pod
  cp <label> <src> <dst>  copy a file to/from the pod
  flash <label> <image> [--target T]  flash a DUT image via the pod
  reset <label> [--mode MODE]         reset the DUT via the pod
  gdb <label> [--listen-port N]       start a local GDB RSP server to the DUT

Registry: $POD_CONFIG_DIR/pods.json (default: ~/.config/pod/pods.json)
"""

import argparse
import sys
from datetime import datetime, timezone

from pod.registry import (
    load_registry,
    save_registry,
    get_pod,
    set_pod,
    remove_pod,
)
from pod.client import Pod


# ── helpers ──────────────────────────────────────────────────────────────


def _require_pod(label: str):
    """Load a registry entry by label or exit with an error message."""
    entry = get_pod(label)
    if entry is None:
        print(f"Label '{label}' not found.", file=sys.stderr)
        sys.exit(1)
    return entry


# ── subcommand handlers ──────────────────────────────────────────────────


def cmd_discover(args):
    from pod.discovery import discover_pods

    timeout = getattr(args, "timeout", 5.0)
    print(f"Browsing {timeout}s for _annealage-pod._tcp ...")
    pods = discover_pods(timeout=timeout)
    if not pods:
        print("No pods found.")
        return 0
    for p in pods:
        usbip = f"  usbip={p.usbip_port}" if p.usbip_port else ""
        uart = f"  uart={p.uart_port}" if p.uart_port else ""
        gdb = f"  gdb={p.gdb_port}" if p.gdb_port else ""
        carrier = f"  carrier={p.carrier_id}" if p.carrier_id else ""
        mp = f"  mp={p.mp_version}" if p.mp_version else ""
        print(f"  {p.name}  {p.address}:{p.repl_port}{usbip}{uart}{gdb}{carrier}{mp}")
    return 0


def cmd_list(args):
    registry = load_registry()
    pods = registry.get("pods", {})
    if not pods:
        return 0
    max_label = max((len(k) for k in pods), default=5)
    max_label = max(max_label, 5)
    for label, entry in sorted(pods.items()):
        address = entry.get("address", "?")
        repl_port = entry.get("repl_port", "?")
        mp = entry.get("mp_version", "")
        last = entry.get("last_seen", "")
        desc = f"mp={mp}" if mp else ""
        print(f"  {label:<{max_label}}  {address}:{repl_port}  {desc}  {last}")
    return 0


def cmd_register(args):
    label = args.label

    existing = get_pod(label)
    if existing is not None and not args.force:
        print(f"Label '{label}' already registered. Use --force to overwrite.", file=sys.stderr)
        return 1

    # Build entry from CLI flags
    if args.address is None:
        print("--address is required for manual registration.", file=sys.stderr)
        return 1

    entry = {
        "address": args.address,
        "repl_port": args.repl_port,
        "usbip_port": args.usbip_port,
        "uart_port": args.uart_port,
        "gdb_port": args.gdb_port,
        "carrier_id": args.carrier_id or "",
        "mp_version": args.mp_version or "",
        "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if args.notes:
        entry["notes"] = args.notes

    set_pod(label, entry)
    print(f"Registered '{label}' at {args.address}:{args.repl_port}.")
    return 0


def cmd_unregister(args):
    removed = remove_pod(args.label)
    if not removed:
        print(f"Label '{args.label}' not found.", file=sys.stderr)
        return 1
    print(f"Unregistered '{args.label}'.")
    return 0


def cmd_info(args):
    entry = _require_pod(args.label)
    print(f"Label:      {args.label}")
    print(f"Address:    {entry.get('address', '(none)')}")
    print(f"REPL port:  {entry.get('repl_port', '(none)')}")
    print(f"USB/IP port:{entry.get('usbip_port') or '(none)'}")
    print(f"UART port:  {entry.get('uart_port') or '(none)'}")
    print(f"GDB port:   {entry.get('gdb_port') or '(none)'}")
    print(f"Carrier:    {entry.get('carrier_id') or '(none)'}")
    print(f"MP version: {entry.get('mp_version') or '(none)'}")
    print(f"Last seen:  {entry.get('last_seen') or '(unknown)'}")
    notes = entry.get("notes")
    if notes:
        print("Notes:")
        for line in notes.splitlines():
            print(f"  {line}")
    return 0


def cmd_repl(args):
    entry = _require_pod(args.label)
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    pod.repl()
    return 0


def cmd_mount(args):
    entry = _require_pod(args.label)
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    pod.mount(args.directory)
    return 0


def cmd_exec(args):
    entry = _require_pod(args.label)
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    output = pod.exec(args.code)
    if output:
        print(output, end="")
    return 0


def cmd_cp(args):
    entry = _require_pod(args.label)
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    pod.cp(args.src, args.dst)
    return 0


def cmd_flash(args):
    entry = _require_pod(args.label)
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    addr = int(args.addr, 0) if isinstance(args.addr, str) else args.addr
    result = pod.flash_dut(args.image, target=args.target, addr=addr)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_reset(args):
    entry = _require_pod(args.label)
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    result = pod.reset_dut(mode=args.mode)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_gdb(args):
    entry = _require_pod(args.label)
    pod = Pod(address=entry["address"], repl_port=entry.get("repl_port", 8266))
    gdb_port = args.gdb_port or entry.get("gdb_port") or 3335

    def _announce(host, port):
        print(f"target extended-remote {host}:{port}")

    pod.gdb_endpoint(
        listen_port=args.listen_port,
        gdb_port=gdb_port,
        reset_halt=not args.no_reset_halt,
        resume_window_ms=args.resume_window_ms,
        on_listen=_announce,
    )
    return 0


# ── main ─────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="pod",
        description="Discover and control Annealage Pods over Wi-Fi.",
        epilog="""\
examples:
  %(prog)s discover
  %(prog)s register my-pod --address 192.168.0.121 --repl-port 8266
  %(prog)s list
  %(prog)s info my-pod
  %(prog)s repl my-pod
  %(prog)s exec my-pod "import os; print(os.uname())"
  %(prog)s mount my-pod ./firmware
  %(prog)s cp my-pod ./main.py :main.py

registry: $POD_CONFIG_DIR/pods.json (default: ~/.config/pod/pods.json)""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    # discover
    p = sub.add_parser("discover", help="Browse mDNS for live pods")
    p.add_argument("--timeout", type=float, default=5.0, metavar="SECS",
                   help="Browse duration in seconds (default: 5)")

    # list
    sub.add_parser("list", help="Show registered pods")

    # register
    p = sub.add_parser("register", help="Register a pod under a label")
    p.add_argument("label", help="Short name for the pod (e.g. my-pod)")
    p.add_argument("--address", required=True, metavar="IP",
                   help="Pod IPv4 address")
    p.add_argument("--repl-port", type=int, default=8266, metavar="PORT",
                   dest="repl_port", help="REPL TCP port (default: 8266)")
    p.add_argument("--usbip-port", type=int, default=None, metavar="PORT",
                   dest="usbip_port", help="USB/IP TCP port")
    p.add_argument("--uart-port", type=int, default=None, metavar="PORT",
                   dest="uart_port", help="UART TCP port")
    p.add_argument("--gdb-port", type=int, default=3335, metavar="PORT",
                   dest="gdb_port", help="GDB debug TCP port (default: 3335)")
    p.add_argument("--carrier-id", default="", metavar="ID",
                   dest="carrier_id", help="Carrier board identifier")
    p.add_argument("--mp-version", default="", metavar="VER",
                   dest="mp_version", help="MicroPython version string")
    p.add_argument("--notes", default=None, metavar="TEXT",
                   help="Free-text notes")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing label")

    # unregister
    p = sub.add_parser("unregister", help="Remove a pod from the registry")
    p.add_argument("label")

    # info
    p = sub.add_parser("info", help="Show full details for a registered pod")
    p.add_argument("label")

    # repl
    p = sub.add_parser("repl", help="Attach interactive REPL to a pod")
    p.add_argument("label")

    # mount
    p = sub.add_parser("mount", help="Mount a local directory on the pod")
    p.add_argument("label")
    p.add_argument("directory", metavar="dir", help="Local directory to mount")

    # exec
    p = sub.add_parser("exec", help="Execute MicroPython code on a pod")
    p.add_argument("label")
    p.add_argument("code", help="MicroPython code string to execute")

    # cp
    p = sub.add_parser("cp", help="Copy a file to or from the pod")
    p.add_argument("label")
    p.add_argument("src", help="Source path (use ':path' for pod-side)")
    p.add_argument("dst", help="Destination path (use ':path' for pod-side)")

    # flash
    p = sub.add_parser("flash", help="Flash a DUT firmware image via the pod")
    p.add_argument("label")
    p.add_argument("image", help="Firmware image path (raw binary)")
    p.add_argument("--addr", default="0", help="Flash base address (default: 0)")
    p.add_argument("--target", default=None, help="Target MCU identifier")

    # reset
    p = sub.add_parser("reset", help="Reset the DUT via the pod")
    p.add_argument("label")
    p.add_argument("--mode", default="sysreset", choices=["sysreset", "halt"],
                   help="Reset method (default: sysreset)")

    # gdb
    p = sub.add_parser("gdb", help="Start a local GDB RSP server to the DUT")
    p.add_argument("label")
    p.add_argument("--listen-port", type=int, default=0, metavar="PORT",
                   dest="listen_port",
                   help="Local gdb-facing port (default: 0 = ephemeral)")
    p.add_argument("--gdb-port", type=int, default=None, metavar="PORT",
                   dest="gdb_port",
                   help="Pod debug TCP port (default: registry or 3335)")
    p.add_argument("--no-reset-halt", action="store_true",
                   dest="no_reset_halt",
                   help="Do not reset-and-halt the DUT on attach")
    p.add_argument("--resume-window-ms", type=int, default=200, metavar="MS",
                   dest="resume_window_ms",
                   help="RESUME_WAIT window in ms (default: 200)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    handler = {
        "discover": cmd_discover,
        "list": cmd_list,
        "register": cmd_register,
        "unregister": cmd_unregister,
        "info": cmd_info,
        "repl": cmd_repl,
        "mount": cmd_mount,
        "exec": cmd_exec,
        "cp": cmd_cp,
        "flash": cmd_flash,
        "reset": cmd_reset,
        "gdb": cmd_gdb,
    }[args.command]

    return handler(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
