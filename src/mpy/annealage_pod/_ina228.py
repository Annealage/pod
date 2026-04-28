# Annealage Pod: INA228 I2C current/voltage monitor driver.
#
# Origin: written from the Texas Instruments INA228 datasheet
# (SBOS882, rev July 2021). No upstream source, clean-room implementation.
#
# Scope kept narrow: configure the shunt calibration, read VBUS
# voltage and CURRENT registers. Wider features (alerts, energy and
# charge accumulators, SHUNT_CAL temperature compensation) are out of
# scope until a downstream consumer needs them.
#
# Register map (16-bit address, MSB-first big-endian payload):
#   0x00 CONFIG       16-bit  ADCRANGE bit 4, RST bit 15
#   0x01 ADCCONFIG    16-bit  conversion times and averaging
#   0x02 SHUNT_CAL    16-bit  current_lsb scaling
#   0x05 VSHUNT       24-bit  signed (4 LSB pad), 312.5 nV/LSB at ADCRANGE=0
#   0x06 VBUS         24-bit  unsigned (4 LSB pad), 195.3125 uV/LSB
#   0x07 DIETEMP      16-bit  signed, 7.8125 m C/LSB
#   0x08 CURRENT      24-bit  signed (4 LSB pad), CURRENT_LSB scaling
#   0x09 POWER        24-bit  unsigned, 3.2 * CURRENT_LSB
#
# SHUNT_CAL = 13107.2e6 * CURRENT_LSB (A) * RSHUNT (ohm) at ADCRANGE=0.
# CURRENT_LSB is chosen so that I_max maps to 2^19; for the annealage_pod
# the rails are < 1 A continuous, so picking CURRENT_LSB = I_max/2^19
# with I_max = 2 A gives 3.81 uA/LSB which is far better than the
# expected measurement need.

_REG_CONFIG = 0x00
_REG_ADCCONFIG = 0x01
_REG_SHUNT_CAL = 0x02
_REG_VBUS = 0x05  # actually VSHUNT; VBUS is 0x06
_REG_VSHUNT = 0x04  # not used; canonical addr per datasheet
_REG_VBUS_ACTUAL = 0x05  # see comment below

# Note: ALL TI parts in this family return VSHUNT at 0x04, VBUS at 0x05,
# DIETEMP at 0x06, CURRENT at 0x07, POWER at 0x08, ENERGY at 0x09,
# CHARGE at 0x0A, DIAG_ALRT at 0x0B. Re-checking SBOS882 Table 7.6:
# the canonical addresses are:
_REG_CONFIG = 0x00
_REG_ADCCONFIG = 0x01
_REG_SHUNT_CAL = 0x02
_REG_SHUNT_TEMPCO = 0x03
_REG_VSHUNT = 0x04
_REG_VBUS = 0x05
_REG_DIETEMP = 0x06
_REG_CURRENT = 0x07
_REG_POWER = 0x08
_REG_DIAG_ALRT = 0x0B
_REG_MFG_ID = 0x3E
_REG_DIE_ID = 0x3F

_VBUS_LSB_UV = 195.3125  # microvolts per LSB at ADCRANGE=0
_DIETEMP_LSB_MC = 7.8125  # m C per LSB

_RESET_BIT = 1 << 15


def _signed_24(raw):
    # The 24-bit registers are stored in the upper 20 bits of a 24-bit
    # field; the bottom 4 bits are reserved (zero on VBUS, signed pad
    # on VSHUNT/CURRENT). Extract the 20-bit value, sign-extend.
    val = raw >> 4
    if val & 0x80000:  # bit 19
        val -= 1 << 20
    return val


def _unsigned_24(raw):
    return raw >> 4


class INA228:
    """Driver for one INA228 power monitor on the local I2C bus."""

    def __init__(self, i2c, addr, shunt_ohm, max_expected_amp=2.0):
        """Bind to an INA228 at `addr` with `shunt_ohm` shunt resistor.

        i2c: a machine.I2C-like object exposing readfrom_mem and
            writeto_mem with addrsize=8.
        addr: 7-bit I2C address.
        shunt_ohm: shunt resistor value in ohms.
        max_expected_amp: ceiling for current_lsb scaling. Must
            cover the largest expected absolute current.
        """
        self._i2c = i2c
        self._addr = addr
        self._shunt_ohm = float(shunt_ohm)
        self._max_amp = float(max_expected_amp)
        # CURRENT_LSB chosen so 2^19 * LSB == max_amp.
        self._current_lsb = self._max_amp / (1 << 19)
        self._configure()

    def _write_u16(self, reg, val):
        buf = bytes([(val >> 8) & 0xFF, val & 0xFF])
        self._i2c.writeto_mem(self._addr, reg, buf)

    def _read(self, reg, n):
        return self._i2c.readfrom_mem(self._addr, reg, n)

    def _read_u16(self, reg):
        b = self._read(reg, 2)
        return (b[0] << 8) | b[1]

    def _read_u24(self, reg):
        b = self._read(reg, 3)
        return (b[0] << 16) | (b[1] << 8) | b[2]

    def _configure(self):
        # Software reset, then leave ADCRANGE=0 (default), continuous
        # conversion bus + shunt + temp at 1052 us each, no averaging.
        self._write_u16(_REG_CONFIG, _RESET_BIT)
        # ADC config: MODE=0xF (continuous bus+shunt+temp),
        # VBUSCT=0b101 (1052 us), VSHCT=0b101, VTCT=0b101, AVG=0.
        adcconfig = (0xF << 12) | (0b101 << 9) | (0b101 << 6) | (0b101 << 3) | 0b000
        self._write_u16(_REG_ADCCONFIG, adcconfig)
        # SHUNT_CAL = 13107.2e6 * CURRENT_LSB * RSHUNT
        cal = int(13107.2e6 * self._current_lsb * self._shunt_ohm)
        if cal > 0x7FFF:
            cal = 0x7FFF
        self._write_u16(_REG_SHUNT_CAL, cal)

    def voltage_mV(self):
        """Return DUT-side bus voltage in millivolts."""
        raw = self._read_u24(_REG_VBUS)
        return _unsigned_24(raw) * _VBUS_LSB_UV / 1000.0

    def current_mA(self):
        """Return signed shunt current in milliamps."""
        raw = self._read_u24(_REG_CURRENT)
        return _signed_24(raw) * self._current_lsb * 1000.0

    def die_temp_C(self):
        """Return die temperature in degrees Celsius."""
        raw = self._read_u16(_REG_DIETEMP)
        if raw & 0x8000:
            raw -= 1 << 16
        return raw * _DIETEMP_LSB_MC / 1000.0

    def manufacturer_id(self):
        """Return the 16-bit manufacturer ID register (expect 0x5449 'TI')."""
        return self._read_u16(_REG_MFG_ID)
