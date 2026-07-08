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
  uart <label> [--port P] [--tx]      stream DUT UART over TCP (--tx for full-duplex)

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
    update_pod,
    remove_pod,
    reconcile_dut,
    dut_protect_ranges,
)
from pod.client import Pod, PodExecError


# ── helpers ──────────────────────────────────────────────────────────────


def _require_pod(label: str):
    """Load a registry entry by label or exit with an error message."""
    entry = get_pod(label)
    if entry is None:
        print(f"Label '{label}' not found.", file=sys.stderr)
        sys.exit(1)
    return entry


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _split_addr6(values):
    """Flatten repeated/comma-separated --addr6 values into a clean list."""
    out = []
    for v in values or []:
        for a in v.split(","):
            a = a.strip()
            if a and a not in out:
                out.append(a)
    return out


def _add_dut_flags(p):
    """Add the shared --dut-* declared-block flags to a subparser.

    Everything is --dut-* scoped so it never collides with pod flags (e.g. the
    pod's own --repl-port). The DUT block records identity, where the DUT is
    reached (usb/repl), and how it is wired, so an agent reads one pod_info and
    needs to ask nothing.
    """
    p.add_argument("--dut-label", default=None, dest="dut_label", metavar="NAME",
                   help="DUT friendly name")
    p.add_argument("--dut-family", default=None, dest="dut_family", metavar="FAM",
                   help="DUT target family/part, e.g. nRF52840_xxAA")
    p.add_argument("--dut-board", default=None, dest="dut_board", metavar="BOARD",
                   help="DUT board name, e.g. PCA10059")
    p.add_argument("--dut-flash-base", default=None, dest="dut_flash_base",
                   metavar="ADDR", help="DUT flash base address (e.g. 0x0)")
    p.add_argument("--dut-flash-size", default=None, dest="dut_flash_size",
                   metavar="BYTES", help="DUT flash size in bytes")
    p.add_argument("--dut-usb", default=None, dest="dut_usb", metavar="VID:PID[/CONN]",
                   help="DUT USB id + where it connects, e.g. f055:9802/pod-host "
                        "(conn: pod-host | agent-direct)")
    p.add_argument("--dut-repl", default=None, dest="dut_repl",
                   metavar="TRANSPORT[:PORT]",
                   help="How the DUT REPL is reached, e.g. usbip:3240 or cdc-direct")
    p.add_argument("--dut-wire", default=None, dest="dut_wire", action="append",
                   metavar="SPEC",
                   help="Per-interface wiring (repeatable), e.g. "
                        "i2c:pod.scl=11,pod.sda=10,dut.scl=42/P1.10,dut.sda=45/P1.13")
    p.add_argument("--dut-notes", default=None, dest="dut_notes", metavar="TEXT",
                   help="DUT-specific notes")


def _parse_usb(spec):
    """'f055:9802/pod-host' -> {'vid','pid','connection'}."""
    usb_part, _, conn = spec.partition("/")
    vid_s, _, pid_s = usb_part.partition(":")
    out = {}
    if vid_s:
        out["vid"] = "0x%04x" % int(vid_s, 16)
    if pid_s:
        out["pid"] = "0x%04x" % int(pid_s, 16)
    if conn:
        out["connection"] = conn
    return out


def _parse_repl(spec):
    """'usbip:3240' / 'cdc-direct' -> {'transport', 'port'?}."""
    transport, _, port = spec.partition(":")
    out = {"transport": transport}
    if port:
        out["port"] = int(port, 0)
    return out


def _parse_pin(rhs):
    """'42/P1.10' -> {'pin': 42, 'label': 'P1.10'}; '42' -> {'pin': 42}."""
    pin_s, _, label = rhs.partition("/")
    out = {"pin": int(pin_s, 0)}
    if label:
        out["label"] = label
    return out


def _parse_wire(spec):
    """'i2c:pod.scl=11,pod.sda=10,dut.scl=42/P1.10' -> (iface, {pod:{}, dut:{}})."""
    iface, _, rest = spec.partition(":")
    side = {"pod": {}, "dut": {}}
    for tok in rest.split(","):
        tok = tok.strip()
        if not tok or "=" not in tok:
            continue
        lhs, _, rhs = tok.partition("=")
        where, _, sig = lhs.partition(".")
        if where == "pod":
            side["pod"][sig] = int(rhs, 0)
        elif where == "dut":
            side["dut"][sig] = _parse_pin(rhs)
    return iface, {k: v for k, v in side.items() if v}


