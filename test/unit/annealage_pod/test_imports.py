# Smoke test: every WS-E submodule must import cleanly without
# requiring a board build (Phase 2 exit criterion).


def test_top_level_package_imports():
    import annealage_pod

    assert isinstance(annealage_pod.__version__, str)


def test_subpackages_import():
    from annealage_pod import boot, carrier, compat, dut, ops, power, relays, slave, supervisor  # noqa: F401


def test_ops_subpackage_imports():
    from annealage_pod.ops import log, ota, time, wdt  # noqa: F401


def test_pinmap_constants_present():
    from annealage_pod import _pinmap

    assert _pinmap.RELAY_GPIO == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7}
    assert _pinmap.LOCAL_I2C_SDA == 8
    assert _pinmap.LOCAL_I2C_SCL == 9
    assert _pinmap.VTARGET_EN == 40
    assert _pinmap.DUT_USB_VBUS_EN == 41
    assert _pinmap.NRST == 14
