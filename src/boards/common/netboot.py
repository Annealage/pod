# Single-core asyncio management runtime for the Annealage Pod RP2350.
#
# The pod runs ONE asyncio event loop on core0 that owns the whole management
# plane: the REPL (asyncio.arepl over system stdio = UART + the Wi-Fi socket via
# os.dupterm), a TCP accept loop that dups the REPL onto a connecting client, and
# a Wi-Fi supervisor that connects, keeps the link up, and (re)advertises mDNS.
# The usbip forwarder and the native USB host run underneath, driven by lwIP-RAW
# callbacks and the MicroPython scheduler (serviced during the loop's idle poll);
# nothing else mutates lwIP.
#
# A single lwIP mutator on core0 is the point: it makes the CYW43 inbound-death
# deadlock unreachable. With a second core also mutating lwIP, a pendsv_mutex
# spinlock hold frozen by an SWD halt of the on-pod debug stack became a permanent
# cross-core spin-wait, and cyw43_poll then never drained RX. One core cannot
# reach that - the lock holder always resumes and releases.
#
# This is the pod's ONLY management channel: the native USB port is the DUT host,
# so there is no USB-CDC REPL to fall back to. It therefore has to stay reachable
# - every task is exception-guarded and the Wi-Fi supervisor retries forever, so
# a flaky link or a churning client never strands the pod until a power-cycle.
#
# Credentials and port come from a config.py on the filesystem (NOT frozen, NOT
# committed) so they change without a firmware rebuild. See config.example.py.

import asyncio
import asyncio.arepl as arepl
from micropython import const
import network
import socket
import select
import os
import sys
import time


# Re-association interval while Wi-Fi is down. Longer than a typical association
# so we don't keep restarting an in-progress connect every cycle; short enough
# that a cold-boot CYW43 drop (common on the first attempt) recovers in seconds.
_WIFI_RETRY_MS = const(10000)
# REPL listener poll cadence. The listener is non-blocking and polled
# cooperatively so accept never blocks the loop; sub-second latency is irrelevant
# for a management connection.
_ACCEPT_POLL_MS = const(150)
# Socket-poll events that mean the REPL client is gone: HUP/ERR (reset) or NVAL
# (closed). The dupterm layer closes the client socket on disconnect, so a dead
# cached handle polls one of these. POLLNVAL is not exported by select, so its
# numeric value (MP_STREAM_POLL_NVAL = 0x20) is OR'd in.
_POLL_DEAD = select.POLLHUP | select.POLLERR | 0x20
# Pause before restarting the REPL task after it returns (see main()). Only caps
# the restart rate if stdin is permanently gone; a normal client disconnect
# restarts fast enough to be invisible.
_REPL_RESTART_MS = const(100)


def _pm_none(wlan):
    # Disable CYW43 Wi-Fi power-save so the radio stays active. Without this the
    # link drops on idle and re-associates with a new DHCP IP - the source of the
    # Wi-Fi flapping (unreachable IP, REPL going away) seen during bring-up.
    try:
        wlan.config(pm=network.WLAN.PM_NONE)
    except Exception:
        pass

# The pod's network identity. Derived from the RP2350 chip ID so that several
# pods coexist on one LAN with one firmware image and no per-unit config: a
# fixed name would make two pods collide on both DHCP and mDNS. The suffix is
# the low 5 hex digits of machine.unique_id() - short enough for
# network.hostname() (32 chars) and for a discovery listing, wide enough to
# separate a bench of pods. Hosts find pods by browsing the _annealage-pod._tcp
# service type, not by a fixed name, so a per-unit name costs discovery nothing.
_POD_NAME = None


def pod_name():
    """This pod's hostname and mDNS instance name, e.g. "annealage-pod-8b97a"."""
    global _POD_NAME
    if _POD_NAME is None:
        try:
            import machine
            import binascii

            uid = binascii.hexlify(machine.unique_id()).decode()
            _POD_NAME = "annealage-pod-" + uid[-5:]
        except Exception:
            # Identity derivation must never stop the pod reaching the network:
            # fall back to the generic name, which collides only against another
            # pod that also failed here.
            _POD_NAME = "annealage-pod"
    return _POD_NAME


