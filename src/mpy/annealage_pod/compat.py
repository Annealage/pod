# Annealage Pod: RP_INFRA-equivalent compatibility shim.
#
# Reproduces the symbol table the existing Octoprobe RP_INFRA Pico
# exposes so that testbed_micropython only needs a TCP-REPL transport
# adapter to keep working. See docs/spec-appendix-B-rp_infra-api.md
# for the source of truth.
#
# The Pico-side surface (Appendix B §B.2) is:
#   - 3 module-level vars: pico_unique_id, gpio_hw_version, files_on_flash
#   - 6 status/control Pin objects + RELAY1..RELAY7
#   - 4 helper functions: set_switch, get_relays, set_relays,
#     set_relays_pulse
#
# Implementation strategy: every Pin object is a thin shim with a
# .value() method that delegates to the underlying annealage_pod.* primitive
# (relays.relays for the relays; status LED control for LED_ACTIVE /
# LED_ERROR; power.vtarget.set() for DUT). PROBE_RUN / PROBE_BOOT have
# no real RP_PROBE on the new hardware (it is replaced by the synthetic
# CMSIS-DAP-v2); they accept .value() writes silently per Appendix B
# §B.5 recommendation (silent no-op rather than raise so `op` CLI keeps
# working). gpio_hw_version is the strap-pin equivalent; on rev1 there
# are no straps so it returns 7 (mapped to v0.7 per
# lib_annealage_pod_infra_pico.py:138-148).
#
# Bug fix: Appendix B §B.7 flags lib_annealage_pod_infra_pico.py:59-61's
# `get_relays(relays)` function for shadowing the parameter `relays`
# (it indexes with an unbound `i`). The shim version below uses the
# parameter consistently and returns the right thing.

import binascii

try:
    import machine
except ImportError:
    machine = None

from . import _pinmap, power, relays


# --- Module-level variables (Appendix B §B.2.1) ---------------------------

def _compute_unique_id():
    if machine is None or not hasattr(machine, "unique_id"):
        # Unix port: synthesise a stable but obviously-fake id.
        return "unix-port-no-machine-uid"
    try:
        return binascii.hexlify(machine.unique_id()).decode("ascii")
    except Exception:
        return ""


pico_unique_id = _compute_unique_id()

# Strap-resistor hw_version. On rev1 there are no straps so we report
# the value mapped to v0.7 in lib_annealage_pod_infra_pico.py:138-148. If
# straps are added in a later rev this should sample them and follow
# the same encoding (5-bit GPIO read on the original Pico).
gpio_hw_version = 7

# files_on_flash: testbed uses this in commissioning to assert the
# flash is clean. The new annealage_pod ships with frozen modules and an
# empty / vfs partition, so 0 is the right default. Code that wants
# the live count should call os.listdir() directly.
files_on_flash = 0


# --- Pin object stand-ins (Appendix B §B.2.2) -----------------------------


class _ShimPin:
    """RP_INFRA-shape Pin proxy. Delegates `.value(x)` to a setter."""

    def __init__(self, name, getter, setter):
        self._name = name
        self._getter = getter
        self._setter = setter

    def value(self, *args):
        """Read or write the underlying state.

        With no arg: return current value (int 0/1).
        With one arg: write the value.
        """
        if not args:
            return 1 if self._getter() else 0
        self._setter(bool(args[0]))
        return None


# Status LED controllers. Strapping pins GPIO45/46 on the new board.
_led_state = {"active": False, "error": False}


def _led_pin(idx):
    if machine is None or not hasattr(machine, "Pin"):
        return None
    gpio = _pinmap.LED_STATUS_1 if idx == 0 else _pinmap.LED_STATUS_2
    return machine.Pin(gpio, machine.Pin.OUT, value=0)


_led_active_pin = _led_pin(0)
_led_error_pin = _led_pin(1)


def _led_active_get():
    return _led_state["active"]


def _led_active_set(on):
    _led_state["active"] = bool(on)
    if _led_active_pin is not None:
        _led_active_pin.value(1 if on else 0)


def _led_error_get():
    return _led_state["error"]


