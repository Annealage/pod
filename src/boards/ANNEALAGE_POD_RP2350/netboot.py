# Network management bootstrap for the Annealage Pod RP2350.
#
# Brings up CYW43 Wi-Fi and exposes the MicroPython REPL on a TCP socket via
# os.dupterm, so the pod is reachable over Wi-Fi (ampremote socket://). This is
# the pod's ONLY management channel: the native USB controller is the DUT host
# port, so there is no USB-CDC REPL to fall back to. It therefore has to be
# resilient - a flaky Wi-Fi link or a churning client must never leave the pod
# unreachable until a power-cycle.
#
# Credentials and port come from a config.py on the filesystem (NOT frozen, NOT
# committed) so they change without a firmware rebuild. See config.example.py.
#
# Resilience (one background thread does both jobs; rp2 gives one extra thread):
#   - Wi-Fi supervisor: reconnect on drop, keep retrying if the boot connect
#     failed, re-advertise mDNS on (re)connect. (connect-once at boot is not
#     enough; a cold-boot CYW43 failure or a later drop would strand the pod.)
#   - REPL serve loop: poll-based (non-blocking) accept so the loop stays
#     responsive and is Ctrl-C-interruptible; on a new client it detaches and
#     closes the previous one so a reconnecting host always gets a clean slot;
#     the loop is exception-guarded so a transient error can never kill the
#     pod's only management channel.

import network
import socket
import select
import os
import sys
import time


def _pm_none(wlan):
    # Disable CYW43 Wi-Fi power-save so the radio stays active. Without this the
    # link drops on idle and re-associates with a new DHCP IP - the source of the
    # Wi-Fi flapping (unreachable IP, REPL going away) seen during bring-up.
    # Validated: with PM_NONE, 0 disconnect-seconds and a single stable IP over a
    # 25s window; without it the IP churned across the session.
    try:
        wlan.config(pm=network.WLAN.PM_NONE)
    except Exception:
        pass


def connect(ssid, pw, timeout=15, retries=4):
    # Cold-boot CYW43 often drops the first connect attempt; retry with a
    # disconnect between tries. Without this the frozen firmware can come up
    # unreachable (no USB-CDC REPL to fall back to).
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    _pm_none(wlan)
    for _ in range(retries):
        if wlan.isconnected():
            return wlan
        try:
            wlan.disconnect()
        except Exception:
            pass
        time.sleep_ms(500)
        wlan.connect(ssid, pw)
        t0 = time.ticks_ms()
        while not wlan.isconnected() and time.ticks_diff(time.ticks_ms(), t0) < timeout * 1000:
            time.sleep_ms(250)
    return wlan


def _serve_supervise(port, wlan, ssid, pw):
    # One background thread: serve the REPL socket AND keep Wi-Fi up. Both jobs
    # are non-blocking so neither starves the other, and the whole loop is
    # exception-guarded so it never dies.
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(1)
    s.setblocking(False)
    poller = select.poll()
    poller.register(s, select.POLLIN)
    cur = None
    was_up = wlan.isconnected()
    next_wifi = time.ticks_add(time.ticks_ms(), 5000)
    # Allow the first down-check to re-associate immediately, then rate-limit.
    last_attempt = time.ticks_add(time.ticks_ms(), -20000)
    while True:
        try:
            # --- serve: accept in 500 ms slices (non-blocking, interruptible) -
            if poller.poll(500):
                try:
                    cli, addr = s.accept()
                except OSError:
                    cli = None
                if cli is not None:
                    # Evict any previous client first, so a reconnecting host
                    # always lands on a clean REPL slot (covers the case where
                    # the old client died without the REPL noticing the EOF).
                    try:
                        os.dupterm(None)
                    except Exception:
                        pass
                    if cur is not None:
                        try:
                            cur.close()
                        except Exception:
                            pass
                    cur = cli
                    try:
                        os.dupterm(cli)
                        print("netboot: REPL client", addr)
                    except Exception:
                        try:
                            cli.close()
                        except Exception:
                            pass
                        cur = None
            # --- supervise: keep Wi-Fi connected ----------------------------
            if time.ticks_diff(time.ticks_ms(), next_wifi) >= 0:
                next_wifi = time.ticks_add(time.ticks_ms(), 5000)
                up = wlan.isconnected()
                if up and not was_up:
                    _pm_none(wlan)   # reconnect may have reset power-save
                    try:
                        print("netboot: Wi-Fi (re)connected", wlan.ifconfig()[0])
                        _advertise_mdns(port)
                    except Exception:
                        pass
                elif not up:
                    # Non-blocking re-association, rate-limited so we don't keep
                    # restarting an in-progress association every cycle.
                    if time.ticks_diff(time.ticks_ms(), last_attempt) > 15000:
                        last_attempt = time.ticks_ms()
                        try:
                            wlan.connect(ssid, pw)
                        except Exception:
                            pass
                was_up = up
        except Exception as e:
            try:
                sys.print_exception(e)
            except Exception:
                pass
            time.sleep_ms(200)


def _advertise_mdns(repl_port):
    # Advertise a browsable service via the native lwIP mDNS responder. Guarded
    # so any failure (missing API, responder not ready) does not stop the REPL.
    try:
        import config
    except ImportError:
        config = None
    carrier_id = getattr(config, "CARRIER_ID", "") if config else ""
    try:
        mp_version = ".".join(str(x) for x in sys.implementation.version)
    except Exception:
        mp_version = sys.version
    txt = {
        "repl-port": str(repl_port),
        "usbip-port": "3240",
        "uart-port": "2000",
        "carrier-id": str(carrier_id),
        "mp-version": mp_version,
    }
    try:
        slot = network.mdns_add_service(
            "annealage-pod", "_annealage-pod", "tcp", repl_port, txt=txt
        )
        print("netboot: mDNS service advertised, slot", slot)
        return slot
    except Exception as e:
        print("netboot: mDNS advertise failed:")
        sys.print_exception(e)
        return None


def start():
    try:
        import config
    except ImportError:
        print("netboot: no config.py, skipping Wi-Fi bring-up")
        return None
    ssid = getattr(config, "WIFI_SSID", None)
    if not ssid:
        print("netboot: config.WIFI_SSID not set")
        return None
    pw = getattr(config, "WIFI_PASSWORD", "")
    port = getattr(config, "REPL_PORT", 8266)
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    _pm_none(wlan)
    # Do NOT block boot on the connect: a slow or flaky AP would delay the REPL,
    # and the REPL is the pod's only management channel. Kick off association and
    # hand everything to the serve+supervise thread, which binds the REPL socket
    # immediately, brings Wi-Fi up (retrying forever), and re-advertises mDNS the
    # moment the link is up - so the pod is reachable as soon as it can be and is
    # never stranded by a bad boot.
    try:
        wlan.connect(ssid, pw)
    except Exception:
        pass
    print("netboot: Wi-Fi supervisor + REPL starting on port", port)
    import _thread
    _thread.start_new_thread(_serve_supervise, (port, wlan, ssid, pw))
    return None
