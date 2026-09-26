"""pod CLI: registry and control for Annealage Pods over Wi-Fi.

Thin frontend over the pod core library. Mirrors the mpy-dev CLI conventions.

Command tree:
  discover|list|register|unregister|info|exec|mount|cp|pins|flm|install-udev
  |open|open-raw
      flat pod verbs: find, enroll, inspect, and drive the pod itself. open
      matches the MCP pod_open tool: a persistent streaming session on the
      pod's own socket REPL; open-raw is the operator-only raw-terminal form.
  dut open|exec|flash|erase|reset|reg|mem|halt|resume|gdb|identify|link
      the device under test, by every route: its REPL (session and
      one-shot), its debug port (SWD), its flash, and the USB/IP link that
      carries it.
  bench gpio|adc|la|device|device-regs|uart
      the pod's instruments pointed at the DUT: GPIO, ADC, logic analyser,
      the I2C/SPI device personalities, the UART tap.

Registry: $POD_CONFIG_DIR/pods.json (default: ~/.config/pod/pods.json)
"""

import argparse
import sys
from datetime import datetime, timezone

from pod.registry import (
    load_registry,
    get_pod,
    set_pod,
    update_pod,
    remove_pod,
    reconcile_dut,
    dut_protect_ranges,
)
from pod.client import Pod, PodExecError, PodConflictError


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


def _add_stream_flags(p, subject):
    """Add the shared streaming-session flags to an `open` / `dut open` subparser.

    `subject` ("pod" or "DUT") fills the help text so the two subparsers read
    naturally without duplicating the flag definitions.
    """
    p.add_argument("--log", default=None, metavar="FILE",
                   help="Append all received output to this file")
    p.add_argument("--mount", default=None, metavar="DIR",
                   help="Mount a host dir on the %s for the session "
                        "(stays mounted until exit)" % subject)
    p.add_argument("--exec", action="append", metavar="CODE", dest="exec",
                   help="Run setup code on the %s before connecting (repeatable)"
                        % subject)
    p.add_argument("--cp", action="append", nargs=2, metavar=("SRC", "DST"),
                   help="Copy a file to/from the %s before connecting "
                        "(':path' = %s side; repeatable)" % (subject, subject))
    p.add_argument("--soft-reset", action="store_true", dest="soft_reset",
                   help="Soft-reset the %s before connecting" % subject)
    p.add_argument("--unsafe-links", action="store_true", dest="unsafe_links",
                   help="With --mount, follow symlinks pointing outside the mount root")
    p.add_argument("--no-reconnect", action="store_true", dest="no_reconnect",
                   help="Do not auto-reconnect after a dropped link (default: reconnect)")


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


# ── pod verb handlers (flat) ────────────────────────────────────────────


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


def cmd_unregister(args):
    removed = remove_pod(args.label)
    if not removed:
        print(f"Label '{args.label}' not found.", file=sys.stderr)
        return 1
    print(f"Unregistered '{args.label}'.")
    return 0


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
    spi = pins.get("spi_target")
    if spi:
        out.append("  spi_target: " + " ".join(f"{k}=GP{v}" for k, v in spi.items()))
    return out


def cmd_info(args):
    entry = _require_pod(args.label)
    print(f"Label:      {args.label}")
    print(f"Hostname:   {entry.get('hostname') or '(none)'}")
    for i, a in enumerate(entry.get("addr6") or []):
        print(f"{'IPv6:' if i == 0 else '':<12}{a}")
    if not entry.get("addr6"):
        print("IPv6:       (none)")
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
        print("  (run 'pod dut identify %s' to verify against the live target)"
              % args.label)
    notes = entry.get("notes")
    if notes:
        print("Notes:")
        for line in notes.splitlines():
            print(f"  {line}")
    return 0


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


