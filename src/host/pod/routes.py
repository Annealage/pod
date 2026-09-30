"""Per-channel routes for a registry entry.

An entry describes a DUT and, per channel, how the host reaches it. A pod is one
possible route rather than the entry's type. A channel with no declared route
defaults to the pod, so entries written before routes existed resolve exactly as
they did.

Channels and their routes:

``debug``        ``pod`` (on-pod SWD stack) or ``pyocd`` (host-attached probe)
``uart``         ``pod`` (TCP bridge) or ``tty`` (a host serial device)
``usb``          ``pod`` or ``pod-host`` (usbip forward) or ``agent-direct`` (DUT USB on the host)
``instruments``  ``pod`` (this entry's pod, or a named other pod) or ``none``
"""

from typing import Optional

CHANNELS = ("debug", "uart", "usb", "instruments")

_ROUTES = {
    "debug": ("pod", "pyocd"),
    "uart": ("pod", "tty"),
    "usb": ("pod", "pod-host", "agent-direct"),
    "instruments": ("pod", "none"),
}

POD_HANDLE_KEYS = ("hostname", "addr4", "addr6", "address")


class RouteError(ValueError):
    """A route declaration is malformed or names an unknown route."""


def route(entry: dict, channel: str) -> dict:
    """Return the resolved route for a channel as a dict with a ``via`` key.

    Extra keys (``uid``, ``tty``, ``baud``, ``pod``) are carried from the
    declaration. ``usb`` is read from the entry's ``dut.usb.conn`` when no
    top-level ``usb`` route is declared.
    """
    if channel not in _ROUTES:
        raise RouteError("unknown channel %r (expected one of %s)"
                         % (channel, ", ".join(CHANNELS)))
    declared = entry.get(channel)
    if channel == "usb" and declared is None:
        conn = ((entry.get("dut") or {}).get("usb") or {}).get("conn")
        declared = {"via": conn} if conn else None
    if declared is None:
        return {"via": "pod"}
    if not isinstance(declared, dict) or "via" not in declared:
        raise RouteError("%s route must be an object with a 'via' key" % channel)
    if declared["via"] not in _ROUTES[channel]:
        raise RouteError("%s route %r is not one of %s"
                         % (channel, declared["via"], ", ".join(_ROUTES[channel])))
    return dict(declared)


def has_pod_handles(entry: dict) -> bool:
    """True when the entry carries any way to reach a pod."""
    return any(entry.get(k) for k in POD_HANDLE_KEYS)


def is_local_bench(entry: dict) -> bool:
    """A bench with no way to reach a pod: nothing to discover or connect to."""
    return not has_pod_handles(entry)


def instruments_pod(entry: dict, label: str) -> Optional[str]:
    """The registry label that provides instruments, or None for no instruments.

    A ``pod`` route with a ``pod`` key names another entry; without one it is
    this entry's own pod, provided the entry has pod handles.
    """
    r = route(entry, "instruments")
    if r["via"] == "none":
        return None
    if r.get("pod"):
        return r["pod"]
    return label if has_pod_handles(entry) else None


def dut_direct_tty(entry: dict) -> Optional[str]:
    """The host serial device of a DUT whose USB is plugged into the host."""
    usb = (entry.get("dut") or {}).get("usb") or {}
    if route(entry, "usb")["via"] == "agent-direct":
        return usb.get("tty")
    return None
