# Wi-Fi + dupterm TCP REPL spike for the Annealage Pod RP2350 pivot.
#
# Proves the pod-management transport: bring up CYW43 Wi-Fi, then expose the
# MicroPython REPL on a TCP socket via os.dupterm so a host can drive it with
# ampremote's socket:// transport (incl. mount-over-socket). The pod has a
# single USB controller earmarked for DUT USB host, so its own management has
# to ride Wi-Fi, not USB; this is that path.
#
# No credentials in this file (it lives in the repo). Pass them to start().

import network
import socket
import os
import time


def connect(ssid, pw, timeout=20):
    w = network.WLAN(network.STA_IF)
    w.active(True)
    if not w.isconnected():
        w.connect(ssid, pw)
        t0 = time.ticks_ms()
        while not w.isconnected() and time.ticks_diff(time.ticks_ms(), t0) < timeout * 1000:
            time.sleep_ms(200)
    return w


_srv = None


def serve_loop(port=8266):
    global _srv
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(1)
    _srv = s
    while True:
        try:
            cl, _ = s.accept()
        except Exception:
            break
        # Route the REPL onto the accepted socket. Keep USB REPL too (index 1).
        os.dupterm(cl)


def start(ssid, pw, port=8266):
    import _thread
    w = connect(ssid, pw)
    if not w.isconnected():
        print("WIFI_FAIL")
        return None
    ip = w.ifconfig()[0]
    _thread.start_new_thread(serve_loop, (port,))
    print("WIFI_IP", ip, "PORT", port)
    return ip
