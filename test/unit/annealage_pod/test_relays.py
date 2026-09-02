# Unit tests for annealage_pod.relays.

from annealage_pod.esp32 import relays


def test_numbers():
    assert relays.relays.numbers() == (1, 2, 3, 4, 5, 6, 7)


def test_get_set_returns_changed_flag():
    bank = relays.relays
    bank.batch(open=bank.numbers())
    assert bank.set(1, True) is True  # changed off->on
    assert bank.get(1) is True
    assert bank.set(1, True) is False  # no change
    assert bank.set(1, False) is True
    assert bank.get(1) is False


def test_batch_atomic():
    bank = relays.relays
    bank.batch(open=bank.numbers())
    changed = bank.batch(close=[1, 3, 5], open=[2, 4, 6])
    assert changed is True
    assert bank.get(1) is True
    assert bank.get(3) is True
    assert bank.get(5) is True
    assert bank.get(2) is False
    assert bank.get(4) is False
    assert bank.get(6) is False
    # Idempotent: applying the same batch returns False.
    assert bank.batch(close=[1, 3, 5], open=[2, 4, 6]) is False


def test_pulse_terminal_state_two_durations():
    bank = relays.relays
    bank.batch(open=bank.numbers())
    bank.pulse(7, True, [1, 1])  # initial=True, toggle, toggle -> True
    assert bank.get(7) is True


def test_pulse_terminal_state_three_durations():
    bank = relays.relays
    bank.batch(open=bank.numbers())
    bank.pulse(7, False, [1, 1, 1])  # initial=False, then 3 toggles -> True
    assert bank.get(7) is True


def test_all_off():
    bank = relays.relays
    bank.batch(close=bank.numbers())
    assert bank.all_off() is True
    for n in bank.numbers():
        assert bank.get(n) is False
    # Second call is a no-op.
    assert bank.all_off() is False
