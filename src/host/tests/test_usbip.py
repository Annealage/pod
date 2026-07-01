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


from pod.usbip import parse_usbip_port

USBIP_PORT = """\
Imported USB devices
====================
Port 00: <Port in Use> at Full Speed(12Mbps)
       unknown vendor : unknown product (f055:9802)
       3-1 -> usbip://192.168.0.146:3240/1-1
           -> remote bus/dev 001/002
"""

# The real observed IPv6-literal attach: `usbip port` prints the ULA remote
# unbracketed with its own inner colons, then the TCP :3240 and /busid.
USBIP_PORT_IPV6 = """\
Imported USB devices
====================
Port 00: <Port in Use> at Full Speed(12Mbps)
       unknown vendor : unknown product (f055:9802)
        5-1 -> usbip://fd32:7709:b6ad:0:2ecf:67ff:feb1:8946:3240/1-1
           -> remote bus/dev 001/002
"""


class TestParsePort:
    def test_parses_port_remote_busid(self):
        assert parse_usbip_port(USBIP_PORT) == [
            {"port": 0, "remote": "192.168.0.146", "busid": "1-1"}]

    def test_parses_ipv6_literal_remote(self):
        # The IPv6 remote must be captured whole (not clipped at its first
        # colon) and returned unbracketed, else attached_ports() never matches
        # it and detach leaves the DUT attached.
        assert parse_usbip_port(USBIP_PORT_IPV6) == [
            {"port": 0,
             "remote": "fd32:7709:b6ad:0:2ecf:67ff:feb1:8946",
             "busid": "1-1"}]

    def test_parses_ipv6_bracketed_remote(self):
        text = (
            "Port 02: <Port in Use>\n"
            "        3-1 -> usbip://[fd32:7709:b6ad:0:2ecf:67ff:feb1:8946]"
            ":3240/1-1\n")
        assert parse_usbip_port(text) == [
            {"port": 2,
             "remote": "fd32:7709:b6ad:0:2ecf:67ff:feb1:8946",
             "busid": "1-1"}]

    def test_empty(self):
        assert parse_usbip_port("Imported USB devices\n=====\n") == []


class TestAttachedPortsIPv6:
    """Pod.attached_ports() must match an IPv6-literal vhci attach to the pod."""

    def _match(self, monkeypatch, pod, port_text):
        from pod import usbip as _u
        from pod.client import Pod
        monkeypatch.setattr(
            _u, "ports", lambda runner=None: _u.parse_usbip_port(port_text))
        return Pod.attached_ports(pod)

    def test_matches_ipv6_attach_for_detach(self, monkeypatch):
        # Pod's stored ULA is spelled with an expanded zero group; the vhci
        # remote uses the compressed form. Both normalise to the same address,
        # so the port is matched and detach_dut / `pod detach` can act on it.
        from pod.client import Pod
        pod = Pod(addr6=["fd32:7709:b6ad:0000:2ecf:67ff:feb1:8946"])
        assert self._match(monkeypatch, pod, USBIP_PORT_IPV6) == [0]

    def test_no_match_for_other_pod(self, monkeypatch):
        from pod.client import Pod
        pod = Pod(addr6=["fd32:7709:b6ad:0:2ecf:67ff:feb1:0001"])
        assert self._match(monkeypatch, pod, USBIP_PORT_IPV6) == []
