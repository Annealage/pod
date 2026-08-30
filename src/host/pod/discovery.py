"""Pod mDNS discovery.

Browses the _annealage-pod._tcp service type. Two backends:
  - zeroconf (preferred, if importable)
  - avahi-browse shell-out (fallback)

The parsing layer is a pure function (parse_avahi_line / parse_zeroconf_info)
so it can be tested offline without any live network access.

Each resolved pod carries its stable handles (the mDNS hostname and the full
set of IPv6 addresses, ULA/global before link-local) alongside the DHCP IPv4,
so a caller can register the handles rather than a lease that drifts. PodInfo
keys:
  name        - mDNS service instance name
  hostname    - mDNS host A/AAAA name, e.g. "annealage-pod.local"
  addr6       - IPv6 literals, ULA/global first then link-local, no %zone
  addr4       - IPv4 address string, or None
  address     - back-compat preferred address (addr4, else first addr6)
  port        - primary port (same as repl_port)
  repl_port   - ampremote socket REPL port (int)
  usbip_port  - USB/IP server port (int or None)
  uart_port   - UART-over-TCP port (int or None)
  gdb_port    - GDB debug-command server port (int or None)
  control_port - holder/status listener port (int or None; absent on a pod
                whose firmware predates it, which is how a caller tells)
  carrier_id  - carrier board identifier string (may be empty)
  mp_version  - MicroPython version string (may be empty)
"""

import ipaddress
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional


_SERVICE_TYPE = "_annealage-pod._tcp"


def _sort_addrs(addrs):
    """Split a mixed address list into (addr4, addr6) with addr6 ULA-first.

    Zones (%iface) are stripped - they are host-specific and re-derived at
    connect time. Link-local IPv6 sorts after ULA/global so a resolver prefers
    the address that needs no zone.
    """
    addr4 = None
    glob6, link6 = [], []
    for a in addrs:
        if not a:
            continue
        a = a.split("%")[0]
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.version == 4:
            if addr4 is None:
                addr4 = a
        elif ip.is_link_local:
            if a not in link6:
                link6.append(a)
        else:
            if a not in glob6:
                glob6.append(a)
    return addr4, glob6 + link6


@dataclass
class PodInfo:
    """Resolved pod service record."""

    name: str
    address: str = ""
    port: int = 0
    repl_port: int = 0
    usbip_port: Optional[int] = None
    uart_port: Optional[int] = None
    gdb_port: Optional[int] = None
    control_port: Optional[int] = None
    carrier_id: str = ""
    mp_version: str = ""
    hostname: str = ""
    addr6: List[str] = field(default_factory=list)
    addr4: Optional[str] = None

    def __post_init__(self):
        # Backfill the handle fields from a legacy single `address`, and derive
        # the back-compat `address` mirror from the handles, so old and new
        # callers (and tests) both produce a coherent record.
        if not self.addr4 and not self.addr6 and self.address:
            a4, a6 = _sort_addrs([self.address])
            self.addr4, self.addr6 = a4, a6
        if not self.address:
            self.address = self.addr4 or (self.addr6[0] if self.addr6 else "")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hostname": self.hostname,
            "addr6": self.addr6,
            "addr4": self.addr4,
            "address": self.address,
            "port": self.port,
            "repl_port": self.repl_port,
            "usbip_port": self.usbip_port,
            "uart_port": self.uart_port,
            "gdb_port": self.gdb_port,
            "control_port": self.control_port,
            "carrier_id": self.carrier_id,
            "mp_version": self.mp_version,
        }


def _parse_txt_properties(txt_parts: list) -> dict:
    """Parse a list of 'key=value' strings (as produced by avahi or zeroconf) into a dict."""
    props = {}
    for part in txt_parts:
        part = part.strip().strip('"')
        if "=" in part:
            k, _, v = part.partition("=")
            props[k.strip()] = v.strip()
    return props


def _ports_from_props(props: dict, default_port: int):
    """Pull the repl/usbip/uart/gdb/control ports out of a parsed TXT dict.

    Each optional port is None when the key is absent, which is meaningful
    rather than merely missing: the pod only advertises a key once that listener
    has actually bound, so None says the feature is not there to be used.
    """
    repl_port = int(props.get("repl-port", default_port))
    usbip_raw = props.get("usbip-port")
    uart_raw = props.get("uart-port")
    gdb_raw = props.get("gdb-port")
    control_raw = props.get("control-port")
    return (
        repl_port,
        int(usbip_raw) if usbip_raw else None,
        int(uart_raw) if uart_raw else None,
        int(gdb_raw) if gdb_raw else None,
        int(control_raw) if control_raw else None,
    )