def cmd_mount(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    pod.mount(args.directory)
    return 0


def cmd_exec(args):
    """Execute MicroPython code on the pod itself (not the DUT)."""
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


def cmd_cp(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    pod.cp(args.src, args.dst)
    return 0


def cmd_flm(args):
    """Report or install the DUT's generic CMSIS flash algorithm.

    With no options, prints what the pod currently has installed. Given any of
    --device / --pack / --download / --force, resolves the algorithm from the
    target's CMSIS pack and installs it, then prints the result. Flashing with
    Flashing/erasing installs one automatically; this command is for pointing
    at a specific pack, forcing a refresh, or checking what is loaded.
    """
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)

    installing = any((args.device, args.pack, args.download, args.force))
    if not installing:
        print(pod.flm_algo_info())
        return 0

    kwargs = {}
    if args.pack:
        kwargs["pack"] = args.pack
    if args.download:
        kwargs["allow_download"] = True
        if args.vendor:
            kwargs["vendor"] = args.vendor
        if args.pack_name:
            kwargs["pack_name"] = args.pack_name
    algo = pod.resolve_flm_algo(device=args.device, **kwargs)
    print(pod.install_flm_algo(algo))
    return 0


_UDEV_RULE_PATH = "/etc/udev/rules.d/99-annealage-pod.rules"


def _udev_rule_text(vids):
    """The ModemManager-ignore udev rule for pod-forwarded DUTs.

    Scope: devices imported over USB/IP are bound under the vhci_hcd virtual host
    controller, so matching that ancestor restricts the rule to pod-forwarded
    DUTs and leaves the host's own USB devices untouched. Extra idVendor matches
    (--vid) cover setups where the vhci match needs a belt-and-braces fallback.
    """
    lines = [
        "# Annealage Pod: stop ModemManager probing USB/IP-forwarded DUTs.",
        "# MM opens a DUT's CDC-ACM tty and toggles DTR, which gates a MicroPython",
        "# REPL's stdout (tud_cdc_connected()) so the DUT goes silent. Scoped to",
        "# devices imported over USB/IP (bound under vhci_hcd) so only pod-forwarded",
        "# DUTs are affected, not the host's own USB devices.",
        'ACTION=="add|change", SUBSYSTEM=="usb", DRIVERS=="vhci_hcd", '
        'ENV{ID_MM_DEVICE_IGNORE}="1"',
    ]
    for vid in vids:
        lines.append('# Belt-and-braces: ignore MM for idVendor %s on any transport.'
                     % vid)
        lines.append('ACTION=="add|change", SUBSYSTEM=="usb", ATTR{idVendor}=="%s", '
                     'ENV{ID_MM_DEVICE_IGNORE}="1"' % vid.lower())
    return "\n".join(lines) + "\n"


def cmd_install_udev(args):
    import subprocess as _sp
    text = _udev_rule_text(args.vid or [])
    if args.print_only:
        print(text, end="")
        return 0
    path = args.path
    try:
        with open(path, "w") as f:
            f.write(text)
    except PermissionError:
        sys.stderr.write(
            "pod: need root to write %s. Re-run with sudo, or install manually:\n\n"
            "  sudo tee %s <<'EOF'\n%sEOF\n"
            "  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
            % (path, path, text))
        return 1
    ok = True
    for cmd in (["udevadm", "control", "--reload-rules"], ["udevadm", "trigger"]):
        try:
            _sp.run(cmd, check=True)
        except Exception as exc:  # noqa: BLE001 - reload needs root / udevadm
            sys.stderr.write("pod: wrote %s but '%s' failed (%r); run it manually "
                             "(needs root).\n" % (path, " ".join(cmd), exc))
            ok = False
    print("pod: installed ModemManager-ignore udev rule at %s" % path)
    print("     re-attach the DUT so the rule applies at enumeration.")
    return 0 if ok else 1


def _stream_session(pod, args, device=None):
    """Open a persistent streaming session and pump stdin to it until EOF.

    Tees the session's output to the console and, if --log was given, to a
    file. Ctrl-C typed on stdin interrupts the target; EOF (Ctrl-D) exits,
    leaving the target running.
    """
    def _echo(data):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    pre_cp = [tuple(pair) for pair in (args.cp or [])]
    try:
        sess = pod.open_session(
            log_path=args.log, device=device, on_output=_echo,
            mount=args.mount, pre_exec=args.exec or None, pre_cp=pre_cp or None,
            soft_reset=args.soft_reset, unsafe_links=args.unsafe_links,
            reconnect=not args.no_reconnect)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f"connect failed: {exc}", file=sys.stderr)
        return 1
    extras = []
    if args.log:
        extras.append(f"log={args.log}")
    if args.mount:
        extras.append(f"mount={args.mount}")
    note = ("  " + "  ".join(extras)) if extras else ""
    print(f"[{sess.target}{note}  |  Ctrl-C interrupts the target, "
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
                print(f"\n[{exc}; line not sent]", file=sys.stderr)
    finally:
        sess.close()
    return 0


def cmd_repl(args):
    """Persistent streaming session on the pod's own socket REPL (`pod open`,
    matching the MCP pod_open tool).

    Tees the pod's stdout to the console (and a log file with --log), and
    sends typed stdin lines to it. Ctrl-C interrupts the target; EOF
    (Ctrl-D) exits, leaving it running. Setup chains before connecting via
    --mount/--exec/--cp/--soft-reset. For a full raw terminal (arrow keys,
    history, paste) use `pod open-raw` instead.
    """
    pod = Pod.from_entry(_require_pod(args.label))
    return _stream_session(pod, args)


def cmd_repl_raw(args):
    """Full raw-terminal passthrough to the pod's own socket REPL (`pod
    open-raw`), for interactive operator use only - it has no MCP
    counterpart.

    Arrow keys, history, and paste mode all work; blocks until you
    disconnect. Cannot chain --mount/--exec/--cp/--soft-reset setup - use
    `pod open` for that.
    """
    pod = Pod.from_entry(_require_pod(args.label))
    pod.repl()
    return 0


# ── dut verb handlers ────────────────────────────────────────────────────


def cmd_dut_open(args):
    """Persistent streaming session on the DUT's CDC tty.

    Tees the DUT's stdout to the console (and a log file with --log), and
    sends typed stdin lines to it. Ctrl-C interrupts the target; EOF
    (Ctrl-D) exits, leaving it running. Setup chains before connecting via
    --mount/--exec/--cp/--soft-reset. --recover sends Ctrl-C then Ctrl-B
    over the tty first, to un-stick a DUT latched in raw REPL mode, and
    prints the machine-readable result (ok, recovered, prompt_seen, was_raw).

    With no device the USB/IP link is brought up and the tty it returns is
    used, so one command is enough; that activates the pod USB host, which can
    disturb the pod's Wi-Fi.
    """
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    # Blank is absent, not a device: the session layer falls back to the pod's
    # own socket REPL when given no device, so an empty argument would quietly
    # open a POD session from a command line that says dut.
    device = (args.device or "").strip() or None
    if device is None:
        try:
            device = pod.dut_tty()
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            print("dut open: %s" % exc, file=sys.stderr)
            return 1
        print("dut open: using DUT tty %s" % device)
    if args.recover:
        rec = pod.recover_dut_repl(device, settle=args.settle,
                                   read_wait=args.read_wait)
        print(rec)
    return _stream_session(pod, args, device=device)


def cmd_dut_exec(args):
    """Run MicroPython on the DUT (ensures the USB/IP link, attaches, runs
    over mpremote, each call from scratch). Prefer `pod dut open` for more
    than a single call.
    """
    entry = _require_pod(args.label)
    try:
        res = Pod.from_entry(entry).dut_exec(args.code)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f"dut exec failed: {exc}", file=sys.stderr)
        return 1
    if res.get("stdout"):
        print(res["stdout"], end="")
    if res.get("returncode"):
        if res.get("stderr"):
            print(res["stderr"], file=sys.stderr)
        return 1
    return 0