def _print_dut_fields(declared, pad):
    """Print a declared DUT block's fields (identity, usb, repl, wiring)."""
    for k in ("label", "target_family", "board", "flash_base", "flash_size",
              "notes"):
        if declared.get(k) is not None:
            v = declared[k]
            v = hex(v) if isinstance(v, int) else v
            print(f"{pad}{k}: {v}")
    usb = declared.get("usb")
    if usb:
        conn = f" ({usb['connection']})" if usb.get("connection") else ""
        print(f"{pad}usb: {usb.get('vid', '?')}:{usb.get('pid', '?')}{conn}")
    repl = declared.get("repl")
    if repl:
        port = f":{repl['port']}" if repl.get("port") else ""
        print(f"{pad}repl: {repl.get('transport', '?')}{port}")
    wiring = declared.get("wiring")
    if wiring:
        print(f"{pad}wiring:")
        for iface, m in wiring.items():
            pod_s = " ".join(f"{s}=GP{p}" for s, p in m.get("pod", {}).items())
            dut_s = " ".join(
                f"{s}=Pin({d['pin']})" + (f"/{d['label']}" if d.get("label") else "")
                for s, d in m.get("dut", {}).items())
            print(f"{pad}  {iface}: pod[{pod_s}]  dut[{dut_s}]")
    exp = declared.get("expected")
    if exp:
        print(f"{pad}expected: " + "  ".join(
            f"{k}={hex(v)}" for k, v in exp.items()))


def _dut_block_from_args(args):
    """Build a declared DUT block from --dut-* flags, or None if none given."""
    block = {}
    if getattr(args, "dut_label", None) is not None:
        block["label"] = args.dut_label
    if getattr(args, "dut_family", None) is not None:
        block["target_family"] = args.dut_family
    if getattr(args, "dut_board", None) is not None:
        block["board"] = args.dut_board
    if getattr(args, "dut_flash_base", None) is not None:
        block["flash_base"] = int(args.dut_flash_base, 0) \
            if isinstance(args.dut_flash_base, str) else args.dut_flash_base
    if getattr(args, "dut_flash_size", None) is not None:
        block["flash_size"] = int(args.dut_flash_size, 0) \
            if isinstance(args.dut_flash_size, str) else args.dut_flash_size
    if getattr(args, "dut_notes", None) is not None:
        block["notes"] = args.dut_notes
    if getattr(args, "dut_usb", None):
        block["usb"] = _parse_usb(args.dut_usb)
    if getattr(args, "dut_repl", None):
        block["repl"] = _parse_repl(args.dut_repl)
    if getattr(args, "dut_wire", None):
        wiring = {}
        for spec in args.dut_wire:
            iface, m = _parse_wire(spec)
            if iface:
                wiring[iface] = m
        if wiring:
            block["wiring"] = wiring
    if not block:
        return None
    block["declared_at"] = _now()
    return block


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
        addrs = " ".join(p.addr6 + ([p.addr4] if p.addr4 else [])) or p.address
        host = (p.hostname or p.name) + "  "
        usbip = f"  usbip={p.usbip_port}" if p.usbip_port else ""
        uart = f"  uart={p.uart_port}" if p.uart_port else ""
        gdb = f"  gdb={p.gdb_port}" if p.gdb_port else ""
        carrier = f"  carrier={p.carrier_id}" if p.carrier_id else ""
        mp = f"  mp={p.mp_version}" if p.mp_version else ""
        print(f"  {host}repl={p.repl_port}{usbip}{uart}{gdb}{carrier}{mp}")
        print(f"      addrs: {addrs}")
    return 0


def _caps(entry):
    """Compact [v6+v4] capability flag from an entry's stored handles."""
    have = []
    if entry.get("addr6"):
        have.append("v6")
    if entry.get("addr4"):
        have.append("v4")
    return "[" + "+".join(have) + "]" if have else "[name]"


def cmd_list(args):
    registry = load_registry()
    pods = registry.get("pods", {})
    if not pods:
        return 0
    max_label = max((len(k) for k in pods), default=5)
    max_label = max(max_label, 5)
    for label, entry in sorted(pods.items()):
        host = entry.get("hostname") or entry.get("address", "?")
        repl_port = entry.get("repl_port", "?")
        fp = entry.get("fingerprint")
        fp_s = f"fp={fp[:6]}.." if fp else "fp=-"
        mp = entry.get("mp_version", "")
        dut = entry.get("dut")
        dut_s = ""
        if dut:
            fam = dut.get("target_family") or dut.get("label") or "dut"
            dut_s = f"  dut={fam}"
        desc = f"mp={mp}" if mp else ""
        print(f"  {label:<{max_label}}  {host}:{repl_port}  {_caps(entry)}  "
              f"{fp_s}  {desc}{dut_s}")
    return 0


