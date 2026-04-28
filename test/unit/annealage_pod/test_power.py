# Unit tests for annealage_pod.power.

from annealage_pod import power


def test_rails_default_off():
    # On Unix port, the soft pin starts at 0.
    power.vtarget.off()
    power.dut_usb.off()
    assert power.vtarget.is_on() is False
    assert power.dut_usb.is_on() is False


def test_on_off_round_trip():
    power.vtarget.off()
    power.vtarget.on()
    assert power.vtarget.is_on() is True
    power.vtarget.off()
    assert power.vtarget.is_on() is False


def test_cycle_increments_counter():
    power.vtarget.off()
    before = power.vtarget.cycle_count()
    power.vtarget.cycle(off_ms=1)
    assert power.vtarget.cycle_count() == before + 1
    assert power.vtarget.is_on() is True


def test_vbus_present_threshold():
    # Without an INA228 the voltage_mV() returns 0.0 so vbus_present is False.
    power.dut_usb.off()
    assert power.dut_usb.vbus_present() is False


def test_voltage_mV_default_zero_without_i2c():
    # No I2C bus on the Unix port; current/voltage default to 0.0.
    assert power.vtarget.current_mA() == 0.0
    assert power.vtarget.voltage_mV() == 0.0