def _advertise_mdns(repl_port, uart_port=None):
    # Advertise a browsable service via the native lwIP mDNS responder. Guarded
    # so any failure (missing API, responder not ready) does not stop the REPL.
    # uart_port: include the "uart-port" TXT key only when a bound port is
    # passed; omit it entirely when None so no client follows a dangling port.
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
        "carrier-id": str(carrier_id),
        "mp-version": mp_version,
    }
    if uart_port is not None:
        txt["uart-port"] = str(uart_port)
    try:
        slot = network.mdns_add_service(
            pod_name(), "_annealage-pod", "tcp", repl_port, txt=txt
        )
        print("netboot: mDNS service advertised, slot", slot)
        return slot
    except Exception as e:
        print("netboot: mDNS advertise failed:")
        sys.print_exception(e)
        return None


async def _wifi_supervisor(wlan, ssid, pw, port, uart_port=None):
    # Connect, keep the link up, and (re)advertise mDNS on each (re)connect. Runs
    # forever and is exception-guarded so a transient radio error can never kill
    # the pod's only management channel. The first iteration attempts the connect
    # (last_attempt starts a full interval in the past).
    # uart_port: the bound UART listener port, or None if not bound; passed
    # through to _advertise_mdns so each re-advertise reflects the real state.
    was_up = False
    last_attempt = time.ticks_add(time.ticks_ms(), -_WIFI_RETRY_MS - 1)
    while True:
        try:
            up = wlan.isconnected()
            if up and not was_up:
                _pm_none(wlan)  # a reconnect may have reset power-save
                try:
                    print("netboot: Wi-Fi (re)connected", wlan.ifconfig()[0])
                    _advertise_mdns(port, uart_port)
                except Exception:
                    pass
            elif not up and time.ticks_diff(time.ticks_ms(), last_attempt) > _WIFI_RETRY_MS:
                last_attempt = time.ticks_ms()
                # A bare connect() does not re-associate after a drop on CYW43:
                # disconnect() first clears the supplicant state so the next
                # connect() actually re-associates (the cold-boot connect works
                # because the interface starts fresh). Harmless on first boot.
                try:
                    wlan.disconnect()
                except Exception:
                    pass
                await asyncio.sleep_ms(500)
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
        await asyncio.sleep_ms(2000)


async def _repl_accept(port):
    # Bind the REPL listener and serve ONE client at a time. The pod does NOT evict
    # a live session: while a client holds the REPL a new connection is refused (a
    # one-line BUSY notice, then closed without attaching it), so an agent that
    # accidentally dials the wrong pod can't knock the active one off.
    #
    # The active client is tracked in `cur` and its disconnect is detected by polling
    # THAT handle: a peer close makes it poll HUP/ERR, a fully closed handle polls
    # NVAL, while a live one polls RD/WR. Polling the socket touches no shared REPL
    # state. We never read the socket: arepl owns the read via sys.stdin, into which
    # os.dupterm aggregates the slot. On a detected disconnect we detach the slot
    # before closing the socket (see the dead branch for why that order matters).
    # AF_INET6 + "::" = dual-stack (v4+v6) via modlwip's listen() promotion of
    # an IP6_ADDR_ANY listener to IPADDR_TYPE_ANY; one socket serves both.
    s = socket.socket(socket.AF_INET6)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("::", port))
    s.listen(4)
    s.setblocking(False)
    poller = select.poll()
    poller.register(s, select.POLLIN)
    live = select.poll()
    cur = None
    while True:
        try:
            # Has the active session ended? On disconnect the client handle polls
            # dead (HUP/ERR on a peer close, NVAL once closed).
            if cur is not None:
                dead = False
                try:
                    for _o, ev in live.poll(0):
                        if ev & _POLL_DEAD:
                            dead = True
                except Exception:
                    dead = True
                if dead:
                    try:
                        live.unregister(cur)
                    except Exception:
                        pass
                    # Detach the dupterm slot BEFORE closing the socket, so the slot
                    # never points at a closed fd. A lingering closed slot polls NVAL,
                    # and a dupterm poll that doesn't mask closed slots would wake
                    # arepl's blocking stdin read on a dead fd and park the single-core
                    # loop (the REPL is the pod's only management path). Detaching is
                    # the app-side guard; current MicroPython also masks it, so the two
                    # together keep stdin on the UART alone and the loop live.
                    try:
                        os.dupterm(None, 0)
                    except Exception:
                        pass
                    try:
                        cur.close()
                    except Exception:
                        pass
                    cur = None
            if poller.poll(0):
                try:
                    cli, addr = s.accept()
                except OSError:
                    cli = None
                if cli is not None:
                    cli.setblocking(False)
                    if cur is None:
                        cur = cli
                        try:
                            os.dupterm(cli)
                            live.register(cli, select.POLLIN)
                            print("netboot: REPL client", addr)
                        except Exception:
                            try:
                                os.dupterm(None, 0)
                            except Exception:
                                pass
                            try:
                                cli.close()
                            except Exception:
                                pass
                            cur = None
                    else:
                        # A session is already attached: refuse without evicting.
                        try:
                            cli.send(b"annealage-pod: BUSY - REPL in use by another client\r\n")
                        except Exception:
                            pass
                        try:
                            cli.close()
                        except Exception:
                            pass
                        print("netboot: REPL busy, refused", addr)
        except Exception as e:
            try:
                sys.print_exception(e)
            except Exception:
                pass
        await asyncio.sleep_ms(_ACCEPT_POLL_MS)


