# Who currently holds each contended pod resource (workstream: conflict legibility).
#
# A holder RECORD, not a lease. You become the holder by USING a resource; holding
# is a fact observed, not a right granted. There is no TTL, no renewal and no steal
# protocol, because liveness comes from the resource itself: when the REPL socket
# closes or the usbip import drops, the code that noticed calls drop() and the
# record goes with it. That is what keeps a zombie holder from outliving its
# holder, which is the failure a lease has to work to avoid.
#
# The record exists so a refusal can NAME someone. A pod that refuses without
# saying who holds it looks like a broken pod, and the documented response to a
# broken pod is reset and power-cycle, which against a pod that is merely in use
# destroys another agent's session.
#
# Deliberately not a security boundary: `caller` is a label nobody checks, and a
# human at the REPL is god-mode and always wins.
#
# RAM only. A reboot means a clean bench, for the same reason the lease is RAM
# only: a holder record surviving a reboot with no live holder is worse than no
# record at all.
#
# pio and peripherals are NOT duplicated here. They already have registries
# (pio_arbiter._claims, peripherals._INST) and who() reads through to those, so
# each fact lives in exactly one place.

import time

# resource -> (caller, since_ms, detail)
_HELD = {}

# Cumulative call counts, for diagnosing a record that appears to come and go:
# they distinguish "drop is firing" from "the reader is racing".
_STATS = {"note": 0, "drop": 0}

# Resources this module records directly. pio and peripherals are read through.
RESOURCES = ("repl", "usbip", "swd")


def note(resource, caller, detail=""):
    """Record that `caller` now holds `resource`. Idempotent for the same caller."""
    _HELD[resource] = (caller, time.ticks_ms(), detail)
    _STATS["note"] += 1
    return True


def drop(resource):
    """Clear `resource`. Returns True if something was held."""
    gone = _HELD.pop(resource, None) is not None
    if gone:
        _STATS["drop"] += 1
    return gone


def _age_s(since_ms):
    return time.ticks_diff(time.ticks_ms(), since_ms) // 1000


def _read_through():
    """Holders that live in someone else's registry, read at call time."""
    out = {}
    try:
        from .debug import pio_arbiter
        for key, owner in pio_arbiter._claims.items():
            # key is (block, sm); owner is the subsystem name
            out["pio:%s.%s" % key] = {"caller": owner, "since_s": None,
                                      "detail": "pio_arbiter claim"}
    except Exception:  # noqa: BLE001 - absent or not yet imported
        pass
    try:
        from . import peripherals
        for name in peripherals._INST:
            out["peripherals:%s" % name] = {"caller": name, "since_s": None,
                                            "detail": "live instance"}
    except Exception:  # noqa: BLE001
        pass
    return out


def who(resource=None):
    """Who holds what.

    With no argument, every held resource including the read-through registries.
    With one, just that resource, or {} when nobody holds it. since_s is None for
    read-through entries: those registries do not record when the claim was made.
    """
    direct = {}
    for key, rec in _HELD.items():
        direct[key] = {"caller": rec[0], "since_s": _age_s(rec[1]),
                       "detail": rec[2]}
    if resource is not None:
        return {resource: direct[resource]} if resource in direct else {}
    direct.update(_read_through())
    return direct


def clear():
    """Forget every directly-held record. For tests and a clean re-init."""
    _HELD.clear()
