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


from pod.usbip import wait_for_new_tty


class TestWaitForNewTty:
    """The by-id-preferring grace logic (the udev-race fix)."""

    def _clock(self):
        t = [0.0]

        def now():
            return t[0]

        def sleep(dt):
            t[0] += dt
        return t, now, sleep

    def test_returns_by_id_immediately(self):
        before = {"/dev/ttyACM0"}
        byid = "/dev/serial/by-id/usb-MicroPython_board-if00"
        _, now, sleep = self._clock()
        got = wait_for_new_tty(before, _sleep=sleep, _list=lambda: before | {byid},
                               _now=now)
        # by-id path does not exist on the test host, so it comes back verbatim
        assert got == byid

    def test_waits_for_by_id_when_raw_appears_first(self):
        """Raw ttyACM shows up first, the by-id link lands within `settle` -> the
        by-id path wins, not the raw node (the permission-denied race fix)."""
        before = {"/dev/ttyACM0"}
        raw = "/dev/ttyACM1"
        byid = "/dev/serial/by-id/usb-MicroPython_board-if00"
        t, now, sleep = self._clock()

        def _list():
            # raw for the first ~0.5s, then udev adds the by-id link
            return before | ({raw} if t[0] < 0.5 else {raw, byid})

        got = wait_for_new_tty(before, settle=2.0, _sleep=sleep, _list=_list, _now=now)
        assert got == byid

    def test_falls_back_to_raw_after_settle(self):
        """A device that never gets a by-id link is accepted after `settle`."""
        before = {"/dev/ttyACM0"}
        raw = "/dev/ttyACM1"
        t, now, sleep = self._clock()
        got = wait_for_new_tty(before, settle=1.0, _sleep=sleep,
                               _list=lambda: before | {raw}, _now=now)
        assert got == raw
        assert t[0] >= 1.0  # it waited the grace period first

    def test_none_on_timeout(self):
        before = {"/dev/ttyACM0"}
        _, now, sleep = self._clock()
        got = wait_for_new_tty(before, timeout=1.0, _sleep=sleep,
                               _list=lambda: before, _now=now)
        assert got is None


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

    def test_matches_across_ula_prefix_change(self, monkeypatch):
        # #3: the pod is stored under an OLD ULA prefix, but the DUT is attached
        # over a NEW prefix (router re-advertised) sharing the pod's EUI-64
        # interface id. The exact-address compare misses it; the interface-id
        # match keeps detach working across the drift.
        from pod.client import Pod
        pod = Pod(addr6=["fdaa:1111:2222:0:2ecf:67ff:feb1:8946"])
        assert self._match(monkeypatch, pod, USBIP_PORT_IPV6) == [0]


class TestIid6:
    def test_extracts_low64(self):
        from pod.client import _iid6
        assert _iid6("fd32:7709:b6ad:0:2ecf:67ff:feb1:8946") == 0x2ecf67fffeb18946

    def test_link_local_and_ula_share_id(self):
        from pod.client import _iid6
        assert _iid6("fe80::2ecf:67ff:feb1:8946") == \
            _iid6("fd32:7709:b6ad:0:2ecf:67ff:feb1:8946")

    def test_strips_zone(self):
        from pod.client import _iid6
        assert _iid6("fe80::2ecf:67ff:feb1:8946%eth0") == 0x2ecf67fffeb18946

    def test_strips_brackets(self):
        from pod.client import _iid6
        assert _iid6("[fd32:7709:b6ad:0:2ecf:67ff:feb1:8946]") == 0x2ecf67fffeb18946

    def test_ipv4_and_hostname_and_none_are_none(self):
        from pod.client import _iid6
        assert _iid6("192.168.0.146") is None
        assert _iid6("annealage-pod.local") is None
        assert _iid6(None) is None
