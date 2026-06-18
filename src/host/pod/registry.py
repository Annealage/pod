"""Pod registry storage.

Persists pod entries in ~/.config/pod/pods.json (or POD_CONFIG_DIR/pods.json).
Mirrors the mpy-dev registry model: labels map to pod record dicts.

A pod is identified by stable handles, not a drifting DHCP lease (see
pod.target for the connect strategy). Pod record keys:

  hostname      - mDNS hostname, e.g. "annealage-pod.local" (display + tier-3
                  re-resolve key); null until first discovered
  addr6         - list of IPv6 literals, ULA/global first then link-local, no
                  %zone (the zone is host-specific, re-derived at connect time)
  addr4         - DHCP IPv4 literal (the only non-stable handle), or null
  address       - back-compat mirror of the preferred address (addr4, else the
                  first addr6, else hostname); old clients still read this
  repl_port     - ampremote socket REPL port
  usbip_port    - USB/IP port (int or null)
  uart_port     - UART-over-TCP port (int or null)
  gdb_port      - GDB debug-command server port (int or null)
  carrier_id    - carrier board identifier
  mp_version    - MicroPython version string
  fingerprint   - machine.unique_id() hex; the identity an IPv4/mDNS connect
                  must confirm before it is trusted (null until probed)
  dut           - declared DUT block (target/flash/expected ids/notes), or null;
                  see reconcile_dut. User-managed, preserved across discovery.
  last_seen     - ISO-8601 UTC timestamp of last discovery update
  notes         - free-text notes (optional)
  links         - list of {label, rel} dicts for associated pods (optional)
"""

import ipaddress
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REGISTRY_VERSION = 2


