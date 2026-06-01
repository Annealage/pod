"""Pod mDNS discovery.

Browses the _annealage-pod._tcp service type. Two backends:
  - zeroconf (preferred, if importable)
  - avahi-browse shell-out (fallback)

The parsing layer is a pure function (parse_avahi_line / parse_zeroconf_info)
so it can be tested offline without any live network access.

Resolved pod record keys:
  name        - mDNS service instance name
  address     - IPv4 address string
  port        - primary port (same as repl_port)
  repl_port   - ampremote socket REPL port (int)
  usbip_port  - USB/IP server port (int or None)
  uart_port   - UART-over-TCP port (int or None)
  carrier_id  - carrier board identifier string (may be empty)
  mp_version  - MicroPython version string (may be empty)
"""

import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Optional


_SERVICE_TYPE = "_annealage-pod._tcp"


@dataclass
class PodInfo:
    """Resolved pod service record."""

    name: str
    address: str
    port: int
    repl_port: int
    usbip_port: Optional[int] = None
    uart_port: Optional[int] = None
    carrier_id: str = ""
    mp_version: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "address": self.address,
            "port": self.port,
            "repl_port": self.repl_port,
            "usbip_port": self.usbip_port,
            "uart_port": self.uart_port,
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


def parse_avahi_line(line: str) -> Optional[PodInfo]:
    """Parse one resolved avahi-browse -rpt output line into a PodInfo.

    Expected format (semicolon-separated):
      =;iface;proto;name;type;domain;hostname;address;port;"k=v" "k=v" ...

    Returns None if the line is not a resolved record (does not start with '=')
    or cannot be parsed.
    """
    line = line.strip()
    if not line.startswith("="):
        return None

    parts = line.split(";", 9)
    if len(parts) < 10:
        return None

    # parts[0] = "=", [1]=iface, [2]=proto, [3]=name, [4]=type, [5]=domain,
    # [6]=hostname, [7]=address, [8]=port, [9]=txt_field
    name = parts[3]
    address = parts[7]
    try:
        port = int(parts[8])
    except ValueError:
        return None

    txt_raw = parts[9]
    # The TXT field contains space-separated quoted key=value tokens.
    try:
        txt_tokens = shlex.split(txt_raw)
    except ValueError:
        txt_tokens = txt_raw.split()
    props = _parse_txt_properties(txt_tokens)

    repl_port = int(props.get("repl-port", port))
    usbip_raw = props.get("usbip-port")
    uart_raw = props.get("uart-port")
    usbip_port = int(usbip_raw) if usbip_raw else None
    uart_port = int(uart_raw) if uart_raw else None

    return PodInfo(
        name=name,
        address=address,
        port=port,
        repl_port=repl_port,
        usbip_port=usbip_port,
        uart_port=uart_port,
        carrier_id=props.get("carrier-id", ""),
        mp_version=props.get("mp-version", ""),
    )


def _discover_avahi(timeout: float = 5.0) -> list:
    """Run avahi-browse and return a list of PodInfo records."""
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
    return pods


def _discover_zeroconf(timeout: float = 5.0) -> list:
    """Use zeroconf to browse _annealage-pod._tcp and return PodInfo list."""
    import time
    from zeroconf import ServiceBrowser, Zeroconf
    from zeroconf._utils.ipaddress import cached_ip_addresses

    zc = Zeroconf()
    found = []

    class _Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            if info is None:
                return
            addresses = info.parsed_addresses()
            address = addresses[0] if addresses else ""
            props_raw = {
                k.decode() if isinstance(k, bytes) else k: (
                    v.decode() if isinstance(v, bytes) else (v or "")
                )
                for k, v in info.properties.items()
            }
            port = info.port
            repl_port = int(props_raw.get("repl-port", port))
            usbip_raw = props_raw.get("usbip-port")
            uart_raw = props_raw.get("uart-port")
            found.append(PodInfo(
                name=info.name,
                address=address,
                port=port,
                repl_port=repl_port,
                usbip_port=int(usbip_raw) if usbip_raw else None,
                uart_port=int(uart_raw) if uart_raw else None,
                carrier_id=props_raw.get("carrier-id", ""),
                mp_version=props_raw.get("mp-version", ""),
            ))

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
