"""Pod connect-target resolution.

A pod is identified by stable handles - its mDNS hostname and IPv6 addresses -
plus a DHCP IPv4 that can drift. TargetResolver picks a live connect target,
preferring the handles that need no name resolution and that authenticate the
device by construction, and verifying identity before trusting any address that
does not:

  1. IPv6 literal (ULA first, then link-local with a host interface zone). No
     resolution latency, and self-authenticating: the pod's interface id is its
     MAC via EUI-64, so reaching the address proves it is the right board.
  2. IPv4 literal. Fast, but a DHCP lease can move to a different host, so the
     pod's machine.unique_id() fingerprint is read and checked before the
     address is trusted. A mismatch is discarded, never operated on - a
     wrong-target flash/reset is the failure this whole strategy prevents.
  3. mDNS hostname re-resolution. The slow last resort; the freshly learned
     addresses are exposed via `reconciled` for the caller to write back so the
     next session starts at tier 1 again.

The chosen target is memoized: every transport (the ampremote socket REPL and
the raw flash/read/LA/gdb data sockets) shares one resolution, so the per-op
poll-connect retries never re-run the tier walk. resolve() is thread-safe; a
hard failure mid-session is cleared with invalidate() to force a fresh walk.
"""

import ipaddress
import re
import socket
import subprocess
import sys
import threading


class PodUnreachable(Exception):
    """No tier yielded a usable, identity-confirmed target for the pod."""


def _is_link_local6(addr):
    try:
        return ipaddress.ip_address(addr).is_link_local
    except ValueError:
        return False


def _extract_fingerprint(text):
    """Pull the machine.unique_id().hex() value out of REPL stdout."""
    if not text:
        return None
    for line in reversed(text.strip().splitlines()):
        m = re.search(r"[0-9a-fA-F]{8,}", line.strip())
        if m:
            return m.group(0).lower()
    return None


def _ampremote_target(host, port):
    """Render an ampremote socket:// target, bracketing IPv6 literals."""
    if ":" in host:          # IPv6 literal, possibly with a %zone suffix
        return "socket://[%s]:%d" % (host, port)
    return "socket://%s:%d" % (host, port)