async def main():
    # The pod's single-core management runtime. Brings up Wi-Fi + the socket REPL
    # accept loop as background tasks, then runs the asyncio REPL as the long-lived
    # foreground task. Returns only if stdin closes (it does not, while UART is the
    # primary), at which point boot falls through to the normal UART REPL.
    try:
        import config
    except ImportError:
        config = None
    ssid = getattr(config, "WIFI_SSID", None) if config else None
    port = getattr(config, "REPL_PORT", 8266) if config else 8266
    # Claim the per-unit identity before the interface goes up, so the DHCP
    # client offers it and the lwIP mDNS responder answers <pod_name>.local.
    try:
        network.hostname(pod_name())
    except Exception as e:
        sys.print_exception(e)
    if ssid:
        pw = getattr(config, "WIFI_PASSWORD", "")
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        _pm_none(wlan)

        # Bind the UART bridge listener synchronously before any task fires an
        # mDNS advertise, so the first advertise reflects the real bind state.
        # uart_port is None on failure; _advertise_mdns omits the TXT key then.
        # The entire import+configure+bind block is guarded so a missing or
        # broken uart_bridge module degrades to no-bridge rather than killing
        # the Wi-Fi management plane (supervisor + socket REPL).
        _ub = None
        uart_cfg = {}
        uart_port = None
        try:
            from annealage_pod import uart_bridge as _ub
            if config:
                for k in ("baudrate", "bits", "parity", "stop"):
                    v = getattr(config, "DUT_UART_" + k.upper(), None)
                    if v is not None:
                        uart_cfg[k] = v
            uart_port = _ub.bind(
                getattr(config, "UART_PORT", 2000) if config else 2000
            )
        except Exception as _e:
            sys.print_exception(_e)
            print("netboot: uart_bridge unavailable, bridge not started")

        # Do NOT block boot on the connect: a slow or flaky AP would delay the
        # REPL, the pod's only management channel. The supervisor drives the
        # connect on its first iteration and retries forever; the accept loop
        # binds the REPL socket immediately so the pod is reachable as soon as the
        # link is up.
        asyncio.create_task(_wifi_supervisor(wlan, ssid, pw, port, uart_port))
        asyncio.create_task(_repl_accept(port))
        if uart_port is not None:
            asyncio.create_task(_ub.serve(uart_port, uart_cfg))
            print("netboot: UART bridge on port", uart_port)
        else:
            print("netboot: UART bridge bind failed, not advertised")
        print("netboot: single-core runtime, Wi-Fi supervisor + REPL on port", port)
    else:
        print("netboot: no config.py/WIFI_SSID, REPL on UART only")
    # persistent=True: Ctrl-D re-prompts, keeping the console up for the life of
    # the pod. stop_loop_on_exit=False: the REPL task can never tear down the
    # management plane. One sys.stdin reader serves UART and the dupterm socket.
    #
    # arepl.task() returns when the stream feeding it goes away, which
    # persistent= does not cover: that governs Ctrl-D only. A socket REPL client
    # that disconnects mid-session is exactly that, and the REPL is meant to end
    # there and be restarted rather than keep reading a dead source. This is the
    # last await in main(), so letting it return would end asyncio.run() and
    # cancel the accept loop, Wi-Fi supervisor and UART bridge with it, leaving
    # a pod that still completes TCP handshakes on the REPL port but serves
    # nobody, reachable only over the fallback UART REPL until someone resets
    # it. Restart the REPL instead, so a disconnecting client costs its own
    # session and nothing more.
    while True:
        try:
            await arepl.task(persistent=True, stop_loop_on_exit=False)
        except Exception as _e:
            # A client that resets mid-session can surface as OSError out of the
            # REPL's own stdin handling. Swallow it for the same reason the
            # return is swallowed: the REPL is one session, the management plane
            # is everything.
            sys.print_exception(_e)
        await asyncio.sleep_ms(_REPL_RESTART_MS)