def cmd_dut_flash(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    addr = int(args.addr, 0) if isinstance(args.addr, str) else args.addr
    try:
        result = pod.flash_dut(args.image, target=args.target, addr=addr,
                               keep_attached=args.keep_attached,
                               mass_erase=args.mass_erase,
                               force=args.force)
    except PodConflictError as exc:
        print(f"pod dut flash: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0 if result.get("ok") else 1


def cmd_dut_erase(args):
    """Erase the entire DUT flash via the on-pod debug stack."""
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    try:
        result = pod.erase_dut(keep_attached=args.keep_attached, force=args.force)
    except PodConflictError as exc:
        print(f"pod dut erase: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0 if result.get("ok") else 1


def cmd_dut_reset(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    try:
        result = pod.reset_dut(mode=args.mode, keep_attached=args.keep_attached,
                               force=args.force)
    except PodConflictError as exc:
        print(f"pod dut reset: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0 if result.get("ok") else 1


def cmd_dut_reg(args):
    """Read or write one DUT core register over SWD (core must be halted).

    Prints the current value when `value` is omitted; writes it when given.
    """
    pod = Pod.from_entry(_require_pod(args.label))
    try:
        if args.value is None:
            result = pod.read_reg(args.reg)
        else:
            result = pod.write_reg(args.reg, int(args.value, 0))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.value is None and result.get("ok"):
        print("reg %s = 0x%08x" % (args.reg, result["value"]))
    else:
        print(result)
    return 0 if result.get("ok") else 1


def cmd_dut_mem(args):
    """Read or write DUT memory over SWD.

    --data given writes those hex bytes (RAM/peripherals only, <= 4096
    bytes; flash/code region refused). Otherwise reads `length` bytes from
    `addr`: printed inline as hex by default (<= 4096 bytes), or streamed
    straight from pod RAM to the host file at --out with no size cap.
    """
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    addr = int(args.addr, 0)

    if args.data is not None and args.out is not None:
        print("pod dut mem: --data (write) and --out (read destination) are "
              "mutually exclusive - pass one or the other", file=sys.stderr)
        return 1

    if args.data is not None:
        result = pod.write_mem(addr, args.data, protect=dut_protect_ranges(entry))
        print(result)
        return 0 if result.get("ok") else 1

    if args.length is None:
        print("pod dut mem: length is required for a read", file=sys.stderr)
        return 1
    length = int(args.length, 0)

    if args.out:
        path = pod.read_dut(addr, length, args.out)
        print(f"Wrote {length} bytes from 0x{addr:08x} to {path}.")
        return 0

    try:
        result = pod.read_mem(addr, length)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if result.get("ok"):
        print("0x%08x: %s" % (result["addr"], result["hex"]))
    else:
        print(result)
    return 0 if result.get("ok") else 1


def cmd_dut_halt(args):
    """Halt the DUT core over SWD (hold it; no auto-resume)."""
    pod = Pod.from_entry(_require_pod(args.label))
    try:
        result = pod.halt_dut(keep_attached=args.keep_attached, force=args.force)
    except PodConflictError as exc:
        print(f"pod dut halt: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0 if result.get("ok") else 1


def cmd_dut_resume(args):
    """Resume the DUT core over SWD."""
    pod = Pod.from_entry(_require_pod(args.label))
    result = pod.resume_dut()
    print(result)
    return 0 if result.get("ok") else 1


def cmd_dut_gdb(args):
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


def cmd_dut_identify(args):
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


def cmd_dut_link(args):
    """Bring up, inspect, or tear down the pod's USB/IP export of the DUT.

    status (default): list the DUT USB devices the pod currently exports.
    up: bring the pod USB host + usbip server up (unless --no-ensure) and
        attach the exported device on this host; prints the DUT's CDC tty.
    down: detach a vhci port (--port), or every port attached to this pod.
    reprobe: re-seed a stale or empty export without a cold power cycle.
    """
    entry = _require_pod(args.label)
    action = args.action

    if action == "status":
        pod = Pod.from_entry(entry)
        try:
            devs = pod.usbip_list()
            ports = pod.attached_ports()
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            print(f"link status failed: {exc}", file=sys.stderr)
            print("(the pod usbip server must be running - try "
                  "'pod dut link up' first)", file=sys.stderr)
            return 1
        if not devs:
            print("No DUT USB devices exported by the pod.")
        else:
            for d in devs:
                print(f"  {d['busid']}  {d['vid']}:{d['pid']}")
        print(f"Attached vhci ports: {ports}" if ports
              else "No vhci ports attached to this pod.")
        return 0

    if action == "up":
        try:
            dev = Pod.from_entry(entry).usbip_attach(ensure=not args.no_ensure)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            print(f"link up failed: {exc}", file=sys.stderr)
            return 1
        print(f"Attached DUT {dev['vid']}:{dev['pid']} (busid {dev['busid']}).")
        if dev.get("tty"):
            print(f"  DUT REPL: {dev['tty']}")
            print(f"  e.g. mpremote connect {dev['tty']}")
        else:
            print("  (no CDC tty appeared yet; check 'usbip port' / dmesg)")
        return 0

    if action == "down":
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
            print(f"link down failed: {exc}", file=sys.stderr)
            return 1
        return 0

    # action == "reprobe"
    try:
        result = Pod.from_entry(entry).reprobe_dut(force=args.force)
    except PodConflictError as exc:
        print(f"pod dut link reprobe: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0 if result.get("ok") else 1


# ── bench verb handlers ──────────────────────────────────────────────────


def cmd_bench_gpio(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    value = None if args.value is None else int(args.value, 0)
    result = pod.gpio(args.pin, value=value, pull=args.pull)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_bench_adc(args):
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    result = pod.adc(args.pin)
    print(result)
    return 0 if result.get("ok") else 1


def _parse_pins(spec):
    """'16' -> (16, 1); '16-23' -> (16, 8)  (base, width)."""
    if "-" in spec:
        a, b = spec.split("-", 1)
        a, b = int(a, 0), int(b, 0)
        return a, b - a + 1
    return int(spec, 0), 1


def cmd_bench_la(args):
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


def cmd_bench_device(args):
    """Bring up, query, or release an I2C or SPI device personality on the pod.

    up (default) and status need --bus to say which personality; down
    releases by --name and does not need --bus (peripheral_release is
    bus-agnostic; --name '*' sweeps every personality, I2C and SPI together).
    status on --bus spi reads the byte/transfer counters; I2C has none, so
    status there reports whether the named instance is up.
    """
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)

    if args.size is not None and not (1 <= args.size <= 8192):
        print("pod bench device: --size must be between 1 and 8192",
              file=sys.stderr)
        return 1
    if not (1 <= args.table_size <= 4096):
        print("pod bench device: --table-size must be between 1 and 4096",
              file=sys.stderr)
        return 1

    if args.action == "down":
        result = pod.peripheral_release(name=args.name or "*")
        print(result)
        return 0 if result.get("ok") else 1

    if args.bus is None:
        print("pod bench device: --bus {i2c,spi} is required for up/status",
              file=sys.stderr)
        return 1

    if args.bus == "i2c":
        name = args.name or "i2c_target"
        if args.action == "status":
            listed = pod.peripheral_list()
            names = listed.get("instances") or []
            result = {"label": args.label, "bus": "i2c", "name": name,
                      "present": name in names,
                      "note": "no transfer counters for I2C; presence is "
                              "read from peripheral_list's instance name list"}
            print(result)
            return 0
        regs = [int(x, 0) for x in args.regs] if args.regs else None
        kwargs = {"addr": int(args.addr, 0), "regs": regs, "bus": args.i2c_bus,
                  "scl": args.scl, "sda": args.sda, "name": name}
        if args.size is not None:
            kwargs["size"] = args.size
        result = pod.i2c_target(**kwargs)
    else:
        name = args.name or "spi_target"
        if args.action == "status":
            result = pod.spi_target_status(name=name)
        else:
            size = args.size if args.size is not None else 1024
            result = pod.spi_target(
                mode=args.mode, bits=args.bits, miso=args.miso, mosi=args.mosi,
                sck=args.sck, cs=args.cs, size=size,
                personality=args.personality, table_size=args.table_size,
                name=name)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_bench_device_regs(args):
    """Read or write an I2C or SPI device personality's register file.

    --off/--length read; --write writes at --off first. --table selects the
    SPI regfile personality's backing table (read/write; ignored for I2C).
    """
    entry = _require_pod(args.label)
    pod = Pod.from_entry(entry)
    write = [int(x, 0) for x in args.write] if args.write else None
    if args.bus == "i2c":
        result = pod.i2c_target_regs(off=args.off, length=args.length,
                                     write=write, name=args.name or "i2c_target")
    else:
        result = pod.spi_target_regs(off=args.off, length=args.length,
                                     write=write, table=args.table,
                                     name=args.name or "spi_target")
    print(result)
    return 0 if result.get("ok") else 1


def cmd_bench_uart(args):
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
  %(prog)s open my-pod
  %(prog)s exec my-pod "import os; print(os.uname())"
  %(prog)s mount my-pod ./firmware
  %(prog)s cp my-pod ./main.py :main.py
  %(prog)s dut identify my-pod
  %(prog)s dut link my-pod up
  %(prog)s dut exec my-pod "import os; print(os.uname())"
  %(prog)s dut flash my-pod firmware.hex
  %(prog)s bench gpio my-pod 5 --value 1

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

    # info
    p = sub.add_parser("info", help="Show full details for a registered pod")
    p.add_argument("label")

    # exec (pod-side)
    p = sub.add_parser("exec", help="Execute MicroPython code on the POD itself")
    p.add_argument("label")
    p.add_argument("code", help="MicroPython code string to run on the pod")

    # mount
    p = sub.add_parser("mount", help="Mount a local directory on the pod")
    p.add_argument("label")
    p.add_argument("directory", metavar="dir", help="Local directory to mount")

    # cp
    p = sub.add_parser("cp", help="Copy a file to or from the pod")
    p.add_argument("label")
    p.add_argument("src", help="Source path (use ':path' for pod-side)")
    p.add_argument("dst", help="Destination path (use ':path' for pod-side)")

    # pins
    p = sub.add_parser("pins",
                       help="Show the pod's own DUT-facing pin assignments")
    p.add_argument("label")
    p.add_argument("--cached", action="store_true",
                   help="Show the cached map instead of a live read")

    # flm (generic CMSIS flash algorithm)
    p = sub.add_parser("flm", help="Report or install the DUT's CMSIS flash algorithm")
    p.add_argument("label")
    p.add_argument("--device", default=None,
                   help="CMSIS device name (default: the DUT's declared target_family)")
    p.add_argument("--pack", default=None,
                   help="Explicit .pack or .FLM path instead of the pack cache")
    p.add_argument("--download", action="store_true",
                   help="Allow fetching the pack from the vendor index")
    p.add_argument("--vendor", default=None, help="Pack vendor, with --download")
    p.add_argument("--pack-name", default=None, dest="pack_name",
                   help="Pack name, with --download")
    p.add_argument("--force", action="store_true",
                   help="Reinstall even if the pod already has an algorithm")

    # install-udev
    p = sub.add_parser(
        "install-udev",
        help="Install a udev rule so ModemManager ignores pod-forwarded DUTs")
    p.add_argument("--vid", action="append", metavar="VID",
                   help="Also ignore MM for this idVendor (hex, e.g. f055); "
                        "repeatable")
    p.add_argument("--path", default=_UDEV_RULE_PATH,
                   help="Rule file path (default %s)" % _UDEV_RULE_PATH)
    p.add_argument("--print", action="store_true", dest="print_only",
                   help="Print the rule instead of installing it")

    # open (persistent streaming session on the pod's own socket REPL,
    # matching the MCP pod_open tool; `pod dut open` is the DUT-side twin)
    p = sub.add_parser(
        "open",
        help="Persistent streaming session on the pod's own socket REPL",
        description="Stream the pod's stdout to the console and optionally a "
                    "log file, and send typed lines to its stdin. Chain setup "
                    "before connecting with --mount/--exec/--cp/--soft-reset. "
                    "Ctrl-C interrupts the pod; Ctrl-D / EOF exits, leaving "
                    "it running. For a full raw terminal use `pod open-raw`.")
    p.add_argument("label")
    _add_stream_flags(p, "pod")

    # open-raw (raw-terminal passthrough on the pod's own socket REPL; an
    # operator-only form with no MCP counterpart)
    p = sub.add_parser(
        "open-raw",
        help="Full raw-terminal REPL passthrough to the pod's own socket REPL",
        description="Arrow keys, history, and paste mode all work; blocks "
                    "until you disconnect. Cannot chain setup - use `pod "
                    "open --mount/--exec/--cp/--soft-reset` for that.")
    p.add_argument("label")

    # dut (nested)
    dut_parser = sub.add_parser(
        "dut", help="Drive the device under test: REPL, SWD debug, flash, USB/IP link")
    dut_sub = dut_parser.add_subparsers(dest="dut_command", metavar="command")

    # dut open
    p = dut_sub.add_parser(
        "open", help="Open a persistent streaming session on the DUT's CDC tty",
        description="Stream the DUT's stdout to the console and optionally a "
                    "log file, and send typed lines to its stdin. Chain setup "
                    "before connecting with --mount/--exec/--cp/--soft-reset. "
                    "Ctrl-C interrupts the DUT; Ctrl-D / EOF exits, leaving "
                    "it running. --recover un-sticks a DUT latched in raw "
                    "REPL mode (Ctrl-C then Ctrl-B over the tty) first. With "
                    "no device the USB/IP link is brought up and its tty used, "
                    "which activates the pod USB host and can disturb the pod's "
                    "Wi-Fi; pass a device to skip that.")
    p.add_argument("label")
    p.add_argument("device", nargs="?",
                   help="DUT CDC tty (e.g. /dev/ttyACM0). Omit to bring the "
                        "USB/IP link up and use the tty it returns.")
    p.add_argument("--recover", action="store_true",
                   help="Un-stick a raw-REPL-latched DUT before connecting")
    p.add_argument("--settle", type=float, default=0.4,
                   help="With --recover: seconds to wait after each control char (default 0.4)")
    p.add_argument("--read-wait", type=float, default=0.6, dest="read_wait",
                   help="With --recover: seconds before reading the prompt back (default 0.6)")
    _add_stream_flags(p, "DUT")

    # dut exec
    p = dut_sub.add_parser(
        "exec", help="Run MicroPython on the DUT (ensure link + attach + mpremote)")
    p.add_argument("label")
    p.add_argument("code", help="MicroPython code string to run on the DUT")

    # dut flash
    p = dut_sub.add_parser("flash", help="Flash a DUT firmware image via the pod")
    p.add_argument("label")
    p.add_argument("image", help="Firmware image path (raw binary or ELF)")
    p.add_argument("--addr", default="0",
                   help="Flash base address (default: 0; ignored for ELF)")
    p.add_argument("--target", default=None, help="Target MCU identifier")
    p.add_argument("--keep-attached", action="store_true", dest="keep_attached",
                   help="Do not detach a live USB/IP session first (risks a wedge)")
    p.add_argument("--mass-erase", action="store_true", dest="mass_erase",
                   help="Erase the entire DUT flash before programming")
    p.add_argument("--force", action="store_true",
                   help="Bump another caller's USB/IP session instead of refusing")

    # dut erase
    p = dut_sub.add_parser("erase", help="Erase the entire DUT flash via the pod")
    p.add_argument("label")
    p.add_argument("--keep-attached", action="store_true", dest="keep_attached",
                   help="Do not detach a live USB/IP session first (risks a wedge)")
    p.add_argument("--force", action="store_true",
                   help="Bump another caller's USB/IP session instead of refusing")

    # dut reset
    p = dut_sub.add_parser("reset", help="Reset the DUT via the pod")
    p.add_argument("label")
    p.add_argument("--mode", default="sysreset", choices=["sysreset", "halt"],
                   help="Reset method (default: sysreset)")
    p.add_argument("--keep-attached", action="store_true", dest="keep_attached",
                   help="Do not detach a live USB/IP session first (risks a wedge)")
    p.add_argument("--force", action="store_true",
                   help="Bump another caller's USB/IP session instead of refusing")

    # dut reg (read-or-write; value omitted = read)
    p = dut_sub.add_parser(
        "reg", help="Read or write a DUT core register over SWD (core halted)")
    p.add_argument("label")
    p.add_argument("reg", help="regsel 0..18 or name (r0..r12, sp, lr, pc, "
                                "xpsr, msp, psp)")
    p.add_argument("value", nargs="?", default=None,
                   help="32-bit value to write (e.g. 0x20004000); omit to read")

    # dut mem (read-or-write, inline or streamed to a file; value/data omitted = read)
    p = dut_sub.add_parser(
        "mem", help="Read or write DUT memory over SWD (RAM/peripherals only)")
    p.add_argument("label")
    p.add_argument("addr", help="Address (e.g. 0x20000000)")
    p.add_argument("length", nargs="?", default=None,
                   help="Bytes to read, 1..4096 unless --out (required for a "
                        "read; ignored for --data)")
    p.add_argument("--data", default=None, metavar="HEX",
                   help="Bytes as a hex string to write (e.g. deadbeef); omit to read")
    p.add_argument("--out", default=None, metavar="PATH",
                   help="Stream a read straight from pod RAM to this host "
                        "file instead of printing it inline (no 4096-byte cap)")

    # dut halt / resume
    p = dut_sub.add_parser(
        "halt", help="Halt the DUT core over SWD (hold; no auto-resume)")
    p.add_argument("label")
    p.add_argument("--keep-attached", action="store_true", dest="keep_attached",
                   help="Do not detach a live USB/IP session first (risks a wedge)")
    p.add_argument("--force", action="store_true",
                   help="Bump another caller's USB/IP session instead of refusing")

    p = dut_sub.add_parser("resume", help="Resume the DUT core over SWD")
    p.add_argument("label")

    # dut gdb
    p = dut_sub.add_parser("gdb", help="Start a local GDB RSP server to the DUT")
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

    # dut identify
    p = dut_sub.add_parser(
        "identify", help="Show / set / verify the DUT a pod is wired to",
        description="With no flags, probe the live DUT over SWD and reconcile "
                    "it against the declared block. --dut-* set declared "
                    "metadata; --adopt snapshots the live IDs into expected{}.")
    p.add_argument("label")
    p.add_argument("--no-probe", action="store_true", dest="no_probe",
                   help="Do not read the live DUT (registry-only)")
    p.add_argument("--adopt", action="store_true",
                   help="Snapshot the live DUT IDs into the declared expected{}")
    _add_dut_flags(p)

    # dut link
    p = dut_sub.add_parser(
        "link", help="Bring up, inspect, or tear down the pod's DUT USB/IP export",
        description="Inspect or drive the pod's USB/IP export of the DUT. "
                    "status (the default) is a read-only list of what the "
                    "pod currently exports. up brings the pod USB host + "
                    "usbip server up (unless --no-ensure), attaches the "
                    "exported device on this host (needs passwordless sudo "
                    "for usbip), and reports the DUT's CDC tty. NB: "
                    "activating the pod USB host can disturb Wi-Fi. down "
                    "detaches a vhci port (--port), or every port attached "
                    "to this pod. reprobe re-seeds a stale or empty export "
                    "without a cold power cycle.")
    p.add_argument("label")
    p.add_argument("action", nargs="?", default="status",
                   choices=["status", "up", "down", "reprobe"],
                   help="status (default): list exported devices; up: bring "
                        "up + attach; down: detach; reprobe: re-seed a "
                        "stale/empty export")
    p.add_argument("--no-ensure", action="store_true", dest="no_ensure",
                   help="up: do not (re)start the pod USB host + usbip server first")
    p.add_argument("--port", type=int, default=None,
                   help="down: vhci port from 'usbip port' (omit to detach "
                        "all for this pod)")
    p.add_argument("--force", action="store_true",
                   help="reprobe: bump another host's live USB/IP import "
                        "instead of refusing")

    # bench (nested)
    bench_parser = sub.add_parser(
        "bench", help="Drive the pod's instruments pointed at the DUT: GPIO, "
                      "ADC, logic analyser, I2C/SPI device personalities, UART tap")
    bench_sub = bench_parser.add_subparsers(dest="bench_command", metavar="command")

    # bench gpio
    p = bench_sub.add_parser("gpio", help="Read or drive a pod GPIO")
    p.add_argument("label")
    p.add_argument("pin", type=int, help="GPIO number")
    p.add_argument("--value", default=None, help="0/1 to drive; omit to read")
    p.add_argument("--pull", default=None, choices=["up", "down"],
                   help="Input pull (read only)")

    # bench adc
    p = bench_sub.add_parser("adc", help="Sample a pod ADC channel")
    p.add_argument("label")
    p.add_argument("pin", type=int, help="ADC-capable GPIO number")

    # bench la (logic analyser)
    p = bench_sub.add_parser("la", help="Logic-analyse DUT pins (capture -> VCD)")
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

    # bench device (i2c or spi personality: bring up / status / release)
    p = bench_sub.add_parser(
        "device",
        help="Bring up, query, or release an I2C or SPI device personality on the pod")
    p.add_argument("label")
    p.add_argument("action", nargs="?", default="up",
                   choices=["up", "status", "down"],
                   help="up (default): bring up; status: SPI byte/transfer "
                        "counters, or whether the named instance is up for "
                        "i2c (no counters there); down: release (bus-agnostic)")
    p.add_argument("--bus", choices=["i2c", "spi"], default=None,
                   help="Personality to act on; required for up/status, "
                        "ignored for down")
    # i2c-only
    p.add_argument("--addr", default="0x42",
                   help="i2c: 7-bit address (default: 0x42)")
    p.add_argument("--regs", nargs="*", metavar="BYTE",
                   help="i2c: initial register bytes from offset 0 (e.g. 0xAB 0xCD)")
    p.add_argument("--i2c-bus", type=int, default=1, dest="i2c_bus",
                   help="i2c: hardware I2C bus id (default: 1)")
    p.add_argument("--scl", type=int, default=11, help="i2c: SCL GPIO (default: 11)")
    p.add_argument("--sda", type=int, default=10, help="i2c: SDA GPIO (default: 10)")
    # spi-only
    p.add_argument("--mode", type=int, default=0, choices=[0, 1, 2, 3],
                   help="spi: mode 0-3 (default: 0)")
    p.add_argument("--bits", type=int, default=8, choices=[8],
                   help="spi: frame width in bits (default: 8; 8-bit only)")
    p.add_argument("--miso", type=int, default=16, help="spi: MISO GPIO (default: 16)")
    p.add_argument("--mosi", type=int, default=19, help="spi: MOSI GPIO (default: 19)")
    p.add_argument("--sck", type=int, default=18, help="spi: SCK GPIO (default: 18)")
    p.add_argument("--cs", type=int, default=17,
                   help="spi: CS GPIO, active low (default: 17)")
    p.add_argument("--size", type=int, default=None,
                   help="i2c: register-file byte size (default: 256). spi: "
                        "MOSI capture retained-byte capacity, rounded up to "
                        "a power of two, max 8192, RAM used is 4x "
                        "(default: 1024)")
    p.add_argument("--personality", choices=["stream", "regfile"], default="stream",
                   help="spi: stream = MISO counter + MOSI capture (default). "
                        "regfile = [reg_ptr][data...] register-file responder")
    p.add_argument("--table-size", type=int, default=256, dest="table_size",
                   help="spi: regfile personality's register table size per "
                        "direction (default: 256, max 4096)")
    p.add_argument("--name", default=None,
                   help="Instance name (default: i2c_target / spi_target by "
                        "--bus; '*' for down releases every personality)")

    # bench device-regs (i2c or spi personality's register file)
    p = bench_sub.add_parser(
        "device-regs",
        help="Read/write an I2C or SPI device personality's register file")
    p.add_argument("label")
    p.add_argument("--bus", choices=["i2c", "spi"], required=True,
                   help="Which personality's register file")
    p.add_argument("--off", type=int, default=0, help="Register offset (default: 0)")
    p.add_argument("--length", type=int, default=None,
                   help="Bytes to read (default: to end)")
    p.add_argument("--write", nargs="*", metavar="BYTE",
                   help="Bytes to write at off first")
    p.add_argument("--table", choices=["read", "write"], default="read",
                   help="spi personality=regfile only: which backing table "
                        "(default: read)")
    p.add_argument("--name", default=None,
                   help="Instance name (default: i2c_target / spi_target by --bus)")

    # bench uart (DUT UART bridge)
    p = bench_sub.add_parser("uart", help="Stream DUT UART output over TCP")
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

    if args.command == "dut":
        if not getattr(args, "dut_command", None):
            dut_parser.print_help()
            return 1
        handler = {
            "open": cmd_dut_open,
            "exec": cmd_dut_exec,
            "flash": cmd_dut_flash,
            "erase": cmd_dut_erase,
            "reset": cmd_dut_reset,
            "reg": cmd_dut_reg,
            "mem": cmd_dut_mem,
            "halt": cmd_dut_halt,
            "resume": cmd_dut_resume,
            "gdb": cmd_dut_gdb,
            "identify": cmd_dut_identify,
            "link": cmd_dut_link,
        }[args.dut_command]
        return handler(args)

    if args.command == "bench":
        if not getattr(args, "bench_command", None):
            bench_parser.print_help()
            return 1
        handler = {
            "gpio": cmd_bench_gpio,
            "adc": cmd_bench_adc,
            "la": cmd_bench_la,
            "device": cmd_bench_device,
            "device-regs": cmd_bench_device_regs,
            "uart": cmd_bench_uart,
        }[args.bench_command]
        return handler(args)

    handler = {
        "discover": cmd_discover,
        "list": cmd_list,
        "register": cmd_register,
        "unregister": cmd_unregister,
        "info": cmd_info,
        "exec": cmd_exec,
        "mount": cmd_mount,
        "cp": cmd_cp,
        "pins": cmd_pins,
        "flm": cmd_flm,
        "install-udev": cmd_install_udev,
        "open": cmd_repl,
        "open-raw": cmd_repl_raw,
    }[args.command]

    return handler(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
