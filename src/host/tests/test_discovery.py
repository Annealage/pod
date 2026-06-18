"""Tests for pod.discovery - pure parsing, no live network access."""

import pytest
from pod.discovery import parse_avahi_line, PodInfo, _parse_txt_properties


# A captured avahi-browse -rpt line for an annealage-pod service.
# Format: =;iface;proto;name;type;domain;hostname;address;port;"k=v" "k=v" ...
AVAHI_LINE = (
    '=;eth0;IPv4;annealage-pod;_annealage-pod._tcp;local;'
    'annealage-pod.local;192.168.0.121;8266;'
    '"repl-port=8266" "usbip-port=3240" "uart-port=2000" "gdb-port=3335" '
    '"carrier-id=" "mp-version=1.29.0.preview"'
)

# A line with only the minimum fields (no optional ports)
AVAHI_LINE_MINIMAL = (
    '=;wlan0;IPv4;my-pod;_annealage-pod._tcp;local;'
    'my-pod.local;10.0.0.5;8266;'
    '"repl-port=8266"'
)

# A non-resolved line (should be ignored)
AVAHI_LINE_BROWSE = '+;eth0;IPv4;annealage-pod;_annealage-pod._tcp;local'

# A malformed line (too few fields)
AVAHI_LINE_SHORT = '=;eth0;IPv4;name'


class TestParseTxtProperties:
    def test_simple_kv(self):
        props = _parse_txt_properties(['"repl-port=8266"', '"usbip-port=3240"'])
        assert props["repl-port"] == "8266"
        assert props["usbip-port"] == "3240"

    def test_empty_value(self):
        props = _parse_txt_properties(['"carrier-id="'])
        assert props["carrier-id"] == ""

    def test_no_equals(self):
        # token with no '=' is silently ignored
        props = _parse_txt_properties(['"noequals"'])
        assert "noequals" not in props

    def test_unquoted_tokens(self):
        props = _parse_txt_properties(["mp-version=1.29.0.preview"])
        assert props["mp-version"] == "1.29.0.preview"


class TestParseAvahiLine:
    def test_full_line(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod is not None
        assert isinstance(pod, PodInfo)

    def test_address(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod.address == "192.168.0.121"

    def test_port(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod.port == 8266

    def test_repl_port(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod.repl_port == 8266

    def test_usbip_port(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod.usbip_port == 3240

    def test_uart_port(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod.uart_port == 2000

    def test_gdb_port(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod.gdb_port == 3335

    def test_carrier_id_empty(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod.carrier_id == ""

    def test_mp_version(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod.mp_version == "1.29.0.preview"

    def test_name(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod.name == "annealage-pod"

    def test_minimal_line(self):
        pod = parse_avahi_line(AVAHI_LINE_MINIMAL)
        assert pod is not None
        assert pod.address == "10.0.0.5"
        assert pod.repl_port == 8266
        assert pod.usbip_port is None
        assert pod.uart_port is None
        assert pod.gdb_port is None

    def test_browse_line_returns_none(self):
        result = parse_avahi_line(AVAHI_LINE_BROWSE)
        assert result is None

    def test_short_line_returns_none(self):
        result = parse_avahi_line(AVAHI_LINE_SHORT)
        assert result is None

    def test_empty_string_returns_none(self):
        result = parse_avahi_line("")
        assert result is None

    def test_to_dict(self):
        pod = parse_avahi_line(AVAHI_LINE)
        d = pod.to_dict()
        assert d["address"] == "192.168.0.121"
        assert d["repl_port"] == 8266
        assert d["usbip_port"] == 3240
        assert d["uart_port"] == 2000
        assert d["gdb_port"] == 3335
        assert d["mp_version"] == "1.29.0.preview"


# ── v2: hostname + IPv6 handles ────────────────────────────────────────────

from pod.discovery import _sort_addrs, _merge_pods, parse_zeroconf_info

ULA = "fd32:7709:b6ad:0:2ecf:67ff:feb1:8946"
LL = "fe80::2ecf:67ff:feb1:8946"

AVAHI_V6_LINE = (
    '=;wlp2s0;IPv6;annealage-pod;_annealage-pod._tcp;local;'
    'annealage-pod.local;' + ULA + ';8266;'
    '"repl-port=8266" "usbip-port=3240"'
)


class TestSortAddrs:
    def test_ula_before_link_local(self):
        addr4, addr6 = _sort_addrs([LL, ULA, "192.168.0.9"])
        assert addr4 == "192.168.0.9"
        assert addr6 == [ULA, LL]      # ULA/global first, link-local last

    def test_zone_stripped(self):
        _a4, addr6 = _sort_addrs([LL + "%wlan0"])
        assert addr6 == [LL]


class TestAvahiHostname:
    def test_parses_hostname_and_classifies_ipv4(self):
        pod = parse_avahi_line(AVAHI_LINE)
        assert pod.hostname == "annealage-pod.local"
        assert pod.addr4 == "192.168.0.121"
        assert pod.addr6 == []

    def test_parses_ipv6_line(self):
        pod = parse_avahi_line(AVAHI_V6_LINE)
        assert pod.hostname == "annealage-pod.local"
        assert pod.addr6 == [ULA]
        assert pod.addr4 is None


class TestMerge:
    def test_dual_stack_lines_merge(self):
        v4 = parse_avahi_line(AVAHI_LINE)
        v6 = parse_avahi_line(AVAHI_V6_LINE.replace(
            "annealage-pod.local", "annealage-pod.local"))
        # same name+hostname? AVAHI_LINE name is 'annealage-pod', v6 too.
        merged = _merge_pods([v4, v6])
        assert len(merged) == 1
        m = merged[0]
        assert m.addr4 == "192.168.0.121"
        assert ULA in m.addr6


class _FakeInfo:
    """Minimal stand-in for a zeroconf ServiceInfo."""
    def __init__(self, name, server, addrs, props, port=8266):
        self.name = name
        self.server = server
        self._addrs = addrs
        self.properties = props
        self.port = port

    def parsed_addresses(self, *a, **k):
        return self._addrs


class TestZeroconfParse:
    def test_surfaces_hostname_and_addrs(self):
        info = _FakeInfo(
            "annealage-pod._annealage-pod._tcp.local.",
            "annealage-pod.local.",
            ["192.168.0.121", ULA, LL],
            {b"repl-port": b"8266", b"usbip-port": b"3240"},
        )
        pod = parse_zeroconf_info(info)
        assert pod.hostname == "annealage-pod.local"
        assert pod.addr4 == "192.168.0.121"
        assert pod.addr6 == [ULA, LL]
        assert pod.repl_port == 8266
        assert pod.usbip_port == 3240
