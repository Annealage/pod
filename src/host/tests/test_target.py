"""Tests for pod.target.TargetResolver - the tiered connect strategy.

The reachability check and the identity probe are injected so the tier logic is
exercised offline, with no real sockets or ampremote.
"""

import pytest

from pod.target import TargetResolver, PodUnreachable


ULA = "fd32:7709:b6ad:0:2ecf:67ff:feb1:8946"
LL = "fe80::2ecf:67ff:feb1:8946"
V4 = "192.168.0.146"
FP = "e6614864abcd1234"


def _resolver(*, reach_ok=(), probe_map=None, **kw):
    """Build a resolver whose reachable()/probe() are driven by test data.

    reach_ok: hosts (substring-exact) that the reachability check accepts.
    probe_map: {host: fingerprint} the identity probe returns (else None).
    """
    calls = {"reachable": [], "probe": []}

    def reachable(host, port, timeout):
        calls["reachable"].append(host)
        return host in reach_ok

    def probe(host, port):
        calls["probe"].append(host)
        return (probe_map or {}).get(host)

    r = TargetResolver(reachable=reachable, probe=probe, **kw)
    r.calls = calls
    return r


class TestTierOrder:
    def test_v6_ula_wins_without_probe(self):
        r = _resolver(addr6=[ULA], addr4=V4, fingerprint=FP, reach_ok=(ULA,))
        assert r.resolve() == ULA
        assert r.tier == "v6"
        assert r.calls["probe"] == []          # v6 is self-authenticating

    def test_v6_unreachable_falls_to_v4_with_matching_fingerprint(self):
        r = _resolver(addr6=[ULA], addr4=V4, fingerprint=FP,
                      reach_ok=(), probe_map={V4: FP})
        assert r.resolve() == V4
        assert r.tier == "v4"
        assert r.calls["probe"] == [V4]

    def test_v4_fingerprint_mismatch_is_discarded(self):
        r = _resolver(addr4=V4, fingerprint=FP, probe_map={V4: "deadbeef"})
        with pytest.raises(PodUnreachable):
            r.resolve()
        assert any("DHCP reassigned" in w for w in r.warnings)

    def test_v4_trust_on_first_use_learns_fingerprint(self):
        # No stored fingerprint, but a hostname exists so it is not the lone-addr
        # short-circuit: the probe runs and the observed id is learned.
        r = _resolver(hostname="pod.local", addr4=V4, probe_map={V4: FP},
                      reach_ok=())
        assert r.resolve() == V4
        assert r.learned_fingerprint == FP

    def test_lone_v4_short_circuits_without_network(self):
        # addr4 only, no fingerprint, no hostname: trust it, touch nothing.
        r = _resolver(addr4=V4)
        assert r.resolve() == V4
        assert r.calls["reachable"] == []
        assert r.calls["probe"] == []


class TestZone:
    def test_link_local_gets_a_host_zone(self, monkeypatch):
        monkeypatch.setattr("pod.target._host_zones", lambda: ["eth0"])
        r = _resolver(addr6=[LL], reach_ok=(LL + "%eth0",))
        assert r.resolve() == LL + "%eth0"

    def test_ula_preferred_over_link_local(self, monkeypatch):
        monkeypatch.setattr("pod.target._host_zones", lambda: ["eth0"])
        # both reachable; ULA appears first in addr6 so it must win
        r = _resolver(addr6=[ULA, LL], reach_ok=(ULA, LL + "%eth0"))
        assert r.resolve() == ULA


class TestRendering:
    def test_ampremote_target_brackets_ipv6(self):
        r = _resolver(addr6=[ULA], reach_ok=(ULA,))
        assert r.ampremote_target(8266) == f"socket://[{ULA}]:8266"

    def test_ampremote_target_plain_ipv4(self):
        r = _resolver(addr4=V4)
        assert r.ampremote_target(8266) == f"socket://{V4}:8266"

    def test_endpoint_tuple(self):
        r = _resolver(addr4=V4)
        assert r.endpoint(3333) == (V4, 3333)

    def test_cache_memoized(self):
        r = _resolver(addr6=[ULA], reach_ok=(ULA,))
        r.resolve()
        n = len(r.calls["reachable"])
        r.resolve()                            # second call must not re-walk
        assert len(r.calls["reachable"]) == n
        assert r.cached == ULA
