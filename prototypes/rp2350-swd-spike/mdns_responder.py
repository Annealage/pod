# Pure-Python mDNS service advertiser for the Annealage Pod RP2350.
#
# Phase F1.3: advertise a browsable mDNS service so host tooling discovers the
# pod by service type (_annealage-pod._tcp) rather than a hardcoded IP.
#
# --- Why pure-Python, and why announcement-only ---
#
# This firmware (rp2 port, CYW43) compiles in lwIP's mDNS responder:
#   ports/rp2/main.c              -> mdns_resp_init()
#   extmod/cyw43_config_common.h  -> mdns_resp_add_netif(netif, hostname) on
#                                    every TCP/IP-up callback.
# So lwIP already answers A-record queries for "<hostname>.local"
# (avahi-resolve -n annealage-pod.local already returns the pod IP). But the
# rp2 port exposes NO Python binding to mdns_resp_add_service(), so the
# built-in responder cannot advertise a *service* (PTR/SRV/TXT). There is no
# `mdns`/`umdns` module and nothing service-related in `network`.
#
# lwIP's responder also owns UDP port 5353 (its own raw-API PCB) from boot.
# MicroPython's socket layer is a separate PCB; binding *any* address on port
# 5353 raises EADDRINUSE even with SO_REUSEADDR (confirmed across clean soft
# resets), and SO_REUSEPORT is not built. So a Python responder CANNOT:
#   * receive multicast queries on 5353 (can't answer the PTR (QM)? query that
#     `avahi-browse` sends), nor
#   * acquire UDP source port 5353 for its own transmissions.
#
# What IS available: sending UDP to 224.0.0.251:5353 from an *ephemeral* source
# port works. So this module operates in unsolicited-announcement mode
# (RFC 6762 section 8.3): it periodically multicasts a full response record set
# (PTR + SRV + TXT + A). tcpdump confirms these packets are well-formed and on
# the wire.
#
# *** KNOWN LIMITATION (validated 2026-05-31) ***
# avahi does NOT enter these announcements into its service-browse cache, so
# `avahi-browse -rt _annealage-pod._tcp` shows nothing. Root cause: RFC 6762
# section 6 requires mDNS responses to be sent FROM source UDP port 5353, and
# avahi drops responses with any other source port. Because lwIP holds 5353,
# this Python responder is forced onto an ephemeral source port and its
# responses are silently discarded by conformant resolvers.
#
# A genuinely browsable service therefore requires lwIP's own responder via
# mdns_resp_add_service(), which has NO Python binding in this firmware (not in
# `mdns`/`umdns` -- absent -- nor in the `lwip`/`network` modules). Exposing it
# is a firmware change (out of scope for this spike). The hostname A-record IS
# already served by lwIP today: `avahi-resolve -n annealage-pod.local` returns
# the pod IP without any Python involvement.
#
# This module is kept because: (a) its DNS wire-format construction is correct
# and reusable, (b) resolvers that accept gratuitous responses (not avahi) will
# still learn the service, and (c) it documents the exact constraint for the
# follow-up firmware-binding work.
#
# --- Threading ---
#
# rp2 allows exactly one extra _thread (core1). The dupterm socket REPL serve
# loop (netboot.py / netrepl.py) already holds it. So this module does NOT
# spawn a second core1 thread when integrated. Two usage modes:
#   * tick():  call periodically from an existing loop (cooperative). This is
#              the mode intended for folding into the netboot serve loop via a
#              select() that also wakes on a timeout.
#   * run():   blocking loop, for standalone testing only (occupies the caller,
#              e.g. the foreground REPL or the single permitted core1 thread).

import socket
import struct
import time

MCAST_ADDR = "224.0.0.251"
MCAST_PORT = 5353

# DNS record types / classes
_TYPE_A = 1
_TYPE_PTR = 12
_TYPE_TXT = 16
_TYPE_SRV = 33
_CLASS_IN = 1
_CACHE_FLUSH = 0x8000  # top bit of the rrclass in mDNS responses

# Default record TTLs (seconds). 120s is the conventional mDNS value.
_TTL = 120


def _enc_name(name):
    # Encode a dotted DNS name (e.g. "annealage-pod.local") as length-prefixed
    # labels terminated by a zero byte. No compression (keeps it simple; the
    # records are small).
    out = bytearray()
    for label in name.split("."):
        b = label.encode()
        out.append(len(b))
        out.extend(b)
    out.append(0)
    return bytes(out)


def _enc_txt(items):
    # items: list of "key=value" strings. Each becomes a length-prefixed
    # character-string inside one TXT rdata blob.
    out = bytearray()
    for it in items:
        b = it.encode()
        out.append(len(b))
        out.extend(b)
    if not out:
        out.append(0)  # empty TXT must contain a single zero-length string
    return bytes(out)


def _ip_to_bytes(ip):
    return bytes(int(x) for x in ip.split("."))