def _config_dir() -> Path:
    """Return the config directory, respecting POD_CONFIG_DIR env override."""
    override = os.environ.get("POD_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "pod"


def _registry_file() -> Path:
    return _config_dir() / "pods.json"


def _preferred_address(entry: dict) -> Optional[str]:
    """The back-compat `address` mirror: addr4, else first addr6, else hostname."""
    if entry.get("addr4"):
        return entry["addr4"]
    if entry.get("addr6"):
        return entry["addr6"][0]
    return entry.get("hostname")


def _migrate_entry(entry: dict) -> dict:
    """Bring a v1 entry (address-only) up to the v2 handle schema, in place.

    Classifies the old `address` into the right handle (IPv4 -> addr4, IPv6 ->
    addr6, anything else -> hostname) and fills the remaining keys with their
    empty defaults. Idempotent and non-destructive: `address` is left in place.
    """
    if "addr4" not in entry and "addr6" not in entry and "hostname" not in entry:
        addr = entry.get("address")
        entry.setdefault("addr6", [])
        entry.setdefault("addr4", None)
        entry.setdefault("hostname", None)
        if addr:
            try:
                ip = ipaddress.ip_address(addr)
                if ip.version == 6:
                    entry["addr6"] = [addr]
                else:
                    entry["addr4"] = addr
            except ValueError:
                entry["hostname"] = addr     # a hostname (e.g. "pod.local")
    entry.setdefault("addr6", [])
    entry.setdefault("addr4", None)
    entry.setdefault("hostname", None)
    entry.setdefault("fingerprint", None)
    entry.setdefault("dut", None)
    return entry


def load_registry() -> dict:
    """Load the pod registry, returning an empty structure if missing.

    Migrates v1 (address-only) entries to the v2 handle schema once, rewriting
    the file on the version bump so the migration is paid at most once.
    """
    path = _registry_file()
    if not path.exists():
        return {"version": _REGISTRY_VERSION, "pods": {}}
    with open(path) as f:
        data = json.load(f)
    data.setdefault("pods", {})
    if data.get("version", 1) < _REGISTRY_VERSION:
        for entry in data["pods"].values():
            _migrate_entry(entry)
        data["version"] = _REGISTRY_VERSION
        save_registry(data)
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
    entry.setdefault("address", _preferred_address(entry))
    registry["pods"][label] = entry
    save_registry(registry)


def update_pod(label: str, **fields) -> Optional[dict]:
    """Merge fields into an existing entry (e.g. learned addrs/fingerprint/dut).

    Returns the updated entry, or None if the label is unknown. Keeps the
    back-compat `address` mirror in sync when the handles change.
    """
    registry = load_registry()
    entry = registry["pods"].get(label)
    if entry is None:
        return None
    entry.update(fields)
    entry["address"] = _preferred_address(entry)
    save_registry(registry)
    return entry


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

    Updates the connection handles (hostname/addr6/addr4), ports, carrier_id,
    mp_version, and last_seen. Preserves user/probe-managed fields (notes, links,
    dut, fingerprint) so a browse-driven rediscovery never clobbers a declared
    DUT block or a learned identity.
    """
    registry = load_registry()
    existing = registry["pods"].get(label, {})

    entry = {
        "hostname": getattr(pod_info, "hostname", "") or None,
        "addr6": list(getattr(pod_info, "addr6", []) or []),
        "addr4": getattr(pod_info, "addr4", None),
        "repl_port": pod_info.repl_port,
        "usbip_port": pod_info.usbip_port,
        "uart_port": pod_info.uart_port,
        "gdb_port": pod_info.gdb_port,
        "carrier_id": pod_info.carrier_id,
        "mp_version": pod_info.mp_version,
        "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Preserve user/probe-managed fields across a discovery refresh.
    for field in ("notes", "links", "dut", "fingerprint"):
        if field in existing:
            entry[field] = existing[field]
    entry.setdefault("fingerprint", None)
    entry.setdefault("dut", None)
    entry["address"] = _preferred_address(entry)

    registry["pods"][label] = entry
    save_registry(registry)
    return entry


# ── DUT declared-vs-live reconciliation ───────────────────────────────────

# CPUID revision bits vary across silicon steppings of the same part; compare on
# the architecture/part field (PARTNO + variant) and ignore the low revision.
_CPUID_MASK = 0xFFFFFFF0

_DUT_ID_FIELDS = ("dpidr", "ap_idr", "cpuid", "rom_base")


def reconcile_dut(declared: Optional[dict], live: Optional[dict]) -> dict:
    """Compare a declared DUT block against a live discover() result.

    Returns {"verdict": <str>, "fields": {name: {declared, live, match}}, ...}.
    Never mutates the declared block; the only write paths to "dut" are the
    explicit `pod dut` set/adopt verbs.

    verdict:
      MATCH        every declared expected id present equals the live read
      MISMATCH     at least one declared expected id differs (wrong/swapped DUT)
      UNDECLARED   a dut block exists but has no expected ids; live shown only
      NO_DECLARED  no dut block at all (pure discovery mode)
      NO_LIVE      declared but discover() failed/absent (target unpowered etc.)
    """
    expected = (declared or {}).get("expected") or {}
    live_ok = bool(live) and live.get("ok", True) and any(
        k in (live or {}) for k in _DUT_ID_FIELDS)

    if not declared:
        if not live_ok:
            return {"verdict": "NO_DECLARED", "fields": {}, "live": live}
        return {"verdict": "NO_DECLARED", "fields": {}, "live": live}
    if not live_ok:
        return {"verdict": "NO_LIVE", "fields": {}, "declared": declared,
                "live": live}
    if not expected:
        return {"verdict": "UNDECLARED", "fields": {}, "declared": declared,
                "live": live}

    fields = {}
    all_match = True
    for name in _DUT_ID_FIELDS:
        if name not in expected or name not in live:
            continue
        d, l = expected[name], live[name]
        if name == "cpuid":
            match = (int(d) & _CPUID_MASK) == (int(l) & _CPUID_MASK)
        else:
            match = int(d) == int(l)
        fields[name] = {"declared": d, "live": l, "match": match}
        all_match = all_match and match
    return {"verdict": "MATCH" if all_match else "MISMATCH", "fields": fields,
            "declared": declared, "live": live}
