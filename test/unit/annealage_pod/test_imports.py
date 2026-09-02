# Smoke test: every submodule must import cleanly without requiring a board
# build (Phase 2 exit criterion), on both sides of the ESP32-S3 quarantine.


def test_top_level_package_imports():
    import annealage_pod

    assert isinstance(annealage_pod.__version__, str)


def test_esp32_subpackage_imports():
    from annealage_pod.esp32 import (  # noqa: F401
        boot, carrier, compat, dut, power, relays, slave, supervisor,
    )


def test_esp32_carrier_modules_are_not_top_level():
    # The ESP32-S3 carrier modules live under annealage_pod.esp32 and must not
    # be reachable at the top level: their pin numbers are the ESP32-S3 carrier
    # map and are wrong (in places dangerous) on the RP2350, so an RP2350 code
    # path must not be able to pick them up by accident.
    import annealage_pod

    for name in ("boot", "carrier", "compat", "dut", "power", "relays",
                 "slave", "supervisor", "_pinmap"):
        assert not hasattr(annealage_pod, name), (
            "%s should be under annealage_pod.esp32, not top level" % name)


def test_ops_subpackage_imports():
    from annealage_pod.ops import log, ota, time, wdt  # noqa: F401


def test_pinmap_constants_present():
    from annealage_pod.esp32 import _pinmap

    assert _pinmap.RELAY_GPIO == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7}
    assert _pinmap.LOCAL_I2C_SDA == 8
    assert _pinmap.LOCAL_I2C_SCL == 9
    assert _pinmap.VTARGET_EN == 40
    assert _pinmap.DUT_USB_VBUS_EN == 41
    assert _pinmap.NRST == 14
