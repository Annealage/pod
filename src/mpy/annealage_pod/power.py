# Annealage Pod power rail control + INA228 telemetry stubs.
#
# Phase 1: signatures only. Phase 2 wires VTARGET_EN / DUT_USB_VBUS_EN
# GPIOs and the INA228 sampler.


class _Rail:
    def __init__(self, name):
        self._name = name

    def on(self):
        raise NotImplementedError("power.{}.on() not implemented in Phase 1".format(self._name))

    def off(self):
        raise NotImplementedError("power.{}.off() not implemented in Phase 1".format(self._name))

    def current_mA(self):
        raise NotImplementedError("power.{}.current_mA() not implemented in Phase 1".format(self._name))

    def voltage_mV(self):
        raise NotImplementedError("power.{}.voltage_mV() not implemented in Phase 1".format(self._name))


vtarget = _Rail("vtarget")
dut_usb = _Rail("dut_usb")
