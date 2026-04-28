# Annealage Pod: DUT reset paths.
#
# Four independent paths from spec.md §3.4:
#   - 'swd':   AIRCR.SYSRESETREQ via the synthetic CMSIS-DAP probe
#              (calls into the C dapprobe module).
#   - 'nrst':  toggles the open-drain nRST GPIO through the
#              fixed-direction translator.
#   - 'power': calls power.vtarget.cycle() with the configured
#              off-time.
#   - 'relay': pulses one of the 7 opto relays for a configured
#              duration. Used for BOOTSEL/BOOT0 patterns.
#
# All paths are synchronous. SWD path is a thin wrapper; if dapprobe
# does not yet expose a system-reset RPC the call is a logged TODO.

import time

try:
    from machine import Pin
except ImportError:
    Pin = None

from . import _pinmap, power, relays


_NRST_PULSE_MS = 50
_RELAY_PULSE_MS_DEFAULT = 100
_POWER_OFF_MS_DEFAULT = 200


_nrst_pin = None


def _nrst():
    global _nrst_pin
    if _nrst_pin is None:
        if Pin is not None:
            # Open-drain through translator. We drive low to assert reset
            # and release (high-Z) to deassert.
            _nrst_pin = Pin(_pinmap.NRST, Pin.OPEN_DRAIN, value=1)
        else:
            # Software stand-in for unit tests on the Unix port.
            from .relays import _SoftPin

            _nrst_pin = _SoftPin(_pinmap.NRST, value=1)
    return _nrst_pin


def _sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(int(ms))
    else:
        time.sleep(ms / 1000.0)


def _reset_swd():
    """Issue an AIRCR.SYSRESETREQ via the C dapprobe module.

    dapprobe currently exposes only `start()`; the system_reset RPC
    will land with WS-C. Until then we log and fall back to no-op.
    """
    try:
        import dapprobe  # type: ignore
    except ImportError:
        print("annealage_pod.dut.reset(swd): dapprobe C module not present")
        return False
    sys_reset = getattr(dapprobe, "system_reset", None)
    if sys_reset is None:
        # TODO(WS-C): replace with real dapprobe.system_reset() once
        # the C module exposes it.
        print("annealage_pod.dut.reset(swd): dapprobe.system_reset() not yet implemented")
        # Trigger a start() call so the C side at least registers.
        start = getattr(dapprobe, "start", None)
        if start is not None:
            start()
        return False
    sys_reset()
    return True


def _reset_nrst():
    """Drive nRST low for _NRST_PULSE_MS, then release."""
    pin = _nrst()
    pin.value(0)
    _sleep_ms(_NRST_PULSE_MS)
    pin.value(1)
    return True


def _reset_power(off_ms):
    """Cycle VTARGET via power.vtarget.cycle()."""
    power.vtarget.cycle(off_ms=off_ms)
    return True


def _reset_relay(n, pulse_ms):
    """Pulse relay `n` closed for `pulse_ms` then open."""
    if n is None:
        raise ValueError("dut.reset(mode='relay') requires relay=N (1..7)")
    if n not in relays.relays.numbers():
        raise ValueError("dut.reset(mode='relay') relay must be 1..7, got {}".format(n))
    relays.relays.pulse(n, True, [pulse_ms])
    return True


def reset(mode="swd", relay=None, off_ms=_POWER_OFF_MS_DEFAULT, pulse_ms=_RELAY_PULSE_MS_DEFAULT):
    """Reset the DUT via one of the four configured paths.

    mode: 'swd', 'nrst', 'power', 'relay'.
    relay: relay number 1..7 (only for mode='relay').
    off_ms: VTARGET off-time for mode='power' (default 200 ms).
    pulse_ms: relay closed-time for mode='relay' (default 100 ms).
    """
    if mode == "swd":
        return _reset_swd()
    if mode == "nrst":
        return _reset_nrst()
    if mode == "power":
        return _reset_power(off_ms)
    if mode == "relay":
        return _reset_relay(relay, pulse_ms)
    raise ValueError("dut.reset() unknown mode: {!r}".format(mode))