def cmd_register(args):
    label = args.label

    existing = get_pod(label)
    if existing is not None and not args.force:
        print(f"Label '{label}' already registered. Use --force to overwrite.", file=sys.stderr)
        return 1

    extra = {}
    if args.notes:
        extra["notes"] = args.notes
    dut = _dut_block_from_args(args)
    if dut:
        extra["dut"] = dut

    explicit = bool(args.address or args.addr4 or args.addr6 or args.hostname)
    if not explicit:
        # Discovery-based registration: browse mDNS, match by label/--match,
        # store the stable handles, and read the identity fingerprint.
        from pod import enroll
        try:
            entry = enroll.register_discovered(
                label, match=args.match, timeout=args.timeout,
                probe=not args.no_probe, force=True, extra=extra)
        except (LookupError, ValueError) as e:
            print(str(e) + " Give --hostname/--addr4/--addr6 to register "
                  "manually, or --match to disambiguate.", file=sys.stderr)
            return 1
    else:
        # Manual registration from explicit handle flags. A bare --address is
        # classified into the right handle (IPv4 -> addr4, IPv6 -> addr6, else
        # hostname) so the same flag works for any handle type.
        hostname = args.hostname or None
        addr6 = _split_addr6(args.addr6)
        addr4 = args.addr4
        if args.address:
            import ipaddress
            try:
                ip = ipaddress.ip_address(args.address)
                if ip.version == 6:
                    if args.address not in addr6:
                        addr6.append(args.address)
                elif addr4 is None:
                    addr4 = args.address
            except ValueError:
                hostname = hostname or args.address
        entry = {
            "hostname": hostname,
            "addr6": addr6,
            "addr4": addr4,
            "repl_port": args.repl_port,
            "usbip_port": args.usbip_port,
            "uart_port": args.uart_port,
            "gdb_port": args.gdb_port,
            "carrier_id": args.carrier_id or "",
            "mp_version": args.mp_version or "",
            "last_seen": _now(),
        }
        entry.update(extra)
        # Read the fingerprint so an IPv4/mDNS connect is trusted from session
        # one. Best-effort; registration still succeeds if briefly unreachable.
        from pod.enroll import probe_fingerprint, read_pinmap, carry_over
        if not args.no_probe:
            fp = probe_fingerprint(entry)
            if fp:
                entry["fingerprint"] = fp
            pins = read_pinmap(entry)
            if pins:
                entry["pins"] = pins
        # A --force re-register refreshes the handles but keeps the existing
        # DUT block / notes / fingerprint / pins (see carry_over).
        carry_over(existing, entry)
        set_pod(label, entry)

    disp = entry.get("hostname") or entry.get("addr4") or \
        (entry["addr6"][0] if entry["addr6"] else "?")
    fp = entry.get("fingerprint")
    fp_note = f" (fingerprint {fp[:8]}..)" if fp else " (fingerprint not read)"
    print(f"Registered '{label}' -> {disp}:{entry['repl_port']}{fp_note}.")
    return 0


def cmd_dut(args):
    """Show / set / verify the DUT a pod is wired to."""
    entry = _require_pod(args.label)

    # Setting declared metadata? Merge nested fields additively so declaring one
    # interface's wiring (or a usb/repl field) does not wipe the others.
    declared = dict(entry.get("dut") or {})
    block = _dut_block_from_args(args)
    changed = False
    if block:
        new_wiring = block.pop("wiring", None)
        new_usb = block.pop("usb", None)
        new_repl = block.pop("repl", None)
        declared.update(block)
        if new_usb:
            declared.setdefault("usb", {}).update(new_usb)
        if new_repl:
            declared.setdefault("repl", {}).update(new_repl)
        if new_wiring:
            w = declared.setdefault("wiring", {})
            for iface, m in new_wiring.items():
                cur = w.setdefault(iface, {})
                for side, sigs in m.items():
                    cur.setdefault(side, {}).update(sigs)
        changed = True

    # Live discover (default unless --no-probe), and optionally adopt the read
    # IDs into the declared expected{} block.
    live = None
    if not args.no_probe:
        pod = Pod.from_entry(entry)
        try:
            live = pod.discover_dut()
        except Exception as exc:  # noqa: BLE001 - surfaced in the verdict
            live = {"ok": False, "err": repr(exc)}
        if args.adopt and live and live.get("ok", True):
            expected = {k: live[k] for k in ("dpidr", "ap_idr", "cpuid", "rom_base")
                        if k in live}
            if expected:
                declared["expected"] = expected
                declared.setdefault("declared_at", _now())
                changed = True

    if changed:
        update_pod(args.label, dut=declared)
        entry = get_pod(args.label)

    declared = entry.get("dut")
    result = reconcile_dut(declared, live)
    print(f"DUT verdict: {result['verdict']}")
    if declared:
        print("  declared:")
        _print_dut_fields(declared, "    ")
    if result.get("fields"):
        print("  identity (declared vs live):")
        for name, f in result["fields"].items():
            flag = "ok" if f["match"] else "MISMATCH"
            print(f"    {name}: {hex(f['declared'])} vs {hex(f['live'])}  [{flag}]")
    elif live is not None:
        if live.get("ok", True):
            print("  live identity:")
            for k in ("dpidr", "ap_idr", "cpuid", "rom_base"):
                if k in live:
                    print(f"    {k}: {hex(live[k])}")
        else:
            print(f"  live read failed: {live.get('err')}")
    return 0 if result["verdict"] not in ("MISMATCH",) else 1


def _format_pinmap(pins):
    """Render a pod pinmap dict to compact lines."""
    out = []
    swd = pins.get("swd")
    if swd:
        out.append("  swd: " + " ".join(f"{k}=GP{v}" for k, v in swd.items()))
    if pins.get("nrst") is not None:
        out.append(f"  nrst: GP{pins['nrst']}")
    i2c = pins.get("i2c_target")
    if i2c:
        out.append("  i2c_target: " + " ".join(
            (f"{k}=GP{v}" if k != "bus" else f"bus={v}") for k, v in i2c.items()))
    return out


