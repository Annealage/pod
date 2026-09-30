"""Building registry entries for local benches (a DUT with a host-attached probe).

A pod entry is discovered over mDNS. A local bench has nothing to discover, so
its entry is assembled from what the operator says, optionally seeded from the
``mpy-dev`` registry, which already links a board's programmer to the board.
"""

import json
import os
from pathlib import Path
from typing import Optional

from pod.routes import CHANNELS, RouteError, route

# USB vendor ids of debug probes; the linked device with one of these is the
# programmer and the other is the DUT's own USB.
_PROBE_VIDS = ("0483", "1366", "0d28", "2e8a", "1fc9", "c251")


def _mpy_dev_file() -> Path:
    env = os.environ.get("MPY_DEV_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "mpy-dev" / "devices.json"


def _load_mpy_dev() -> dict:
    try:
        return json.loads(_mpy_dev_file().read_text())
    except (OSError, ValueError) as exc:
        raise RouteError("cannot read the mpy-dev registry at %s: %s"
                         % (_mpy_dev_file(), exc)) from exc


def from_mpy_dev(name: str) -> dict:
    """Bench fields for a board registered in mpy-dev, by either linked label.

    Returns the probe uid, the programmer's serial port (the board's UART when
    it is a virtual COM port) and the DUT's own USB serial port, where known.
    """
    data = _load_mpy_dev()
    devices = data.get("devices", {})
    if name not in devices:
        raise RouteError("mpy-dev has no device %r" % name)
    partners = [d for link in data.get("links", []) if name in link["devices"]
                for d in link["devices"] if d != name]
    group = [name] + [p for p in partners if p in devices]
    probes = [d for d in group if devices[d].get("vid") in _PROBE_VIDS]
    duts = [d for d in group if d not in probes]
    if not probes:
        raise RouteError("no debug probe is linked to %r in mpy-dev" % name)
    probe = devices[probes[0]]
    out = {"uid": probe["serial_number"]}
    if probe.get("by_id_path"):
        out["uart_tty"] = probe["by_id_path"]
    if duts and devices[duts[0]].get("by_id_path"):
        out["usb_tty"] = devices[duts[0]]["by_id_path"]
    return out


def bench_entry(uid: str, family: str, flash_base: Optional[int] = None,
                flash_size: Optional[int] = None,
                flash_algorithm: Optional[str] = None,
                uart_tty: Optional[str] = None, baud: Optional[int] = None,
                usb_tty: Optional[str] = None,
                instruments_pod: Optional[str] = None) -> dict:
    """A local-bench registry entry: pyocd debug, optional tty/pod routes."""
    dut = {"target_family": family}
    if flash_base is not None:
        dut["flash_base"] = flash_base
    if flash_size is not None:
        dut["flash_size"] = flash_size
    if flash_algorithm:
        dut["flash_algorithm"] = flash_algorithm
    entry = {"debug": {"via": "pyocd", "uid": uid}, "dut": dut}
    if uart_tty:
        entry["uart"] = {"via": "tty", "tty": uart_tty}
        if baud:
            entry["uart"]["baud"] = baud
    if usb_tty:
        dut["usb"] = {"conn": "agent-direct", "tty": usb_tty}
    entry["instruments"] = ({"via": "pod", "pod": instruments_pod}
                            if instruments_pod else {"via": "none"})
    return entry


def set_route(entry: dict, channel: str, via: str, **fields) -> dict:
    """Return `entry` with one channel's route replaced, after validating it."""
    if channel not in CHANNELS:
        raise RouteError("unknown channel %r" % channel)
    updated = dict(entry)
    updated[channel] = dict({"via": via}, **{k: v for k, v in fields.items()
                                             if v is not None})
    route(updated, channel)
    return updated