def parse_avahi_line(line: str) -> Optional[PodInfo]:
    """Parse one resolved avahi-browse -rpt output line into a PodInfo.

    Expected format (semicolon-separated):
      =;iface;proto;name;type;domain;hostname;address;port;"k=v" "k=v" ...

    avahi emits one resolved line per (interface, protocol, address), so a
    dual-stack pod produces several lines (one IPv4, one or more IPv6) that
    _discover_avahi merges by (name, hostname). Returns None if the line is not
    a resolved record (does not start with '=') or cannot be parsed.
    """
    line = line.strip()
    if not line.startswith("="):
        return None

    parts = line.split(";", 9)
    if len(parts) < 10:
        return None

    # parts[0]="=", [1]=iface, [2]=proto, [3]=name, [4]=type, [5]=domain,
    # [6]=hostname, [7]=address, [8]=port, [9]=txt_field
    name = parts[3]
    hostname = parts[6].rstrip(".")
    address = parts[7].split("%")[0]
    try:
        port = int(parts[8])
    except ValueError:
        return None

    txt_raw = parts[9]
    try:
        txt_tokens = shlex.split(txt_raw)
    except ValueError:
        txt_tokens = txt_raw.split()
    props = _parse_txt_properties(txt_tokens)
    (repl_port, usbip_port, uart_port, gdb_port,
     control_port) = _ports_from_props(props, port)

    addr4, addr6 = _sort_addrs([address])
    return PodInfo(
        name=name,
        hostname=hostname,
        addr4=addr4,
        addr6=addr6,
        port=port,
        repl_port=repl_port,
        usbip_port=usbip_port,
        uart_port=uart_port,
        gdb_port=gdb_port,
        control_port=control_port,
        carrier_id=props.get("carrier-id", ""),
        mp_version=props.get("mp-version", ""),
    )


def _merge_pods(pods: list) -> list:
    """Merge per-line/per-address PodInfo records by (name, hostname).

    Combines the separate IPv4 and IPv6 lines avahi emits for a dual-stack pod
    into a single record carrying the full addr6 list + addr4.
    """
    merged = {}
    for p in pods:
        key = (p.name, p.hostname)
        if key not in merged:
            merged[key] = p
            continue
        cur = merged[key]
        all4 = [a for a in (cur.addr4, p.addr4) if a]
        addr4, addr6 = _sort_addrs(all4 + cur.addr6 + p.addr6)
        cur.addr4 = addr4
        cur.addr6 = addr6
        cur.address = addr4 or (addr6[0] if addr6 else cur.address)
    return list(merged.values())


def _discover_avahi(timeout: float = 5.0) -> list:
    """Run avahi-browse and return a list of merged PodInfo records."""
    try:
        result = subprocess.run(
            ["avahi-browse", "-rpt", _SERVICE_TYPE],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    pods = []
    for line in result.stdout.splitlines():
        pod = parse_avahi_line(line)
        if pod is not None:
            pods.append(pod)
    return _merge_pods(pods)


def parse_zeroconf_info(info) -> Optional[PodInfo]:
    """Build a PodInfo from a zeroconf ServiceInfo (pure, for testability)."""
    if info is None:
        return None
    try:
        from zeroconf import IPVersion
        addresses = info.parsed_addresses(IPVersion.All)
    except Exception:  # noqa: BLE001 - older zeroconf without the arg
        addresses = info.parsed_addresses()
    addr4, addr6 = _sort_addrs(addresses)
    props_raw = {
        k.decode() if isinstance(k, bytes) else k: (
            v.decode() if isinstance(v, bytes) else (v or "")
        )
        for k, v in info.properties.items()
    }
    (repl_port, usbip_port, uart_port, gdb_port,
     control_port) = _ports_from_props(
        props_raw, info.port)
    hostname = (getattr(info, "server", "") or "").rstrip(".")
    return PodInfo(
        name=info.name,
        hostname=hostname,
        addr4=addr4,
        addr6=addr6,
        port=info.port,
        repl_port=repl_port,
        usbip_port=usbip_port,
        uart_port=uart_port,
        gdb_port=gdb_port,
        control_port=control_port,
        carrier_id=props_raw.get("carrier-id", ""),
        mp_version=props_raw.get("mp-version", ""),
    )


def _discover_zeroconf(timeout: float = 5.0) -> list:
    """Use zeroconf to browse _annealage-pod._tcp and return PodInfo list."""
    import time
    from zeroconf import ServiceBrowser, Zeroconf

    zc = Zeroconf()
    found = []

    class _Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            pod = parse_zeroconf_info(info)
            if pod is not None:
                found.append(pod)

        def remove_service(self, zc, type_, name):
            pass

        def update_service(self, zc, type_, name):
            pass

    browser = ServiceBrowser(zc, f"{_SERVICE_TYPE}.local.", _Listener())
    time.sleep(timeout)
    zc.close()
    return found


def discover_pods(timeout: float = 5.0) -> list:
    """Discover pods on the local network via mDNS.

    Uses zeroconf if available, otherwise falls back to avahi-browse.
    Returns a list of PodInfo instances.
    Does NOT modify the registry; the caller decides what to do with results.
    """
    try:
        import zeroconf  # noqa: F401 - test availability
        return _discover_zeroconf(timeout=timeout)
    except ImportError:
        return _discover_avahi(timeout=timeout)