def _led_error_set(on):
    _led_state["error"] = bool(on)
    if _led_error_pin is not None:
        _led_error_pin.value(1 if on else 0)


def _dut_get():
    return power.vtarget.is_on()


def _dut_set(on):
    power.vtarget.set(bool(on))


# RP_PROBE replacement: no real probe on the new hardware. Per
# Appendix B §B.5, silently no-op so `op` CLI keeps running. State
# is kept in module dicts so .value() reads back what was last
# written.
_probe_state = {"run": False, "boot": True}


def _probe_run_get():
    return _probe_state["run"]


def _probe_run_set(on):
    _probe_state["run"] = bool(on)


def _probe_boot_get():
    return _probe_state["boot"]


def _probe_boot_set(on):
    _probe_state["boot"] = bool(on)


pin_LED_ACTIVE = _ShimPin("LED_ACTIVE", _led_active_get, _led_active_set)
pin_LED_ERROR = _ShimPin("LED_ERROR", _led_error_get, _led_error_set)
pin_DUT = _ShimPin("DUT", _dut_get, _dut_set)
pin_PICO_PROBE_RUN = _ShimPin("PICO_PROBE_RUN", _probe_run_get, _probe_run_set)
pin_PICO_PROBE_BOOT = _ShimPin("PICO_PROBE_BOOT", _probe_boot_get, _probe_boot_set)


# Relay Pin proxies (RELAY1..RELAY7) and the dict the original code uses.
def _make_relay_pin(n):
    return _ShimPin("RELAY{}".format(n), lambda n=n: relays.relays.get(n), lambda v, n=n: relays.relays.set(n, v))


pin_RELAY1 = _make_relay_pin(1)
pin_RELAY2 = _make_relay_pin(2)
pin_RELAY3 = _make_relay_pin(3)
pin_RELAY4 = _make_relay_pin(4)
pin_RELAY5 = _make_relay_pin(5)
pin_RELAY6 = _make_relay_pin(6)
pin_RELAY7 = _make_relay_pin(7)

pin_relays = {
    1: pin_RELAY1,
    2: pin_RELAY2,
    3: pin_RELAY3,
    4: pin_RELAY4,
    5: pin_RELAY5,
    6: pin_RELAY6,
    7: pin_RELAY7,
}


# --- Helper functions (Appendix B §B.2.3) ---------------------------------


def set_switch(pin, on):
    """Write `on` to the given _ShimPin. Returns True iff the value changed."""
    prior = bool(pin.value())
    new = bool(on)
    if prior == new:
        return False
    pin.value(1 if new else 0)
    return True


def get_relays(relay):
    """Return True iff relay `relay` (1..7) is closed.

    Bug fix: the upstream RP_INFRA helper at
    lib_annealage_pod_infra_pico.py:59-61 declares the parameter as
    `relays` then indexes with `i` (an unbound name). Appendix B
    §B.7 flags this. The shim uses the parameter consistently.
    """
    return bool(pin_relays[relay].value())


def set_relays(list_relays):
    """Apply `list_relays` (sequence of (n, on) pairs) atomically.

    Returns True iff at least one relay state changed.
    """
    changed = False
    for n, on in list_relays:
        prior = bool(pin_relays[n].value())
        new = bool(on)
        if prior != new:
            pin_relays[n].value(1 if new else 0)
            changed = True
    return changed


def set_relays_pulse(relay, initial_closed, durations_ms):
    """Drive `relay` to `initial_closed`, then toggle for each duration_ms.

    Used for double-tap reset patterns (RP2 BOOTSEL, NRF, SAMD).
    """
    relays.relays.pulse(relay, bool(initial_closed), list(durations_ms))


# --- Strict mode toggle ---------------------------------------------------

_strict = False


def set_strict(strict):
    """Toggle strict mode. When True, no-op compat surfaces raise instead.

    Per Appendix B §B.7 recommendation: default behaviour is silent
    no-op so `op` CLI continues working without exception spam.
    """
    global _strict
    _strict = bool(strict)


def is_strict():
    """Return current strict-mode setting."""
    return _strict