class MDNSResponder:
    def __init__(self, ip, hostname="annealage-pod", service="_annealage-pod._tcp",
                 instance=None, port=8266, txt=None, interval=5):
        self.ip = ip
        self.hostname = hostname
        self.host_fqdn = hostname + ".local"
        self.service = service + ".local"           # _annealage-pod._tcp.local
        if instance is None:
            instance = hostname
        self.instance_fqdn = instance + "." + self.service  # name._service.local
        self.port = port
        self.txt = txt if txt is not None else ["repl-port=%d" % port]
        self.interval = interval
        self._sock = None
        self._dest = None
        self._next = 0
        self._pkt = self._build_announcement()

    # --- wire format ---------------------------------------------------

    def _build_announcement(self):
        # One DNS message carrying PTR, SRV, TXT, A as answer records.
        # Header: id=0, flags=0x8400 (response + authoritative), qd=0,
        # an=4, ns=0, ar=0.
        ptr = self._rr(self.service, _TYPE_PTR, _CLASS_IN, _TTL,
                       _enc_name(self.instance_fqdn))
        # SRV rdata: priority(2) weight(2) port(2) target-name
        srv_rdata = struct.pack(">HHH", 0, 0, self.port) + _enc_name(self.host_fqdn)
        srv = self._rr(self.instance_fqdn, _TYPE_SRV,
                       _CLASS_IN | _CACHE_FLUSH, _TTL, srv_rdata)
        txt = self._rr(self.instance_fqdn, _TYPE_TXT,
                       _CLASS_IN | _CACHE_FLUSH, _TTL, _enc_txt(self.txt))
        a = self._rr(self.host_fqdn, _TYPE_A,
                     _CLASS_IN | _CACHE_FLUSH, _TTL, _ip_to_bytes(self.ip))
        header = struct.pack(">HHHHHH", 0, 0x8400, 0, 4, 0, 0)
        return header + ptr + srv + txt + a

    @staticmethod
    def _rr(name, rtype, rclass, ttl, rdata):
        return (_enc_name(name)
                + struct.pack(">HHIH", rtype, rclass, ttl, len(rdata))
                + rdata)

    # --- socket --------------------------------------------------------

    def open(self):
        if self._sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Join the group so the stack programs the mcast route (and so we could
        # receive, were 5353 free). Bind to an ephemeral port for TX.
        try:
            mreq = struct.pack(">4s4s", _ip_to_bytes(MCAST_ADDR),
                               _ip_to_bytes(self.ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception:
            pass
        self._sock = s
        self._dest = socket.getaddrinfo(MCAST_ADDR, MCAST_PORT)[0][-1]

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # --- announce ------------------------------------------------------

    def announce(self):
        # Send one unsolicited response. Sent twice with a short gap is the
        # RFC-recommended pattern for robustness against packet loss.
        if self._sock is None:
            self.open()
        self._sock.sendto(self._pkt, self._dest)

    def tick(self, now_ms=None):
        # Cooperative: call frequently; sends an announcement every `interval`
        # seconds. Returns True if it announced this call.
        if now_ms is None:
            now_ms = time.ticks_ms()
        if time.ticks_diff(now_ms, self._next) >= 0:
            self.announce()
            self._next = time.ticks_add(now_ms, self.interval * 1000)
            return True
        return False

    def run(self):
        # Blocking standalone loop (testing only). Initial burst then steady.
        self.open()
        for _ in range(3):
            self.announce()
            time.sleep_ms(250)
        while True:
            self.tick()
            time.sleep_ms(500)


_responder = None


def start(ip=None, hostname="annealage-pod", port=8266, instance=None,
          txt=None, interval=5, background=False):
    # Build and start advertising. If ip is None, read it from the STA netif.
    # Returns the MDNSResponder. With background=True, spawns a core1 thread
    # (ONLY safe if no other _thread is running -- not the case under the
    # dupterm REPL; use tick() integration there instead).
    global _responder
    if ip is None:
        import network
        ip = network.WLAN(network.STA_IF).ifconfig()[0]
    if txt is None:
        txt = default_txt(port)
    r = MDNSResponder(ip, hostname=hostname, port=port, instance=instance,
                      txt=txt, interval=interval)
    r.open()
    # Initial announce burst so resolvers learn the records immediately.
    for _ in range(3):
        r.announce()
        time.sleep_ms(200)
    _responder = r
    if background:
        import _thread
        _thread.start_new_thread(r.run, ())
    return r


def default_txt(port=8266):
    # TXT record set for the pod. Later phases fill the placeholders.
    import sys
    v = sys.implementation.version
    mpver = ".".join(str(x) for x in v[:3])
    if len(v) > 3 and v[3]:
        mpver += "-" + v[3]
    return [
        "repl-port=%d" % port,
        "usbip-port=3240",     # placeholder, filled by USB/IP phase
        "uart-port=2000",      # placeholder, filled by UART-forward phase
        "carrier-id=",         # placeholder, filled when carrier EEPROM read
        "firmware-version=",   # placeholder, filled from build metadata
        "mp-version=" + mpver,
    ]


def tick():
    # Convenience for cooperative integration into an existing loop.
    if _responder is not None:
        return _responder.tick()
    return False
