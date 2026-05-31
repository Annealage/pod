# Network management bootstrap for the Annealage Pod RP2350.
#
# Brings up CYW43 Wi-Fi and exposes the MicroPython REPL on a TCP socket via
# os.dupterm, so the pod is reachable over Wi-Fi (ampremote socket://). This is
# required because the native USB controller is the DUT host port, leaving no
# USB-CDC REPL on the pod.
#
# Credentials and port come from a config.py on the filesystem (NOT frozen, NOT
# committed) so they change without a firmware rebuild. See config.example.py.
#
# After Wi-Fi is up it also advertises a browsable mDNS service
# (_annealage-pod._tcp) via the native lwIP responder (network.mdns_add_service)
# so host tooling discovers the pod by service type rather than a hardcoded IP.

import network
import socket
import os
import sys
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
    wlan = connect(ssid, pw)
    if not wlan.isconnected():
        print("netboot: Wi-Fi connect failed for SSID", ssid)
        return None
    ip = wlan.ifconfig()[0]
    print("netboot: Wi-Fi up", ip, "REPL on port", port)
    import _thread
    _thread.start_new_thread(_serve, (port,))
    _advertise_mdns(port)
    return ip
