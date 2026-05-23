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
        # R23 step 1: disable Wi-Fi power save so the radio stays active.
        # PM_NONE = 0 in IDF (WIFI_PS_NONE); confirmed exported as
        # network.WLAN.PM_NONE in the running esp32 port.
        try:
            sta.config(pm=network.WLAN.PM_NONE)
            print("annealage_pod.boot: Wi-Fi PS disabled (PM_NONE)")
        except Exception as exc:  # noqa: BLE001
            print("annealage_pod.boot: Wi-Fi PS disable failed: {!r}".format(exc))
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
    # Activate the Python-level USB host before usbip.start() calls
    # usbhost_start() in C.  Without this, usbhost_start() would call
    # mp_usbh_init_tuh() while machine.USBHost.active is False; a later
    # machine.USBHost().active(True) call would then re-init the USB PHY
    # and drop any already-connected device.
    try:
        import machine as _machine
        _machine.USBHost().active(True)
    except (ImportError, AttributeError):
        pass
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
    # SPI engine is broken on ESP32-S3 (R14); bit-bang is the working
    # backend until that is resolved. set_swd_mode(1) selects bit-bang.
    set_mode = getattr(dapprobe, "set_swd_mode", None)
    if set_mode is not None:
        try:
            set_mode(1)
        except Exception as exc:  # noqa: BLE001
            print("annealage_pod.boot: dapprobe.set_swd_mode(1) raised {!r}".format(exc))
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


def attach_uartcdc():
    """Register the synthetic CDC UART device with the usbip server. Returns True if attached.

    Falls back to uartbridge (TCP) if the uartcdc C module is not present,
    i.e. when built with ANNEALAGE_POD_UART_BACKEND=tcp.
    """
    try:
        import uartcdc  # type: ignore
    except ImportError:
        return start_uartbridge()
    try:
        from . import _pinmap
        uartcdc.attach(_pinmap.DUT_UART_NUM, _pinmap.DUT_UART_TX, _pinmap.DUT_UART_RX, 115200)
        return True
    except Exception as exc:  # noqa: BLE001
        print("annealage_pod.boot: uartcdc.attach failed: {!r}".format(exc))
        return False


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


def _repl_accept_loop(listener):
    # Run forever in a background thread: accept a client, dup the REPL
    # onto it, wait for the client to disconnect (REPL's read path
    # detaches the dupterm slot on EOF), fire supervisor.run_cleanup(),
    # accept the next client.
    try:
        import time as _time
    except ImportError:
        import utime as _time
    while True:
        try:
            cli, addr = listener.accept()
        except OSError as exc:
            print("annealage_pod.boot: REPL accept loop exiting: {!r}".format(exc))
            return
        print("annealage_pod.boot: REPL client connected from {}".format(addr))
        try:
            os.dupterm(cli)
        except (TypeError, AttributeError):
            print("annealage_pod.boot: os.dupterm() unavailable; dropping client")
            try:
                cli.close()
            except Exception:  # noqa: BLE001
                pass
            continue
        except Exception as exc:  # noqa: BLE001
            print("annealage_pod.boot: dupterm failed: {!r}".format(exc))
            try:
                cli.close()
            except Exception:  # noqa: BLE001
                pass
            continue
        while True:
            _time.sleep_ms(500)
            try:
                cur = os.dupterm(None, 0)
            except Exception:  # noqa: BLE001
                cur = None
                break
            if cur is None or cur is not cli:
                break
            try:
                os.dupterm(cur)
            except Exception:  # noqa: BLE001
                break
        try:
            cli.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            supervisor.run_cleanup()
        except Exception as exc:  # noqa: BLE001
            print("annealage_pod.boot: cleanup hook raised {!r}".format(exc))
        print("annealage_pod.boot: REPL client disconnected; cleanup ran")


def start_repl_thread(listener):
    """Spawn the REPL accept loop in a background thread. Returns thread id or None."""
    if listener is None:
        return None
    try:
        import _thread
    except ImportError:
        print("annealage_pod.boot: _thread unavailable; REPL accept loop not spawned")
        return None
    try:
        return _thread.start_new_thread(_repl_accept_loop, (listener,))
    except Exception as exc:  # noqa: BLE001
        print("annealage_pod.boot: _thread.start_new_thread raised {!r}".format(exc))
        return None


# --- Top-level orchestration ---------------------------------------------


def up(creds_path=_CREDS_PATH, repl_port=8266, mark_ota_valid=True):
    """Run the full Annealage Pod boot sequence; designed to be called once.

    Returns a dict summarising which services started.
    """
    status = {
        "wifi": False,
        "mdns": None,
        "vtarget": False,
        "dut_usb": False,
        "usbip": False,
        "dapprobe": False,
        "uartcdc": False,
        "repl_listener": False,
        "repl_loop": False,
        "version": _version.__version__,
        "cleanup_hooks": len(supervisor.list_hooks()),
    }
    creds = load_credentials(creds_path)
    sta = wifi_connect(creds)
    if sta is not None and getattr(sta, "isconnected", lambda: False)():
        status["wifi"] = True
        status["mdns"] = mdns_announce()
    # Energise the DUT power rails before bringing up usbip / dapprobe so the
    # DUT is alive when a debugger attaches. Phase 3 dev-kit topology drives DUT
    # USB VBUS unconditionally; rev1 PCB will gate this via the supervisor
    # lifecycle.
    try:
        from . import power as _power

        _power.dut_usb.on()
        _power.vtarget.on()
        status["dut_usb"] = True
        status["vtarget"] = True
    except Exception as exc:  # noqa: BLE001
        print("annealage_pod.boot: power rail bring-up failed: {!r}".format(exc))
        status["dut_usb"] = False
        status["vtarget"] = False
    status["usbip"] = start_usbip()
    status["dapprobe"] = attach_dapprobe()
    status["uartcdc"] = attach_uartcdc()
    listener = start_repl_socket(repl_port)
    status["repl_listener"] = listener is not None
    status["repl_loop"] = start_repl_thread(listener) is not None
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