def cmd_pins(args):
    """Show the pod's own DUT-facing pin assignments (live, or cached)."""
    entry = _require_pod(args.label)
    pins = None
    if not args.cached:
        try:
            pins = Pod.from_entry(entry).pinmap()
        except Exception as exc:  # noqa: BLE001 - fall back to the cached map
            print(f"(live read failed: {exc}; showing cached)", file=sys.stderr)
    if pins is None:
        pins = entry.get("pins")
    if not pins:
        print("No pin map available (register the pod, or use a live read).",
              file=sys.stderr)
        return 1
    print(f"Pod {args.label} DUT-facing pins:")
    for line in _format_pinmap(pins):
        print(line)
    return 0


def cmd_usb(args):
    """List the DUT USB devices the pod exports over USB/IP (live VID:PID)."""
    entry = _require_pod(args.label)
    try:
        devs = Pod.from_entry(entry).usbip_list()
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f"usb list failed: {exc}", file=sys.stderr)
        print("(the pod usbip server must be running - try 'pod attach' first)",
              file=sys.stderr)
        return 1
    if not devs:
        print("No DUT USB devices exported by the pod.")
        return 0
    for d in devs:
        print(f"  {d['busid']}  {d['vid']}:{d['pid']}")
    return 0


def cmd_attach(args):
    """Attach the pod's DUT USB over USB/IP and report the DUT's tty."""
    entry = _require_pod(args.label)
    try:
        dev = Pod.from_entry(entry).usbip_attach(ensure=not args.no_ensure)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f"attach failed: {exc}", file=sys.stderr)
        return 1
    print(f"Attached DUT {dev['vid']}:{dev['pid']} (busid {dev['busid']}).")
    if dev.get("tty"):
        print(f"  DUT REPL: {dev['tty']}")
        print(f"  e.g. mpremote connect {dev['tty']}")
    else:
        print("  (no CDC tty appeared yet; check 'usbip port' / dmesg)")
    return 0


def cmd_detach(args):
    """Detach a USB/IP vhci port, or all ports attached to this pod."""
    entry = _require_pod(args.label)
    try:
        if args.port is not None:
            from pod import usbip as _u
            _u.detach(args.port)
            print(f"Detached vhci port {args.port}.")
        else:
            res = Pod.from_entry(entry).usbip_detach()
            ports = res.get("detached", [])
            print(f"Detached vhci port(s) {ports}." if ports
                  else "No vhci attachment to this pod.")
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f"detach failed: {exc}", file=sys.stderr)
        return 1
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
    print(f"Hostname:   {entry.get('hostname') or '(none)'}")
    for i, a in enumerate(entry.get("addr6") or []):
        print(f"{'IPv6:' if i == 0 else '':<12}{a}")
    if not entry.get("addr6"):
        print(f"IPv6:       (none)")
    print(f"IPv4:       {entry.get('addr4') or '(none)'}")
    print(f"Fingerprint:{entry.get('fingerprint') or '(none)'}")
    print(f"REPL port:  {entry.get('repl_port', '(none)')}")
    print(f"USB/IP port:{entry.get('usbip_port') or '(none)'}")
    print(f"UART port:  {entry.get('uart_port') or '(none)'}")
    print(f"GDB port:   {entry.get('gdb_port') or '(none)'}")
    print(f"Carrier:    {entry.get('carrier_id') or '(none)'}")
    print(f"MP version: {entry.get('mp_version') or '(none)'}")
    print(f"Last seen:  {entry.get('last_seen') or '(unknown)'}")
    pins = entry.get("pins")
    if pins:
        print("Pod pins:")
        for line in _format_pinmap(pins):
            print(line)
    dut = entry.get("dut")
    if dut:
        print("DUT (declared):")
        _print_dut_fields(dut, "  ")
        print("  (run 'pod dut %s' to verify against the live target)" % args.label)
    notes = entry.get("notes")
    if notes:
        print("Notes:")
        for line in notes.splitlines():
            print(f"  {line}")
    return 0


def cmd_repl_raw(args):
    """Full raw-terminal REPL passthrough (`pod repl --raw`): arrow keys,
    history, paste mode. Blocks until you disconnect."""
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    pod.repl()
    return 0


