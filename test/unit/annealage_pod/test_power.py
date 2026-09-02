# Unit tests for annealage_pod.power.

from annealage_pod.esp32 import power


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


def test_is_measurable_without_i2c():
    # Unix port has no I2C bus; both rails report unmeasurable up front
    # rather than silently returning a zero sentinel from voltage_mV().
    assert power.vtarget.is_measurable() is False
    assert power.dut_usb.is_measurable() is False


def test_voltage_mV_raises_when_unavailable():
    import errno
    import pytest
    with pytest.raises(OSError) as exc:
        power.vtarget.voltage_mV()
    assert exc.value.errno == errno.ENODEV
    with pytest.raises(OSError) as exc:
        power.vtarget.current_mA()
    assert exc.value.errno == errno.ENODEV


def test_vbus_present_raises_when_unavailable():
    import errno
    import pytest
    power.dut_usb.off()
    with pytest.raises(OSError) as exc:
        power.dut_usb.vbus_present()
    assert exc.value.errno == errno.ENODEV
