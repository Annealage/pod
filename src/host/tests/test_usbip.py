"""Tests for pod.usbip - pure parsing/selection (no real usbip or sockets)."""

from pod.usbip import parse_usbip_list, pick_new_tty


USBIP_LIST = """\
Exportable USB devices
======================
 - annealage-pod.local
        1-1: unknown vendor : unknown product (f055:9802)
           : /sys/devices/platform/usbip/1-1
           : (Defined at Interface level) (00/00/00)
"""

USBIP_LIST_TWO = USBIP_LIST + """\
        1-2: Nordic Semiconductor : nRF52 (1915:520f)
           : /sys/devices/platform/usbip/1-2
"""


class TestParseList:
    def test_single_device(self):
        devs = parse_usbip_list(USBIP_LIST)
        assert devs == [{"busid": "1-1", "vid": "f055", "pid": "9802"}]

    def test_two_devices(self):
        devs = parse_usbip_list(USBIP_LIST_TWO)
        assert len(devs) == 2
        assert devs[1] == {"busid": "1-2", "vid": "1915", "pid": "520f"}

    def test_empty(self):
        assert parse_usbip_list("Exportable USB devices\n===\n - host\n") == []


class TestPickNewTty:
    def test_prefers_by_id(self):
        before = {"/dev/ttyACM0"}
        after = before | {"/dev/ttyACM1",
                          "/dev/serial/by-id/usb-MicroPython_board-if00"}
        assert pick_new_tty(before, after) == \
            "/dev/serial/by-id/usb-MicroPython_board-if00"

    def test_falls_back_to_ttyacm(self):
        before = {"/dev/ttyACM0"}
        after = before | {"/dev/ttyACM1"}
        assert pick_new_tty(before, after) == "/dev/ttyACM1"

    def test_none_when_nothing_new(self):
        s = {"/dev/ttyACM0"}
        assert pick_new_tty(s, s) is None
