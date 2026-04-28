# Annealage Pod: carrier identification.
#
# The carrier identifies itself via either an I2C EEPROM at the
# address from Appendix A (CARRIER_ID_SDA/SCL on the local bus, default
# 0x50) or via strap resistors read at boot. Spec.md §3.6.
#
# Two methods:
#   - id():           reads the EEPROM, returns a UTF-8 string (empty
#                     if no EEPROM is present or parsing fails).
#   - hw_version():   returns the carrier hardware version string.
#                     For the rev1 carrier this is "v0.7" matching the
#                     existing Octoprobe carrier; on revs that drive a
#                     strap-pin pattern this is computed at boot.

try:
    from machine import Pin, I2C
except ImportError:
    Pin = None
    I2C = None

from . import _pinmap


# Number of bytes to read from the EEPROM. Standard 24Cxx parts hold
# the carrier-id JSON or string in the first 64 bytes; trim trailing
# 0xFF / 0x00 padding.
_EEPROM_READ_LEN = 64


_i2c = None


def _local_i2c():
    global _i2c
    if _i2c is None and I2C is not None:
        _i2c = I2C(0, sda=Pin(_pinmap.LOCAL_I2C_SDA), scl=Pin(_pinmap.LOCAL_I2C_SCL), freq=400_000)
    return _i2c


def _read_eeprom():
    i2c = _local_i2c()
    if i2c is None:
        return None
    try:
        # Address byte 0x00 then read EEPROM_READ_LEN bytes.
        i2c.writeto(_pinmap.CARRIER_EEPROM_ADDR, b"\x00")
        return i2c.readfrom(_pinmap.CARRIER_EEPROM_ADDR, _EEPROM_READ_LEN)
    except OSError:
        return None


def id():
    """Return the carrier identification string from the EEPROM, or ''."""
    raw = _read_eeprom()
    if raw is None:
        return ""
    # Strip 0xFF and NUL padding from the right.
    buf = bytes(raw)
    buf = buf.rstrip(b"\xff").rstrip(b"\x00")
    try:
        return buf.decode("utf-8")
    except UnicodeError:
        # Bytewise hex is the safe fallback for binary carrier IDs.
        return "".join("{:02x}".format(b) for b in buf)


def hw_version():
    """Return the carrier hardware version label.

    On rev1 with no strap pins this is a fixed compile-time string;
    revs that strap a hardware ID extend this to read the strap.
    """
    return "v0.7"