def cmd_mount(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    pod.mount(args.directory)
    return 0


def cmd_exec(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    try:
        output = pod.exec(args.code)
    except PodExecError as exc:
        print(str(exc), file=sys.stderr)
        if exc.stderr:
            print("--- pod stderr ---", file=sys.stderr)
            print(exc.stderr, file=sys.stderr)
        return 1
    if output:
        print(output, end="")
    return 0


def cmd_dut_exec(args):
    """Run MicroPython on the DUT (turnkey: ensure link + attach + mpremote)."""
    entry = _require_pod(args.label)
    try:
        res = Pod.from_entry(entry).dut_exec(args.code)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f"dut-exec failed: {exc}", file=sys.stderr)
        return 1
    if res.get("stdout"):
        print(res["stdout"], end="")
    if res.get("returncode"):
        if res.get("stderr"):
            print(res["stderr"], file=sys.stderr)
        return 1
    return 0


def cmd_cp(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    pod.cp(args.src, args.dst)
    return 0


def cmd_flash(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    addr = int(args.addr, 0) if isinstance(args.addr, str) else args.addr
    result = pod.flash_dut(args.image, target=args.target, addr=addr,
                           keep_attached=args.keep_attached,
                           mass_erase=args.mass_erase)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_erase(args):
    """Erase the entire DUT flash via the on-pod debug stack."""
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    result = pod.erase_dut()
    print(result)
    return 0 if result.get("ok") else 1


def cmd_reset(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    result = pod.reset_dut(mode=args.mode, keep_attached=args.keep_attached)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_gdb(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
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


def cmd_halt(args):
    """Halt the DUT core over SWD (hold it; no auto-resume)."""
    pod = Pod.from_entry(_require_pod(args.label))
    result = pod.halt_dut(keep_attached=args.keep_attached)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_resume(args):
    """Resume the DUT core over SWD."""
    pod = Pod.from_entry(_require_pod(args.label))
    result = pod.resume_dut()
    print(result)
    return 0 if result.get("ok") else 1


def cmd_read_reg(args):
    """Read one DUT core register over SWD (core must be halted)."""
    pod = Pod.from_entry(_require_pod(args.label))
    try:
        result = pod.read_reg(args.reg)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if result.get("ok"):
        print("reg %s = 0x%08x" % (args.reg, result["value"]))
    else:
        print(result)
    return 0 if result.get("ok") else 1


def cmd_write_reg(args):
    """Write one DUT core register over SWD (core must be halted)."""
    pod = Pod.from_entry(_require_pod(args.label))
    try:
        result = pod.write_reg(args.reg, int(args.value, 0))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(result)
    return 0 if result.get("ok") else 1


def cmd_read_mem(args):
    """Read DUT memory over SWD and print it as hex (<= 4096 bytes)."""
    pod = Pod.from_entry(_require_pod(args.label))
    try:
        result = pod.read_mem(int(args.addr, 0), int(args.length, 0))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if result.get("ok"):
        print("0x%08x: %s" % (result["addr"], result["hex"]))
    else:
        print(result)
    return 0 if result.get("ok") else 1


def cmd_write_mem(args):
    """Write DUT memory over SWD (RAM/peripherals only; flash/code region refused)."""
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    result = pod.write_mem(int(args.addr, 0), args.data,
                           protect=dut_protect_ranges(entry))
    print(result)
    return 0 if result.get("ok") else 1


def cmd_repl(args):
    """`pod repl`: streaming session by default, or `--raw` for a raw terminal."""
    if getattr(args, "raw", False):
        if args.mount or args.exec or args.cp or args.soft_reset:
            print("pod repl --raw is a one-shot passthrough and cannot chain "
                  "--mount/--exec/--cp/--soft-reset; drop --raw for those, or "
                  "use `ampremote connect <t> mount <d> repl` for a raw "
                  "terminal with a mount.", file=sys.stderr)
            return 1
        return cmd_repl_raw(args)
    return cmd_repl_stream(args)


def cmd_repl_stream(args):
    """Persistent streaming REPL: tee the target's stdout to console + file,
    send typed lines to its stdin. Optionally chain mount/exec/cp/soft-reset
    setup first. Ctrl-C interrupts the target; EOF (Ctrl-D) exits, leaving the
    target running. For a full raw terminal use `pod repl --raw`."""
    pod = Pod.from_entry(_require_pod(args.label))

    def _echo(data):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    pre_cp = [tuple(pair) for pair in (args.cp or [])]
    try:
        sess = pod.open_session(
            log_path=args.log, device=args.device, on_output=_echo,
            mount=args.mount, pre_exec=args.exec or None, pre_cp=pre_cp or None,
            soft_reset=args.soft_reset, unsafe_links=args.unsafe_links,
            reconnect=not args.no_reconnect)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f"repl: connect failed: {exc}", file=sys.stderr)
        return 1
    extras = []
    if args.log:
        extras.append(f"log={args.log}")
    if args.mount:
        extras.append(f"mount={args.mount}")
    note = ("  " + "  ".join(extras)) if extras else ""
    print(f"[pod repl: {sess.target}{note}  |  Ctrl-C interrupts the target, "
          f"Ctrl-D / EOF exits]", file=sys.stderr)
    try:
        while True:
            try:
                line = sys.stdin.readline()
            except KeyboardInterrupt:
                sess.interrupt()
                continue
            if not line:                 # EOF -> leave the target running
                break
            try:
                sess.send(line.rstrip("\n"))
            except ConnectionError as exc:
                print(f"\n[pod repl: {exc}; line not sent]", file=sys.stderr)
    finally:
        sess.close()
    return 0


def cmd_i2c_target(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    regs = [int(x, 0) for x in args.regs] if args.regs else None
    result = pod.i2c_target(addr=int(args.addr, 0), regs=regs, bus=args.bus,
                            scl=args.scl, sda=args.sda, name=args.name)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_i2c_regs(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    write = [int(x, 0) for x in args.write] if args.write else None
    result = pod.i2c_target_regs(off=args.off, length=args.length, write=write,
                                 name=args.name)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_gpio(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    value = None if args.value is None else int(args.value, 0)
    result = pod.gpio(args.pin, value=value, pull=args.pull)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_adc(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    result = pod.adc(args.pin)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_release(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    result = pod.peripheral_release(name=args.name)
    print(result)
    return 0 if result.get("ok") else 1


def _parse_pins(spec):
    """'16' -> (16, 1); '16-23' -> (16, 8)  (base, width)."""
    if "-" in spec:
        a, b = spec.split("-", 1)
        a, b = int(a, 0), int(b, 0)
        return a, b - a + 1
    return int(spec, 0), 1


def cmd_la(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    base, width = _parse_pins(args.pins)
    trigger = None
    if args.trigger:
        pin_s, cond = args.trigger.split(":", 1)
        trigger = (int(pin_s, 0), cond)
    names = args.names.split(",") if args.names else None
    result = pod.logic_analyse(
        base_pin=base, width=width, rate=int(float(args.rate)), depth=args.depth,
        trigger=trigger, out_path=args.out, sm_id=args.sm_id, names=names)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_uart(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    port = args.port or entry.get("uart_port") or 2000
    try:
        pod.uart_stream(port=port, duration=args.duration,
                        interactive=args.interactive,
                        out_path=args.out)
    except KeyboardInterrupt:
        pass
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
    p = sub.add_parser(
        "register", help="Register a pod under a label",
        description="With no address flags, browse mDNS and register the "
                    "discovered stable handles (hostname + IPv6 + IPv4). Give "
                    "--hostname/--addr4/--addr6 (or a bare --address) to "
                    "register manually.")
    p.add_argument("label", help="Short name for the pod (e.g. my-pod)")
    p.add_argument("--address", default=None, metavar="HANDLE",
                   help="A single handle (IPv4, IPv6, or hostname); classified "
                        "automatically. Optional - omit to register via mDNS.")
    p.add_argument("--hostname", default=None, metavar="NAME",
                   help="mDNS hostname, e.g. annealage-pod.local")
    p.add_argument("--addr4", default=None, metavar="IP",
                   help="IPv4 address")
    p.add_argument("--addr6", action="append", metavar="IP6",
                   help="IPv6 address (repeatable or comma-separated)")
    p.add_argument("--match", default=None, metavar="NAME",
                   help="Disambiguate the mDNS match (hostname/instance)")
    p.add_argument("--timeout", type=float, default=5.0, metavar="SECS",
                   help="mDNS browse duration when discovering (default: 5)")
    p.add_argument("--no-probe", action="store_true", dest="no_probe",
                   help="Do not read the identity fingerprint at register time")
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
    _add_dut_flags(p)
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing label")

    # unregister
    p = sub.add_parser("unregister", help="Remove a pod from the registry")
    p.add_argument("label")

    # dut
    p = sub.add_parser(
        "dut", help="Show / set / verify the DUT a pod is wired to",
        description="With no flags, probe the live DUT over SWD and reconcile "
                    "it against the declared block. --dut-* set declared "
                    "metadata; --adopt snapshots the live IDs into expected{}.")
    p.add_argument("label")
    p.add_argument("--no-probe", action="store_true", dest="no_probe",
                   help="Do not read the live DUT (registry-only)")
    p.add_argument("--adopt", action="store_true",
                   help="Snapshot the live DUT IDs into the declared expected{}")
    _add_dut_flags(p)

    # info
    p = sub.add_parser("info", help="Show full details for a registered pod")
    p.add_argument("label")

    # pins
    p = sub.add_parser("pins",
                       help="Show the pod's own DUT-facing pin assignments")
    p.add_argument("label")
    p.add_argument("--cached", action="store_true",
                   help="Show the cached map instead of a live read")

    # usb
    p = sub.add_parser("usb",
                       help="List the DUT USB devices the pod exports (live VID:PID)")
    p.add_argument("label")

    # attach
    p = sub.add_parser(
        "attach", help="Attach the pod's DUT USB over USB/IP; prints the DUT tty",
        description="Brings the pod USB host + usbip server up (unless "
                    "--no-ensure), attaches the exported device on this host "
                    "(needs passwordless sudo for usbip), and reports the DUT's "
                    "CDC tty. NB: activating the pod USB host can disturb Wi-Fi.")
    p.add_argument("label")
    p.add_argument("--no-ensure", action="store_true", dest="no_ensure",
                   help="Do not (re)start the pod USB host + usbip server first")

    # detach
    p = sub.add_parser("detach",
                       help="Detach a USB/IP vhci port, or all ports for this pod")
    p.add_argument("label")
    p.add_argument("--port", type=int, default=None,
                   help="vhci port from 'usbip port' (omit to detach all for this pod)")

    # repl (persistent streaming session by default; --raw for a raw terminal)
    p = sub.add_parser(
        "repl",
        help="Persistent REPL: stream target stdout to console+file, type lines to its stdin",
        description="Connect to the target (the pod's socket REPL by default), "
                    "stream its stdout to the console and optionally a log "
                    "file, and send typed lines to its stdin. Chain setup "
                    "before connecting with --mount/--exec/--cp/--soft-reset. "
                    "Ctrl-C interrupts the target; Ctrl-D / EOF exits, leaving "
                    "it running. --raw gives a full raw terminal instead "
                    "(arrow keys, history, paste) but cannot chain setup.")
    p.add_argument("label")
    p.add_argument("--raw", action="store_true",
                   help="Full raw-terminal passthrough instead of the streaming session")
    p.add_argument("--log", default=None, metavar="FILE",
                   help="Append all received output to this file")
    p.add_argument("--device", default=None, metavar="DEV",
                   help="Attach this mpremote device instead of the pod's "
                        "socket REPL (e.g. a DUT CDC tty)")
    p.add_argument("--mount", default=None, metavar="DIR",
                   help="Mount a host dir on the target for the session "
                        "(stays mounted until exit)")
    p.add_argument("--exec", action="append", metavar="CODE", dest="exec",
                   help="Run setup code on the target before connecting (repeatable)")
    p.add_argument("--cp", action="append", nargs=2, metavar=("SRC", "DST"),
                   help="Copy a file to/from the target before connecting "
                        "(':path' = target side; repeatable)")
    p.add_argument("--soft-reset", action="store_true", dest="soft_reset",
                   help="Soft-reset the target before connecting")
    p.add_argument("--unsafe-links", action="store_true", dest="unsafe_links",
                   help="With --mount, follow symlinks pointing outside the mount root")
    p.add_argument("--no-reconnect", action="store_true", dest="no_reconnect",
                   help="Do not auto-reconnect after a dropped link (default: reconnect)")

    # mount
    p = sub.add_parser("mount", help="Mount a local directory on the pod")
    p.add_argument("label")
    p.add_argument("directory", metavar="dir", help="Local directory to mount")

    # exec (pod-side)
    p = sub.add_parser("exec", help="Execute MicroPython code on the POD itself")
    p.add_argument("label")
    p.add_argument("code", help="MicroPython code string to run on the pod")

    # dut-exec (turnkey DUT, over USB/IP)
    p = sub.add_parser("dut-exec",
                       help="Run MicroPython on the DUT (ensure link + attach + mpremote)")
    p.add_argument("label")
    p.add_argument("code", help="MicroPython code string to run on the DUT")

    # cp
    p = sub.add_parser("cp", help="Copy a file to or from the pod")
    p.add_argument("label")
    p.add_argument("src", help="Source path (use ':path' for pod-side)")
    p.add_argument("dst", help="Destination path (use ':path' for pod-side)")

    # flash
    p = sub.add_parser("flash", help="Flash a DUT firmware image via the pod")
    p.add_argument("label")
    p.add_argument("image", help="Firmware image path (raw binary or ELF)")
    p.add_argument("--addr", default="0",
                   help="Flash base address (default: 0; ignored for ELF)")
    p.add_argument("--target", default=None, help="Target MCU identifier")
    p.add_argument("--keep-attached", action="store_true", dest="keep_attached",
                   help="Do not detach a live USB/IP session first (risks a wedge)")
    p.add_argument("--mass-erase", action="store_true", dest="mass_erase",
                   help="Erase the entire DUT flash before programming")

    # erase
    p = sub.add_parser("erase", help="Erase the entire DUT flash via the pod")
    p.add_argument("label")

    # reset
    p = sub.add_parser("reset", help="Reset the DUT via the pod")
    p.add_argument("label")
    p.add_argument("--mode", default="sysreset", choices=["sysreset", "halt"],
                   help="Reset method (default: sysreset)")
    p.add_argument("--keep-attached", action="store_true", dest="keep_attached",
                   help="Do not detach a live USB/IP session first (risks a wedge)")

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

    # halt / resume / read-reg / write-reg / read-mem / write-mem
    # SWD debug-interface peek-poke. Registers need a halted core (halt first).
    p = sub.add_parser("halt",
                       help="Halt the DUT core over SWD (hold; no auto-resume)")
    p.add_argument("label")
    p.add_argument("--keep-attached", action="store_true", dest="keep_attached",
                   help="Do not detach a live USB/IP session first (risks a wedge)")

    p = sub.add_parser("resume", help="Resume the DUT core over SWD")
    p.add_argument("label")

    p = sub.add_parser("read-reg",
                       help="Read a DUT core register over SWD (core halted)")
    p.add_argument("label")
    p.add_argument("reg", help="regsel 0..18 or name (r0..r12, sp, lr, pc, "
                                "xpsr, msp, psp)")

    p = sub.add_parser("write-reg",
                       help="Write a DUT core register over SWD (core halted)")
    p.add_argument("label")
    p.add_argument("reg", help="regsel 0..18 or name")
    p.add_argument("value", help="32-bit value (e.g. 0x20004000)")

    p = sub.add_parser("read-mem",
                       help="Read DUT memory over SWD, print hex (<= 4096 bytes)")
    p.add_argument("label")
    p.add_argument("addr", help="Source address (e.g. 0x20000000)")
    p.add_argument("length", help="Bytes to read, 1..4096 (decimal or 0x..)")

    p = sub.add_parser("write-mem",
                       help="Write DUT memory over SWD (RAM/peripherals only)")
    p.add_argument("label")
    p.add_argument("addr", help="Destination address (>= 0x20000000)")
    p.add_argument("data", help="Bytes as a hex string (e.g. deadbeef)")

    # i2c-target
    p = sub.add_parser("i2c-target",
                       help="Bring up a hardware I2C target (register file) on the pod")
    p.add_argument("label")
    p.add_argument("--addr", default="0x42", help="7-bit I2C address (default: 0x42)")
    p.add_argument("--regs", nargs="*", metavar="BYTE",
                   help="Initial register bytes from offset 0 (e.g. 0xAB 0xCD)")
    p.add_argument("--bus", type=int, default=1, help="Hardware I2C bus id (default: 1)")
    p.add_argument("--scl", type=int, default=11, help="SCL GPIO (default: 11)")
    p.add_argument("--sda", type=int, default=10, help="SDA GPIO (default: 10)")
    p.add_argument("--name", default="i2c_target", help="Instance name")

    # i2c-regs
    p = sub.add_parser("i2c-regs",
                       help="Read/write the pod I2C target register file")
    p.add_argument("label")
    p.add_argument("--off", type=int, default=0, help="Register offset (default: 0)")
    p.add_argument("--length", type=int, default=None,
                   help="Bytes to read (default: to end)")
    p.add_argument("--write", nargs="*", metavar="BYTE",
                   help="Bytes to write at off first")
    p.add_argument("--name", default="i2c_target", help="Instance name")

    # gpio
    p = sub.add_parser("gpio", help="Read or drive a pod GPIO")
    p.add_argument("label")
    p.add_argument("pin", type=int, help="GPIO number")
    p.add_argument("--value", default=None, help="0/1 to drive; omit to read")
    p.add_argument("--pull", default=None, choices=["up", "down"],
                   help="Input pull (read only)")

    # adc
    p = sub.add_parser("adc", help="Sample a pod ADC channel")
    p.add_argument("label")
    p.add_argument("pin", type=int, help="ADC-capable GPIO number")

    # release
    p = sub.add_parser("release", help="Release pod peripheral instance(s)")
    p.add_argument("label")
    p.add_argument("--name", default="*", help="Instance name or '*' for all")

    # la (logic analyser)
    p = sub.add_parser("la", help="Logic-analyse DUT pins (capture -> VCD)")
    p.add_argument("label")
    p.add_argument("--pins", default="16",
                   help="Pin or range: 16 (1 ch) or 16-23 (base-last)")
    p.add_argument("--rate", default="1e6", help="Sample rate Hz (default: 1e6)")
    p.add_argument("--depth", type=int, default=8000,
                   help="Samples to capture (default: 8000)")
    p.add_argument("--trigger", default=None,
                   help="pin:cond, cond in rise/fall/high/low (default: immediate)")
    p.add_argument("--out", default="capture.vcd", help="Output VCD path")
    p.add_argument("--names", default=None,
                   help="Comma-separated channel names (low pin first)")
    p.add_argument("--sm-id", type=int, default=0, dest="sm_id",
                   help="PIO0 state machine id (default: 0; PIO2 is CYW43 Wi-Fi)")

    # uart (DUT UART bridge)
    p = sub.add_parser("uart", help="Stream DUT UART output over TCP")
    p.add_argument("label")
    p.add_argument("--port", type=int, default=None,
                   help="Pod UART TCP port (default: registry uart_port or 2000)")
    p.add_argument("--duration", type=float, default=None,
                   help="Seconds to run (default: until Ctrl-C)")
    p.add_argument("--tx", "--interactive", action="store_true", dest="interactive",
                   help="Forward stdin to the DUT UART (full-duplex)")
    p.add_argument("--out", default=None, help="Write output to a file instead of stdout")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    handler = {
        "discover": cmd_discover,
        "list": cmd_list,
        "register": cmd_register,
        "unregister": cmd_unregister,
        "dut": cmd_dut,
        "info": cmd_info,
        "pins": cmd_pins,
        "usb": cmd_usb,
        "attach": cmd_attach,
        "detach": cmd_detach,
        "repl": cmd_repl,
        "mount": cmd_mount,
        "exec": cmd_exec,
        "dut-exec": cmd_dut_exec,
        "cp": cmd_cp,
        "flash": cmd_flash,
        "erase": cmd_erase,
        "reset": cmd_reset,
        "gdb": cmd_gdb,
        "halt": cmd_halt,
        "resume": cmd_resume,
        "read-reg": cmd_read_reg,
        "write-reg": cmd_write_reg,
        "read-mem": cmd_read_mem,
        "write-mem": cmd_write_mem,
        "i2c-target": cmd_i2c_target,
        "i2c-regs": cmd_i2c_regs,
        "gpio": cmd_gpio,
        "adc": cmd_adc,
        "release": cmd_release,
        "la": cmd_la,
        "uart": cmd_uart,
    }[args.command]

    return handler(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
