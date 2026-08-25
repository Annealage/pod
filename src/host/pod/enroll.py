"""Discover-and-register: enroll a pod from its mDNS broadcast.

Shared by the `pod register` CLI (no-address form) and the MCP pod_register
tool, so an agent or a human enrolls a freshly-flashed pod by name without
hand-copying an address. Stores the stable handles (hostname + IPv6 + IPv4) and
reads the identity fingerprint so an IPv4/mDNS connect is trusted from session
one.
"""

from datetime import datetime, timezone

from pod.discovery import discover_pods
from pod.registry import get_pod, set_pod


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def match_discovered(pods, label, match=None):
    """Pick the single discovered pod matching `match` (or label) by name.

    Returns the PodInfo, or None if there is no unambiguous match.
    """
    want = (match or label or "").rstrip(".").lower()
    cands = []
    for p in pods:
        host = (p.hostname or "").rstrip(".").lower()
        inst = (p.name or "").split(".")[0].lower()
        if not want or want in (host, inst) or host.startswith(want + "."):
            cands.append(p)
    if len(cands) == 1:
        return cands[0]
    if not cands and len(pods) == 1 and not match:
        return pods[0]
    return None


def entry_from_podinfo(info):
    """Build a fresh registry entry dict from a discovered PodInfo."""
    return {
        "hostname": info.hostname or None,
        "addr6": list(info.addr6),
        "addr4": info.addr4,
        "repl_port": info.repl_port or 8266,
        "usbip_port": info.usbip_port,
        "uart_port": info.uart_port,
        "gdb_port": info.gdb_port or 3335,
        "carrier_id": info.carrier_id or "",
        "mp_version": info.mp_version or "",
        "last_seen": _now(),
    }


def carry_over(existing, entry):
    """Preserve user/probe-managed fields from an existing registration, in place.

    A `--force` re-register rebuilds the entry to refresh the network handles
    (hostname/addr6/addr4). This keeps the things the fresh discovery does not
    know about so re-registering to fix an address never silently drops them:
      - the declared DUT block (any explicit --dut fields merge on top of it);
      - notes (unless new notes were given);
      - the stored fingerprint, when the re-probe produced none (pod briefly
        unreachable) - so the identity guard is not lost.
    """
    if not existing:
        return entry
    if existing.get("dut"):
        merged = dict(existing["dut"])
        if entry.get("dut"):
            merged.update(entry["dut"])
        entry["dut"] = merged
    if entry.get("notes") is None and existing.get("notes") is not None:
        entry["notes"] = existing["notes"]
    if not entry.get("fingerprint") and existing.get("fingerprint"):
        entry["fingerprint"] = existing["fingerprint"]
    if not entry.get("pins") and existing.get("pins"):
        entry["pins"] = existing["pins"]
    return entry


def read_pinmap(entry):
    """Best-effort: read the pod's own DUT-facing pin assignments, or None."""
    from pod.client import Pod
    try:
        return Pod.from_entry(entry).pinmap()
    except Exception:  # noqa: BLE001 - enrollment proceeds without the pinmap
        return None


def probe_fingerprint(entry):
    """Best-effort: resolve a live target for the entry and read its fingerprint."""
    from pod.target import TargetResolver, read_fingerprint
    r = TargetResolver(
        hostname=entry.get("hostname"), addr6=entry.get("addr6"),
        addr4=entry.get("addr4"), repl_port=entry.get("repl_port", 8266))
    try:
        host = r.resolve()
    except Exception:  # noqa: BLE001 - enrollment proceeds without a fingerprint
        return None
    return read_fingerprint(host, entry.get("repl_port", 8266))


def register_discovered(label, match=None, timeout=5.0, probe=True, force=False,
                        extra=None):
    """Browse mDNS, match a pod, and register its handles under `label`.

    Raises ValueError if the label exists and force is False, or LookupError if
    no single pod matches. Returns the stored entry.
    """
    existing = get_pod(label)
    if existing is not None and not force:
        raise ValueError(
            "Label '%s' already registered. Use force to overwrite." % label)
    pods = discover_pods(timeout=timeout)
    info = match_discovered(pods, label, match)
    if info is None:
        raise LookupError(
            "No single pod matching '%s' found via mDNS." % (match or label))
    entry = entry_from_podinfo(info)
    if extra:
        entry.update(extra)
    if probe:
        fp = probe_fingerprint(entry)
        if fp:
            entry["fingerprint"] = fp
        pins = read_pinmap(entry)
        if pins:
            entry["pins"] = pins
    carry_over(existing, entry)
    set_pod(label, entry)
    return entry
