# Annealage Pod: time-sync wrapper.
#
# Spec §6.3: MP-managed NTP sync via ntptime.settime() against a
# configurable server. Time sync is not required for the annealage_pod to
# function; before sync, callers should use monotonic uptime.
#
# WS-H scope intentionally leaves this as pure MicroPython. The
# `ntptime` module (frozen in MP's esp32 port) calls
# `settimeofday()` via mphalport, which is sufficient
# granularity (seconds) for SWO/INA228 sample stitching. No C shim
# is added here; the C-shim hook below stays absent so callers see
# a uniform pure-MP path on every target.

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
