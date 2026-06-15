# Unit tests for annealage_pod.compat (RP_INFRA shim).

from annealage_pod import compat, relays


def test_module_level_vars_present():
    assert isinstance(compat.pico_unique_id, str)
    assert isinstance(compat.gpio_hw_version, int)
    assert compat.gpio_hw_version == 7
    assert compat.files_on_flash == 0


def test_pin_objects_exist():
    # The 6 documented status / probe pins plus 7 relay pins.
    for name in (
        "pin_LED_ACTIVE",
        "pin_LED_ERROR",
        "pin_DUT",
        "pin_PICO_PROBE_RUN",
        "pin_PICO_PROBE_BOOT",
    ):
        assert hasattr(compat, name)
    for n in range(1, 8):
        assert hasattr(compat, "pin_RELAY{}".format(n))
    # pin_relays dict mirrors RP_INFRA.
    assert set(compat.pin_relays.keys()) == set(range(1, 8))


def test_set_switch_returns_change_flag():
    pin = compat.pin_LED_ACTIVE
    pin.value(0)
    assert compat.set_switch(pin, True) is True
    assert pin.value() == 1
    assert compat.set_switch(pin, True) is False  # unchanged


def test_get_relays_uses_parameter_name_correctly():
    """Bug fix: upstream RP_INFRA shadowed the `relays` parameter.

    See docs/esp32-s3/spec-appendix-B-rp_infra-api.md §B.7. The shim must
    actually return state for the requested relay number rather
    than crash on an unbound `i`.
    """
    relays.relays.batch(open=relays.relays.numbers())
    assert compat.get_relays(1) is False
    relays.relays.set(1, True)
    assert compat.get_relays(1) is True
    assert compat.get_relays(2) is False


def test_set_relays_batch_returns_change_flag():
    relays.relays.batch(open=relays.relays.numbers())
    changed = compat.set_relays([(1, True), (2, True), (3, False)])
    assert changed is True
    assert compat.get_relays(1) is True
    assert compat.get_relays(2) is True
    assert compat.get_relays(3) is False
    # Idempotent application returns False.
    again = compat.set_relays([(1, True), (2, True), (3, False)])
    assert again is False


def test_set_relays_pulse_terminal_state():
    relays.relays.batch(open=relays.relays.numbers())
    compat.set_relays_pulse(4, True, [1, 1])  # 2 toggles -> True
    assert compat.get_relays(4) is True


def test_probe_pins_silent_no_op():
    """RP_PROBE_RUN/BOOT have no real probe; spec § B.5 says no-op."""
    pin = compat.pin_PICO_PROBE_RUN
    pin.value(1)
    assert pin.value() == 1
    pin.value(0)
    assert pin.value() == 0


def test_strict_mode_toggle_default_off():
    assert compat.is_strict() is False
    compat.set_strict(True)
    assert compat.is_strict() is True
    compat.set_strict(False)
    assert compat.is_strict() is False
