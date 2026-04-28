# Annealage Pod: GPIO assignment table.
#
# Single source of truth on the MicroPython side for the pins listed in
# docs/spec-appendix-A-pinmap.md (§A.5.1). Other annealage_pod.* modules
# import these constants rather than hard-coding GPIO numbers locally.
#
# Values match the appendix exactly. Keep this in sync with appendix
# A on every PCB rev.

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
NRST = 14

# DUT UART (forwarded by C `uartbridge`).
DUT_UART_TX = 15
DUT_UART_RX = 16

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
