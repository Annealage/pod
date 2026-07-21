# Annealage Pod RP2350: single source of truth for the pod's own DUT-facing
# GPIO assignments. The host queries this (`pod pins`) and the on-pod modules
# default from it, so "which pod pin is SWCLK / the I2C-target SDA" has exactly
# one answer. DUT-SIDE wiring - which DUT pin connects to each of these - is
# harness metadata declared per pod in the registry DUT block (dut.wiring), not
# here: software cannot know it.
#
# Values per docs/pod/hardware-setup.md. PIO map (pio_arbiter.PIO_MAP):
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

# DUT UART bridge (machine.UART1 on hardware UART, GP4/GP5). SUGGESTED-untested
# per hardware-setup.md; confirm wiring before treating as VERIFIED. GP4/GP5 are
# the RP2 port's default UART1 pins; UART0 (GP0/GP1) is the backup REPL console
# and is a separate controller not touched here.
DUT_UART_NUM = 1
DUT_UART_TX = 4
DUT_UART_RX = 5

# DUT SPI target (PIO0, pod is the SPI peripheral). SUGGESTED-untested per
# hardware-setup.md; confirm wiring before treating as VERIFIED. Reuses the
# SPI0 block sketch: MISO GP16, CS GP17, SCK GP18, MOSI GP19. PIO imposes no
# contiguity constraint (MOSI/MISO use in_base/out_base; SCK/CS are absolute
# wait gpio). Overlaps the LA default block GP16-21, but SPI target and LA are
# mutually exclusive on PIO0, so the overlap costs nothing.
DUT_SPI_MISO = 16
DUT_SPI_MOSI = 19
DUT_SPI_SCK = 18
DUT_SPI_CS = 17


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
        "dut_uart": {"num": DUT_UART_NUM, "tx": DUT_UART_TX, "rx": DUT_UART_RX},
        "spi_target": {"miso": DUT_SPI_MISO, "mosi": DUT_SPI_MOSI,
                       "sck": DUT_SPI_SCK, "cs": DUT_SPI_CS},
    }
