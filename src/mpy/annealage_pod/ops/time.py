# Annealage Pod: time-sync wrapper.
#
# Spec.md §6.3: MP-managed NTP sync via ntptime.settime() against a
# configurable server. Time sync is not required for the annealage_pod to
# function; before sync, callers should use monotonic uptime.

try:
    import ntptime as _ntptime
except ImportError:
    _ntptime = None


_DEFAULT_SERVER = "pool.ntp.org"


def sync(ntp_server=_DEFAULT_SERVER, timeout_s=5):
    """Run an NTP sync against `ntp_server`. Returns True on success."""
    if _ntptime is None:
        return False
    try:
        _ntptime.host = ntp_server
        if hasattr(_ntptime, "timeout"):
            _ntptime.timeout = timeout_s
        _ntptime.settime()
        return True
    except OSError:
        return False


def server():
    """Return the server most recently set on ntptime.host (if any)."""
    if _ntptime is None:
        return None
    return getattr(_ntptime, "host", None)
