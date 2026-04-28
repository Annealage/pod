# Annealage Pod: boot orchestration.
#
# Wires up the post-power-on sequence (spec.md §5.1, §5.2, §5.3):
#   1. network.WLAN(STA_IF) bring-up using credentials from NVS or
#      a JSON file at /credentials.json (a credentials.example.json
#      template ships in this package's source tree).
#   2. mDNS announcement (annealage_pod-<chipid>.local with TXT records).
#   3. start C usbip server, attach C dapprobe, start C uartbridge.
#   4. dup REPL to TCP 8266 plus UART0 (spec.md §5.3).
#   5. register the supervisor cleanup hook on TCP REPL disconnect.
#
# Each subsystem is best-effort: if a service fails to start we log
# the failure and keep going.

import os
import sys

try:
    import network
except ImportError:
    network = None

try:
    import socket as _socket
except ImportError:
    _socket = None

try:
    import json as _json
except ImportError:
    _json = None

from . import _version, supervisor


# --- Credentials ----------------------------------------------------------

_CREDS_PATH = "/credentials.json"


def load_credentials(path=_CREDS_PATH):
    """Load Wi-Fi credentials from `path`. Returns dict or None."""
    if _json is None:
        return None
    try:
        with open(path, "r") as fh:
            return _json.load(fh)
    except OSError:
        return None
    except ValueError:
        print("annealage_pod.boot: credentials file at {} is not valid JSON".format(path))
        return None


# --- Wi-Fi ----------------------------------------------------------------


def wifi_connect(creds, timeout_s=15):
    """Bring up STA Wi-Fi using `creds` (dict with ssid/password). Returns the WLAN."""
    if network is None:
        print("annealage_pod.boot: network module unavailable")
        return None
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    if not sta.isconnected():
        ssid = creds.get("ssid") if creds else None
        password = creds.get("password") if creds else None
        if not ssid:
            print("annealage_pod.boot: no SSID in credentials; skipping connect")
            return sta
        sta.connect(ssid, password)
        try:
            import time as _time

            deadline = _time.ticks_add(_time.ticks_ms(), timeout_s * 1000)
            while not sta.isconnected() and _time.ticks_diff(deadline, _time.ticks_ms()) > 0:
                _time.sleep_ms(200)
        except (ImportError, AttributeError):
            pass
    if sta.isconnected():
        print("annealage_pod.boot: Wi-Fi up, ifconfig={}".format(sta.ifconfig()))
    else:
        print("annealage_pod.boot: Wi-Fi connect timed out")
    return sta


# --- mDNS -----------------------------------------------------------------


def mdns_announce(hostname=None):
    """Announce annealage_pod-<chipid>.local with the documented TXT records."""
    try:
        import mdns as _mdns
    except ImportError:
        # MicroPython esp32 port doesn't ship a top-level mdns yet; the
        # IDF mdns component is on the C side. WS-H or WS-E follow-up
        # may add a MicroPython binding. Until then this is a no-op.
        return None
    if hostname is None:
        try:
            import machine

            uid = machine.unique_id()
            hostname = "annealage-pod-{}".format("".join("{:02x}".format(b) for b in uid)[-6:])
        except Exception:  # noqa: BLE001
            hostname = "annealage-pod"
    try:
        _mdns.hostname(hostname)
        _mdns.add_service("_annealage-pod", "_tcp", 3240, {
            "carrier-id": "",
            "firmware-version": _version.__version__,
            "mp-version": sys.version,
            "repl-port": "8266",
            "uart-port": "2000",
        })
    except (AttributeError, OSError) as exc:
        print("annealage_pod.boot: mDNS bring-up failed: {!r}".format(exc))
        return None
    return hostname


# --- C-module bring-up ----------------------------------------------------


def start_usbip():
    """Start the C usbip TCP server on port 3240. Returns True if started."""
    try:
        import usbip  # type: ignore
    except ImportError:
        return False
    start = getattr(usbip, "start", None)
    if start is None:
        return False
    start()
    return True


def attach_dapprobe():
    """Register the synthetic CMSIS-DAP-v2 device into usbip. Returns True if attached."""
    try:
        import dapprobe  # type: ignore
    except ImportError:
        return False
    # WS-C exposes only `start()` today; `attach()` is the documented
    # name in spec.md §4.4. Try `attach()` first, fall back to `start()`.
    attach = getattr(dapprobe, "attach", None) or getattr(dapprobe, "start", None)
    if attach is None:
        return False
    attach()
    return True


def start_uartbridge():
    """Start the C uartbridge TCP forwarder on the configured port. Returns True if started."""
    try:
        import uartbridge  # type: ignore
    except ImportError:
        return False
    start = getattr(uartbridge, "start", None)
    if start is None:
        return False
    start()
    return True


# --- REPL dup -------------------------------------------------------------


_repl_listener = None


def start_repl_socket(port=8266):
    """Bind a TCP listener for the REPL on `port`. Returns the listening socket."""
    global _repl_listener
    if _socket is None:
        return None
    if _repl_listener is not None:
        return _repl_listener
    addr = _socket.getaddrinfo("0.0.0.0", port)[0][-1]
    s = _socket.socket()
    s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)
    _repl_listener = s
    return s


def repl_accept_one(listener):
    """Accept one REPL client and dup the REPL onto it.

    Caller is expected to invoke supervisor.run_cleanup() when the
    client disconnects.
    """
    cli, _ = listener.accept()
    try:
        os.dupterm(cli)
    except (TypeError, AttributeError):
        print("annealage_pod.boot: os.dupterm() not available on this port")
    return cli


# --- Top-level orchestration ---------------------------------------------


def up(creds_path=_CREDS_PATH, repl_port=8266, mark_ota_valid=True):
    """Run the full Annealage Pod boot sequence; designed to be called once.

    Returns a dict summarising which services started.
    """
    status = {
        "wifi": False,
        "mdns": None,
        "usbip": False,
        "dapprobe": False,
        "uartbridge": False,
        "repl_listener": False,
        "version": _version.__version__,
        "cleanup_hooks": len(supervisor.list_hooks()),
    }
    creds = load_credentials(creds_path)
    sta = wifi_connect(creds)
    if sta is not None and getattr(sta, "isconnected", lambda: False)():
        status["wifi"] = True
        status["mdns"] = mdns_announce()
    status["usbip"] = start_usbip()
    status["dapprobe"] = attach_dapprobe()
    status["uartbridge"] = start_uartbridge()
    listener = start_repl_socket(repl_port)
    status["repl_listener"] = listener is not None
    # Mark the running OTA image valid only after critical services
    # have started successfully (spec.md §4.2 rollback).
    if mark_ota_valid and status["wifi"]:
        try:
            from .ops import ota as _ota

            _ota.mark_valid()
        except Exception as exc:  # noqa: BLE001
            print("annealage_pod.boot: ota.mark_valid failed: {!r}".format(exc))
    print("annealage_pod.boot: status={}".format(status))
    return status


if __name__ == "__main__":
    up()
