# DUT reset over the dedicated nRST line, and the boot-time parking that keeps
# that line safe when nobody is using it.
#
# The pod drives the DUT's reset input open-drain: pull low to assert reset,
# release to high-Z and let the DUT's own pull-up deassert it. The pin is
# annealage_pod._rp2_pinmap.NRST, so this module never hard-codes a GPIO number.
#
# PARKING IS NOT OPTIONAL. An RP2350 pad comes out of reset with its internal
# pull-down enabled, so a GPIO that no code has configured is not high-impedance
# - it actively pulls its net down. Wired to a DUT reset input, that pull-down
# fights the DUT's reset pull-up, and on any DUT whose pull-up is weaker than the
# pad's (roughly 50-80k) it wins and holds the DUT in reset from the moment the
# pod powers on, with no code having asked for a reset. park() therefore has to
# run during boot, before anything else touches the DUT; see netboot.main().
# Measured on an RP2350B pod wired to an i.MX RT1052: pull-down enabled read
# low (DUT held in reset), pull-down cleared read high (DUT ran).
#
# This is deliberately separate from annealage_pod.dut, which is the ESP32-S3
# carrier's four-path reset API and depends on carrier hardware (relays, VTARGET
# switching) that a bare pod does not have. Here there is one wire and no
# carrier.

import time

from machine import Pin

from .. import _rp2_pinmap

# How long to hold the line low. Comfortably longer than the reset-pulse minimum
# of the parts the pod targets, and short enough to stay interactive.
PULSE_MS = 50

# Settling time after release before the caller may assume the DUT is running.
RELEASE_SETTLE_MS = 10


def _pin(value=1):
    # OPEN_DRAIN with value=1 leaves the output driver off, so the pin is high-Z
    # and the DUT's pull-up holds the line. Constructing the Pin also clears the
    # power-on pull-down, which is the whole point of park().
    return Pin(_rp2_pinmap.NRST, Pin.OPEN_DRAIN, value=value)


def park():
    """Put the DUT reset line in its safe idle state (released, high-Z).

    Idempotent, and safe to call when no DUT is wired. Returns the pin level
    read back after parking: 1 means the line is high (DUT not held in reset),
    0 means something else is holding it down - a DUT with no reset pull-up, a
    DUT holding its own reset, or a wiring fault.
    """
    p = _pin(1)
    time.sleep_ms(RELEASE_SETTLE_MS)
    return p.value()


def assert_reset():
    """Drive the DUT reset line low and leave it asserted."""
    _pin(1).value(0)
    return True


def release():
    """Release the DUT reset line (high-Z) and let the DUT run."""
    p = _pin(1)
    p.value(1)
    time.sleep_ms(RELEASE_SETTLE_MS)
    return p.value()


def pulse(ms=PULSE_MS):
    """Assert reset for `ms`, then release. Returns the line level after release.

    A return of 0 means the line did not come back up: either the DUT has no
    reset pull-up (the pod cannot deassert a line nothing pulls high) or the
    DUT is holding its own reset.
    """
    p = _pin(1)
    p.value(0)
    time.sleep_ms(ms)
    p.value(1)
    time.sleep_ms(RELEASE_SETTLE_MS)
    return p.value()
