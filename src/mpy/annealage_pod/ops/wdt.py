# Annealage Pod: task watchdog wrapper.
#
# Spec.md §6.2 says the long-running C-module tasks subscribe to
# `esp_task_wdt` and the MP main task resets it from its asyncio loop.
# C-module tasks that subscribe do that from C; this MP-side wrapper
# only has to provide a way for the asyncio tick to register and
# kick the task watchdog from MP.
#
# MicroPython exposes `machine.WDT` which on the esp32 port maps to
# the ESP-IDF task watchdog with a configurable timeout. That gives
# us subscribe/feed without touching IDF directly.

try:
    from machine import WDT
except ImportError:
    WDT = None


_DEFAULT_TIMEOUT_MS = 30_000


_wdt = None


def subscribe(timeout_ms=_DEFAULT_TIMEOUT_MS):
    """Subscribe the MP main task to the watchdog with `timeout_ms` deadline."""
    global _wdt
    if WDT is None:
        return None
    if _wdt is None:
        _wdt = WDT(timeout=timeout_ms)
    return _wdt


def kick():
    """Reset the watchdog. Call this from the asyncio loop tick."""
    if _wdt is None:
        return False
    _wdt.feed()
    return True


def is_subscribed():
    """Return True iff the watchdog has been subscribed."""
    return _wdt is not None
