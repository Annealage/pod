# Annealage Pod: VTARGET and DUT-USB rail control + INA228 telemetry.
#
# Two ramped rails (spec.md §3.3):
#   - VTARGET: 3v3 fixed, switched by TPS2595 (GPIO40), monitored by
#     an INA228 at 0x40.
#   - DUT-USB VBUS: 5 V, switched by TPS2595 (GPIO41), monitored by
#     an INA228 at 0x41. VBUS-present probe uses the INA228 voltage
#     register, not an ADC, per Appendix A's ADC2/Wi-Fi conflict
#     workaround (spec.md §8.8).
#
# Shunt resistor values (1 mohm) are placeholders matching the
# reference INA228 EVM; PCB design will pin actual values, override
# at instantiation time if they change.

import errno
import time

try:
    from machine import Pin, I2C
except ImportError:
    Pin = None
    I2C = None

from . import _pinmap
from ._ina228 import INA228


# Shunt resistor in ohms. PCB design currently uses 1 mohm shunts on
# both rails; if the BOM changes, edit here.
_SHUNT_OHM_VTARGET = 0.001
_SHUNT_OHM_DUT_USB = 0.001

# Threshold below which DUT-USB VBUS is considered absent.
_VBUS_PRESENT_THRESHOLD_MV = 4000.0

# Minimum off-time between DUT power off and on, per RP_INFRA's
# 2 second guard (Appendix B §B.3.3 DUT_POWER_OFF_TIME_MIN_S).
_DUT_OFF_MIN_MS = 2000


_i2c = None


def _local_i2c():
    """Return the lazily-initialised local I2C bus (or None on Unix)."""
    global _i2c
    if _i2c is None and I2C is not None:
        _i2c = I2C(0, sda=Pin(_pinmap.LOCAL_I2C_SDA), scl=Pin(_pinmap.LOCAL_I2C_SCL), freq=400_000)
    return _i2c


def _ms_now():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def _ms_diff(a, b):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(a, b)
    return a - b


def _sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(int(ms))
    else:
        time.sleep(ms / 1000.0)


class _Rail:
    """One switched, monitored DUT power rail."""

    def __init__(self, name, en_gpio, ina_addr, shunt_ohm):
        self._name = name
        self._en_gpio = en_gpio
        self._ina_addr = ina_addr
        self._shunt_ohm = shunt_ohm
        self._pin = None
        self._ina = None
        self._last_off_ms = None
        self._cycle_count = 0
        if Pin is not None:
            self._pin = Pin(en_gpio, Pin.OUT, value=0)
        else:
            # Software-only pin so unit tests on the Unix port can
            # drive on()/off()/cycle() and read is_on() back.
            from .relays import _SoftPin

            self._pin = _SoftPin(en_gpio, value=0)

    def _ina_or_none(self):
        if self._ina is None and I2C is not None:
            i2c = _local_i2c()
            if i2c is not None:
                try:
                    self._ina = INA228(i2c, self._ina_addr, self._shunt_ohm)
                except OSError:
                    self._ina = None
        return self._ina

    def is_measurable(self):
        """Return True iff the INA228 for this rail is reachable on I2C.

        When False, current_mA() / voltage_mV() / vbus_present() raise
        OSError rather than silently returning a zero sentinel that can
        be confused with a genuine zero reading. Caller uses this to
        report "rail measurement not available on this carrier" cleanly
        instead of falsely concluding the rail is off.
        """
        return self._ina_or_none() is not None

    def on(self):
        """Energise the rail."""
        self._pin.value(1)

    def off(self):
        """De-energise the rail and record the off timestamp."""
        self._pin.value(0)
        self._last_off_ms = _ms_now()

    def is_on(self):
        """Return True iff the enable pin reads high."""
        return bool(self._pin.value())

    def set(self, on):
        """Switch the rail; honours the 2 s minimum off-time on rising edge."""
        if on:
            if self._last_off_ms is not None:
                wait = _DUT_OFF_MIN_MS - _ms_diff(_ms_now(), self._last_off_ms)
                if wait > 0:
                    _sleep_ms(wait)
            self.on()
        else:
            self.off()

    def cycle(self, off_ms=200):
        """Power-cycle the rail with `off_ms` off-time between off and on."""
        self.off()
        _sleep_ms(off_ms)
        self.on()
        self._cycle_count += 1

    def cycle_count(self):
        """Return the number of cycle() invocations since boot."""
        return self._cycle_count

    def current_mA(self):
        """Return the rail current in milliamps.

        Raises OSError(ENODEV) if the INA228 for this rail is not reachable
        on I2C (carrier without populated INAs, wedged bus, etc.). Use
        is_measurable() to check up front if a clean fallback is preferred.
        """
        ina = self._ina_or_none()
        if ina is None:
            raise OSError(errno.ENODEV, "INA228 unavailable on this rail")
        return ina.current_mA()

    def voltage_mV(self):
        """Return the rail bus voltage in millivolts.

        Raises OSError(ENODEV) if the INA228 for this rail is not reachable
        on I2C. See is_measurable() for the cleanly-checkable predicate.
        """
        ina = self._ina_or_none()
        if ina is None:
            raise OSError(errno.ENODEV, "INA228 unavailable on this rail")
        return ina.voltage_mV()


class _DutUsbRail(_Rail):
    """DUT-USB rail: adds vbus_present() per Appendix A workaround."""

    def vbus_present(self):
        """Return True iff DUT-USB VBUS is above the 4 V detection threshold.

        Raises OSError(ENODEV) if the INA228 is not reachable - the caller
        cannot conclude "VBUS absent" from an absent measurement.
        """
        return self.voltage_mV() >= _VBUS_PRESENT_THRESHOLD_MV


vtarget = _Rail("vtarget", _pinmap.VTARGET_EN, _pinmap.INA228_ADDR_VTARGET, _SHUNT_OHM_VTARGET)
dut_usb = _DutUsbRail("dut_usb", _pinmap.DUT_USB_VBUS_EN, _pinmap.INA228_ADDR_DUT_USB, _SHUNT_OHM_DUT_USB)
