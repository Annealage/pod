# Annealage Pod: GPIO assignment table.
#
# Single source of truth on the MicroPython side for the pins listed in
# docs/esp32-s3/spec-appendix-A-pinmap.md (§A.5.1). Other annealage_pod.* modules
# import these constants rather than hard-coding GPIO numbers locally.
#
# Values match the appendix exactly. Keep this in sync with appendix
# A on every PCB rev.
#
# PLATFORM SCOPE: these numbers are the ESP32-S3 carrier map. They are only
# valid on the ESP32-S3 pod (GP0-GP48). The RP2350 Pico 2 W pod has a different,
# much smaller pin budget (GP0-GP22, GP26-GP28) and a different cluster: SWD is
# GP14/GP15 and the local I2C is GP10/GP11 (docs/rp2350/hardware-setup.md). The
# `power`/`relays`/`dut`/`carrier`/`compat` cluster that imports this map is
# unported ESP32-S3 carrier code (hardware-setup.md §8). The live RP2350 boot
# path (board main.py -> netboot.start()) and the on-pod debug/peripherals
# modules do NOT import this map, so it is latent there - but a manual or future
# import of `dut`/`power`/etc. on the RP2350 must not silently receive ESP32-S3
# GPIO numbers. The acute hazard is NRST: the ESP32-S3 value 14 equals SWDIO
# (GP14) on the RP2350, so a `dut.reset(mode='nrst')` would seize the live SWD
# data line. We therefore override NRST to a documented-safe free pin on the
# RP2350 and leave the rest of the (unported) carrier numbers in place but
# clearly scoped as ESP32-S3-only.
import sys as _sys

_IS_RP2350 = _sys.platform == "rp2"

# Relay drives: opto-coupler low-side switches.
RELAY_GPIO = {
    1: 1,
    2: 2,
    3: 3,
    4: 4,
    5: 5,
    6: 6,
    7: 7,
}

# Local I2C bus (shared by INA228 monitors and carrier-ID EEPROM).
LOCAL_I2C_SDA = 8
LOCAL_I2C_SCL = 9

# SWD pins. Owned by the C `dapprobe` module; listed for reference.
SWCLK = 10
SWDIO = 11
SWDIO_DIR = 12
SWO = 13

# DUT reset (open-drain, through fixed-direction translator).
#
# RP2350: GP14 is SWDIO on this board, so the ESP32-S3 value (14) would drive the
# live SWD data line. Use the documented free DUT-nRST pin GP13 instead, which
# sits next to the SWD cluster (hardware-setup.md §5g). This guarantees NRST can
# never equal an SWD pin (GP14/GP15) on any RP2350 code path.
NRST = 13 if _IS_RP2350 else 14

# DUT UART (forwarded by C `uartbridge` / `uartcdc`).
DUT_UART_NUM = 2
DUT_UART_TX = 17
DUT_UART_RX = 18

# DUT slave I2C / SPI (mutually exclusive, owned by C `slaveio`).
DUT_I2C_SDA_OR_SPI_MOSI = 17
DUT_I2C_SCL_OR_SPI_SCK = 18
DUT_I2C_SDA_DIR = 21
DUT_SPI_MISO = 38
DUT_SPI_CS = 39

# USB-OTG host (DUT USB pass-through).
DUT_USB_DP = 19
DUT_USB_DM = 20

# Power rail enables (TPS2595 EN/UVLO).
VTARGET_EN = 40
DUT_USB_VBUS_EN = 41

# VBUS analog sense pin. Per Appendix A's ADC2/Wi-Fi conflict
# workaround the runtime path reads VBUS via the INA228 voltage
# register; the GPIO is reserved here for completeness only.
VBUS_SENSE = 42

# Console UART0 (CH340N onboard bridge).
UART0_TX = 43
UART0_RX = 44

# Status LEDs (strapping-pin friendly default-off loads).
LED_STATUS_1 = 45
LED_STATUS_2 = 46

# Spare general-purpose DUT IOs (carrier pins 31, 32; v0.7 GPD0/GPD1).
GPD0 = 47
GPD1 = 48

# I2C bus addresses on the local 3v3 bus.
INA228_ADDR_VTARGET = 0x40
INA228_ADDR_DUT_USB = 0x41
CARRIER_EEPROM_ADDR = 0x50


def assert_esp32_carrier(what):
    """Guard the ESP32-S3-only carrier GPIO numbers against use on the RP2350.

    The relay / I2C / power-enable / LED / UART / spare-IO numbers above are the
    ESP32-S3 carrier assignments. On the RP2350 they are wrong: some are out of
    range (GP40-GP48) and others (GP1-GP11) overlap real RP2350 pod functions
    such as the local I2C (GP10/GP11) and the SWD cluster. The carrier cluster
    (power/relays/carrier/compat) is unported ESP32-S3 code; until it is ported
    to RP2350 pins, constructing those pins on the RP2350 must fail loudly rather
    than silently seize the wrong line.

    Call this at the point an ESP32-S3 GPIO number is about to be handed to a real
    machine.Pin/I2C. NRST is handled separately (it is remapped to a safe pin
    above) so that the RP2350 nRST reset path stays usable.
    """
    if _IS_RP2350:
        raise RuntimeError(
            "annealage_pod._pinmap: {} uses the ESP32-S3 carrier GPIO map, which "
            "is not ported to the RP2350 (GP0-GP22, GP26-GP28). The "
            "power/relays/carrier/compat cluster is ESP32-S3-only; see "
            "docs/rp2350/hardware-setup.md section 8.".format(what)
        )
