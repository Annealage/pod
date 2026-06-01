"""Pod registry storage.

Persists pod entries in ~/.config/pod/pods.json (or POD_CONFIG_DIR/pods.json).
Mirrors the mpy-dev registry model: labels map to pod record dicts.

Pod record keys:
  address       - last known IPv4 address
  repl_port     - ampremote socket REPL port
  usbip_port    - USB/IP port (int or null)
  uart_port     - UART-over-TCP port (int or null)
  carrier_id    - carrier board identifier
  mp_version    - MicroPython version string
  last_seen     - ISO-8601 UTC timestamp of last discovery update
  notes         - free-text notes (optional)
  links         - list of {label, rel} dicts for associated pods (optional)
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _config_dir() -> Path:
    """Return the config directory, respecting POD_CONFIG_DIR env override."""
    override = os.environ.get("POD_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "pod"


def _registry_file() -> Path:
    return _config_dir() / "pods.json"


def load_registry() -> dict:
    """Load the pod registry, returning an empty structure if missing."""
    path = _registry_file()
    if not path.exists():
        return {"version": 1, "pods": {}}
    with open(path) as f:
        data = json.load(f)
    data.setdefault("pods", {})
    return data


def save_registry(registry: dict) -> None:
    """Write the pod registry to disk."""
    path = _registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(registry, f, indent=2)
        f.write("\n")


def get_pod(label: str) -> Optional[dict]:
    """Return the registry entry for a label, or None if not found."""
    registry = load_registry()
    return registry["pods"].get(label)


def set_pod(label: str, entry: dict) -> None:
    """Insert or replace a pod entry under label."""
    registry = load_registry()
    registry["pods"][label] = entry
    save_registry(registry)


def remove_pod(label: str) -> bool:
    """Remove a pod entry by label. Returns True if removed, False if not found."""
    registry = load_registry()
    if label not in registry["pods"]:
        return False
    del registry["pods"][label]
    save_registry(registry)
    return True


def reconcile(label: str, pod_info) -> dict:
    """Update or create a registry entry from a discovered PodInfo.

    Updates address, ports, carrier_id, mp_version, and last_seen.
    Preserves existing notes and links fields.
    Returns the updated entry dict.
    """
    registry = load_registry()
    existing = registry["pods"].get(label, {})

    entry = {
        "address": pod_info.address,
        "repl_port": pod_info.repl_port,
        "usbip_port": pod_info.usbip_port,
        "uart_port": pod_info.uart_port,
        "carrier_id": pod_info.carrier_id,
        "mp_version": pod_info.mp_version,
        "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Preserve user-managed fields
    for field in ("notes", "links"):
        if field in existing:
            entry[field] = existing[field]

    registry["pods"][label] = entry
    save_registry(registry)
    return entry
