# Annealage Pod: opto-coupled relay 1..7 control.
#
# Backs the seven G3VM/GAQY221S MOSFET-relays on the carrier (spec.md
# §3.4 reset/relay path, Appendix A §A.5.1). Each relay is driven by
# one S3 GPIO; high turns the LED on which closes the contact pair.
#
# The MicroPython side owns this surface; no C glue. All public
# methods are synchronous; pulse timing uses time.sleep_ms.

import time

try:
    from machine import Pin
except ImportError:
    Pin = None

from . import _pinmap


_RELAY_NUMBERS = (1, 2, 3, 4, 5, 6, 7)


class _SoftPin:
    """Plain-Python stand-in for machine.Pin used on the Unix port.

    Holds a single 0/1 state in software. Valid on hosts where
    machine.Pin is unavailable so that the relay logic stays unit-
    testable without hardware.
    """

    def __init__(self, gpio, value=0):
        self._gpio = gpio
        self._v = 1 if value else 0

    def value(self, *args):
        if not args:
            return self._v
        self._v = 1 if args[0] else 0
        return None


class _Relay:
    """One opto-coupled relay (1..7)."""

    def __init__(self, number, pin):
        self._number = number
        self._pin = pin

    def set(self, on):
        """Drive the relay to `on` (True = closed). Returns True iff state changed."""
        # The relay GPIOs (GP1-GP7) are ESP32-S3 carrier numbers; on the RP2350
        # they are unported and overlap other pod functions, so refuse to drive a
        # relay rather than silently toggle a do-nothing software pin.
        _pinmap.assert_esp32_carrier("relays")
        prior = bool(self._pin.value())
        new = bool(on)
        if prior == new:
            return False
        self._pin.value(1 if new else 0)
        return True

    def get(self):
        """Return True iff the relay is currently closed."""
        return bool(self._pin.value())

    def pulse(self, initial_closed, durations_ms):
        """Drive `initial_closed`, then for each ms in durations_ms toggle.

        Used for double-tap reset patterns (NRF, SAMD, RP2 BOOTSEL).
        """
        self.set(initial_closed)
        state = bool(initial_closed)
        for ms in durations_ms:
            if hasattr(time, "sleep_ms"):
                time.sleep_ms(int(ms))
            else:
                time.sleep(ms / 1000.0)
            state = not state
            self._pin.value(1 if state else 0)


class _RelayBank:
    """Container for the seven relays plus batched and pulse helpers."""

    def __init__(self):
        self._pins = {}
        self._relays = {}
        for n in _RELAY_NUMBERS:
            if Pin is not None and not _pinmap._IS_RP2350:
                p = Pin(_pinmap.RELAY_GPIO[n], Pin.OUT, value=0)
            else:
                # Unix port (Pin is None) and RP2350 both get a software-only pin
                # so import-time succeeds; on the RP2350 the relay GPIOs are
                # unported ESP32-S3 numbers and _Relay.set() refuses to drive them.
                p = _SoftPin(_pinmap.RELAY_GPIO[n], value=0)
            self._pins[n] = p
            self._relays[n] = _Relay(n, p)

    def __getitem__(self, n):
        return self._relays[n]

    def numbers(self):
        """Return the tuple of valid relay numbers (1..7)."""
        return _RELAY_NUMBERS

    def get(self, n):
        """Return True iff relay `n` is closed."""
        return self._relays[n].get()

    def set(self, n, on):
        """Set relay `n` to `on`. Returns True iff state changed."""
        return self._relays[n].set(on)

    def batch(self, close=None, open=None):
        """Close listed relays, open listed relays, in one call.

        Returns True iff at least one relay state changed.
        """
        changed = False
        if close:
            for n in close:
                if self._relays[n].set(True):
                    changed = True
        if open:
            for n in open:
                if self._relays[n].set(False):
                    changed = True
        return changed

    def pulse(self, n, initial_closed, durations_ms):
        """Pulse relay `n` per the durations list (see _Relay.pulse)."""
        self._relays[n].pulse(initial_closed, durations_ms)

    def all_off(self):
        """Open every relay. Returns True iff at least one changed."""
        return self.batch(open=_RELAY_NUMBERS)


# Module-level singleton; on Unix port (Pin is None) the bank is empty
# so import-time succeeds for unit tests.
relays = _RelayBank()
