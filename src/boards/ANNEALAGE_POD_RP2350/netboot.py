# Network management bootstrap for the Annealage Pod RP2350.
#
# Brings up CYW43 Wi-Fi and exposes the MicroPython REPL on a TCP socket via
# os.dupterm, so the pod is reachable over Wi-Fi (ampremote socket://). This is
# required because the native USB controller is the DUT host port, leaving no
# USB-CDC REPL on the pod.
#
# Credentials and port come from a config.py on the filesystem (NOT frozen, NOT
# committed) so they change without a firmware rebuild. See config.example.py.

import network
import socket
import os
import time


def connect(ssid, pw, timeout=15, retries=4):
    # Cold-boot CYW43 often drops the first connect attempt; retry with a
    # disconnect between tries. Without this the frozen firmware can come up
    # unreachable (no USB-CDC REPL to fall back to).
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
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


def _serve(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(1)
    while True:
        try:
            cl, _ = s.accept()
        except Exception:
            break
        os.dupterm(cl)


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
    wlan = connect(ssid, pw)
    if not wlan.isconnected():
        print("netboot: Wi-Fi connect failed for SSID", ssid)
        return None
    ip = wlan.ifconfig()[0]
    print("netboot: Wi-Fi up", ip, "REPL on port", port)
    import _thread
    _thread.start_new_thread(_serve, (port,))
    return ip
