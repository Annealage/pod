# Annealage Pod RP2350: single source of truth for the pod's own DUT-facing
# GPIO assignments. The host queries this (`pod pins`) and the on-pod modules
# default from it, so "which pod pin is SWCLK / the I2C-target SDA" has exactly
# one answer. DUT-SIDE wiring - which DUT pin connects to each of these - is
# harness metadata declared per pod in the registry DUT block (dut.wiring), not
# here: software cannot know it.
#
# Values per docs/rp2350/hardware-setup.md. PIO map (pio_arbiter.PIO_MAP):
# PIO2 is CYW43 Wi-Fi (off-limits), PIO1 is SWD, PIO0 is free (logic analyser).
# This is RP2350-only; the ESP32-S3 carrier map lives in _pinmap.py.

# SWD bit-bang (PIO1). Owned by the debug stack (swd_dap.DebugPort).
SWD_SWDIO = 14
SWD_SWCLK = 15

# DUT reset, open-drain. GP13 is chosen so nRST can never equal an SWD pin
# (GP14/GP15) on any code path.
NRST = 13

# Local hardware I2C target (machine.I2CTarget on I2C1), the DUT-as-controller
# personality. Bench default exercised by `pod i2c-target`.
I2C_TARGET_BUS = 1
I2C_TARGET_SCL = 11
I2C_TARGET_SDA = 10


def pinmap():
    """The pod's own DUT-facing pin assignments, for host introspection.

    Reports only the pod side. Pair it with the registry's dut.wiring (the
    DUT-side pins) for the full interconnect.
    """
    return {
        "swd": {"swdio": SWD_SWDIO, "swclk": SWD_SWCLK},
        "nrst": NRST,
        "i2c_target": {"bus": I2C_TARGET_BUS, "scl": I2C_TARGET_SCL,
                       "sda": I2C_TARGET_SDA},
    }
