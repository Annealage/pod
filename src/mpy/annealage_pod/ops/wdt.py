# Annealage Pod: task watchdog wrapper.
#
# Spec §6.2: the long-running C-module tasks subscribe to
# `esp_task_wdt` and the MP main task resets it from its asyncio
# tick. WS-H provides a C shim (`ops_wdt`) that wraps the IDF API
# directly. WS-E left a pure-MP fallback using `machine.WDT` for
# the Unix port and dev boards; this module prefers the shim and
# falls back to `machine.WDT` only when the shim is absent.

try:
    import ops_wdt as _ops_wdt_c
except ImportError:
    _ops_wdt_c = None

try:
    from machine import WDT as _WDT
except ImportError:
    _WDT = None


_DEFAULT_TIMEOUT_MS = 30_000


# Pure-MP fallback state. Only populated when _ops_wdt_c is None.
_wdt_fallback = None
_subscribed_via_shim = False


def subscribe(timeout_ms=_DEFAULT_TIMEOUT_MS):
    """Subscribe the calling task to the watchdog with `timeout_ms`.

    Idempotent. On the ESP32 build with the C shim available, calls
    ops_wdt.subscribe directly. On Unix or builds lacking the shim,
    falls back to machine.WDT (which on the esp32 port also drives
    esp_task_wdt internally).
    """
    global _wdt_fallback, _subscribed_via_shim
    if _ops_wdt_c is not None:
        _ops_wdt_c.subscribe(timeout_ms=timeout_ms)
        _subscribed_via_shim = True
        return _ops_wdt_c
    if _WDT is None:
        return None
    if _wdt_fallback is None:
        _wdt_fallback = _WDT(timeout=timeout_ms)
    return _wdt_fallback


def kick():
    """Reset the watchdog. Call this from the asyncio loop tick."""
    if _subscribed_via_shim and _ops_wdt_c is not None:
        _ops_wdt_c.kick()
        return True
    if _wdt_fallback is None:
        return False
    _wdt_fallback.feed()
    return True


def unsubscribe():
    """Unsubscribe the calling task from the watchdog.

    Only the C-shim path supports unsubscribe; machine.WDT does not
    expose deinit. Returns True on success, False if the shim is
    absent or the task was not subscribed.
    """
    global _subscribed_via_shim
    if _subscribed_via_shim and _ops_wdt_c is not None:
        try:
            _ops_wdt_c.unsubscribe()
        except OSError:
            return False
        _subscribed_via_shim = False
        return True
    return False


def is_subscribed():
    """Return True iff the watchdog has been subscribed by this task."""
    if _ops_wdt_c is not None and hasattr(_ops_wdt_c, "is_subscribed"):
        try:
            return bool(_ops_wdt_c.is_subscribed())
        except OSError:
            return False
    return _wdt_fallback is not None