def _probe_unique_id(host, port, timeout=10.0):
    """Read machine.unique_id().hex() from the pod at host:port, or None.

    Returns None (rather than raising) on any failure - unreachable, timeout, a
    busy REPL - so the resolver can treat 'could not confirm' distinctly from a
    confirmed mismatch.
    """
    target = _ampremote_target(host, port)
    try:
        out = subprocess.run(
            ["ampremote", "connect", target, "exec",
             "print(__import__('machine').unique_id().hex())"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return _extract_fingerprint(out.stdout)


def read_fingerprint(host, port=8266, timeout=10.0):
    """Read a pod's machine.unique_id() fingerprint at host:port, or None."""
    return _probe_unique_id(host, port, timeout)


def _tcp_reachable(host, port, timeout):
    """True if a TCP connection to host:port can be opened (and closed)."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    s.close()
    return True


class TargetResolver:
    """Resolve a pod's stable identity to a live connect target (see module doc)."""

    def __init__(self, *, hostname=None, addr6=None, addr4=None,
                 fingerprint=None, repl_port=8266, connect_timeout=2.0,
                 probe=_probe_unique_id, reachable=_tcp_reachable):
        self.hostname = hostname or None
        self.addr6 = list(addr6 or [])
        self.addr4 = addr4 or None
        self.fingerprint = (fingerprint or "").lower() or None
        self.repl_port = repl_port
        self._connect_timeout = connect_timeout
        self._probe = probe
        self._reachable = reachable

        self._lock = threading.Lock()
        self._resolved = None       # cached connect host string
        self.tier = None            # 'v6' | 'v4' | 'mdns'
        self.learned_fingerprint = None   # set on trust-on-first-use
        self.reconciled = None      # {'addr6': [...], 'addr4': str} learned via mDNS
        self.warnings = []

    # ── target rendering (consumers call these) ───────────────────────────

    def endpoint(self, port):
        """A (host, port) tuple for socket.create_connection."""
        return (self.resolve(), port)

    def ampremote_target(self, port):
        """An ampremote 'socket://...' target string (brackets IPv6 literals)."""
        return _ampremote_target(self.resolve(), port)

    @property
    def cached(self):
        """The memoized target if resolve() has run, else None (no walk)."""
        return self._resolved

    def invalidate(self):
        """Drop the memoized target so the next resolve() re-walks the tiers."""
        with self._lock:
            self._resolved = None
            self.tier = None

    # ── resolution ────────────────────────────────────────────────────────

    def resolve(self, force=False):
        with self._lock:
            if self._resolved is not None and not force:
                return self._resolved
            host = self._walk()
            self._resolved = host
            return host

    def _warn(self, msg):
        self.warnings.append(msg)
        print("pod: " + msg, file=sys.stderr)

    def _walk(self):
        # Tier 1: IPv6 literals (self-authenticating via EUI-64; no probe).
        for host in self._v6_candidates():
            if self._reachable(host, self.repl_port, self._connect_timeout):
                self.tier = "v6"
                return host

        # Tier 2: IPv4 literal (must pass the identity probe before trust).
        if self.addr4:
            if self.fingerprint is None and not self.hostname:
                # A lone address with no fingerprint to check and no mDNS
                # fallback: trust it. The caller handed us an explicit address
                # and there is nothing to verify it against - this is also the
                # bare Pod(address=...) path, which must stay network-free.
                self.tier = "v4"
                return self.addr4
            host = self._try_v4(self.addr4)
            if host is not None:
                self.tier = "v4"
                return host

        # Tier 3: mDNS hostname re-resolution (slow last resort).
        host = self._try_mdns()
        if host is not None:
            self.tier = "mdns"
            return host

        detail = ("; ".join(self.warnings)) if self.warnings else "no candidates"
        raise PodUnreachable(
            "could not resolve pod (hostname=%r addr6=%r addr4=%r): %s"
            % (self.hostname, self.addr6, self.addr4, detail))

    def _v6_candidates(self):
        """Yield IPv6 host strings to try: ULA/global as-is, link-local %zone."""
        for a in self.addr6:
            if _is_link_local6(a):
                for zone in _host_zones():
                    yield "%s%%%s" % (a, zone)
            else:
                yield a

    def _try_v4(self, addr4):
        """Confirm a v4 literal's identity before returning it; else None."""
        fp = self._probe(addr4, self.repl_port)
        if fp is None:
            # Reachable-but-unconfirmed (busy/timeout) or unreachable. Do not
            # trust a v4 we could not authenticate; fall through to mDNS.
            self._warn("IPv4 %s did not confirm identity; trying mDNS" % addr4)
            return None
        if self.fingerprint is None:
            # Trust-on-first-use: no stored fingerprint yet. Learn it, but flag
            # that it was observed, not verified.
            self.learned_fingerprint = fp
            self._warn("learned fingerprint %s from %s (trust-on-first-use)"
                       % (fp, addr4))
            return addr4
        if fp == self.fingerprint:
            return addr4
        self._warn(
            "IPv4 %s now answers as %s, expected %s - DHCP reassigned; "
            "falling back to mDNS" % (addr4, fp, self.fingerprint))
        return None

    def _try_mdns(self):
        if not self.hostname:
            return None
        # First, the OS resolver (fast where nss-mdns/Avahi is present).
        fams = _resolve_families(self.hostname, self.repl_port)
        if fams.get(socket.AF_INET6):
            v6 = fams[socket.AF_INET6][0]
            if self._reachable(self.hostname, self.repl_port,
                               self._connect_timeout):
                self.reconciled = {"addr6": fams[socket.AF_INET6], "addr4":
                                   (fams.get(socket.AF_INET) or [None])[0]}
                return self.hostname            # v6 path is self-authenticating
        if fams.get(socket.AF_INET):
            host = self._try_v4(self.hostname)  # name -> v4: probe identity
            if host is not None:
                self.reconciled = {"addr4": fams[socket.AF_INET][0],
                                   "addr6": fams.get(socket.AF_INET6, [])}
                return self.hostname

        # OS resolver gave nothing usable; fall back to an in-process mDNS browse
        # (the only path guaranteed available on hosts without nss-mdns).
        return self._try_mdns_browse()

    def _try_mdns_browse(self):
        try:
            from pod.discovery import discover_pods
        except ImportError:
            return None
        try:
            pods = discover_pods(timeout=5.0)
        except Exception:  # noqa: BLE001 - browse failure is just an unreachable tier
            return None
        want = (self.hostname or "").rstrip(".").lower()
        for p in pods:
            phost = (getattr(p, "hostname", "") or "").rstrip(".").lower()
            if want and phost != want:
                continue
            addr6 = list(getattr(p, "addr6", []) or [])
            addr4 = getattr(p, "addr4", None)
            # Re-walk the fresh addresses through the same tier policy.
            sub = TargetResolver(
                addr6=addr6, addr4=addr4, fingerprint=self.fingerprint,
                repl_port=self.repl_port, connect_timeout=self._connect_timeout,
                probe=self._probe, reachable=self._reachable)
            try:
                host = sub.resolve()
            except PodUnreachable:
                continue
            self.learned_fingerprint = (
                self.learned_fingerprint or sub.learned_fingerprint)
            self.reconciled = {"addr6": addr6, "addr4": addr4}
            return host
        return None


def _host_zones():
    """Non-loopback interface names usable as IPv6 link-local %zone scopes."""
    try:
        idx = socket.if_nameindex()
    except (OSError, AttributeError):
        return []
    return [name for _i, name in idx if name != "lo"]


def _resolve_families(host, port):
    """getaddrinfo(host) grouped by address family -> {family: [addr, ...]}."""
    out = {}
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return out
    for family, _t, _p, _c, sockaddr in infos:
        out.setdefault(family, [])
        addr = sockaddr[0]
        if addr not in out[family]:
            out[family].append(addr)
    return out
