# UART-over-TCP bridge for the Annealage Pod RP2350.
#
# Runs as a background asyncio task on core0 alongside _wifi_supervisor and
# _repl_accept. Must stay on the single-core asyncio runtime - it cannot be a
# second lwIP mutator (that is what produced the CYW43 inbound-death deadlock:
# a second core mutating lwIP held a pendsv_mutex the SWD halt froze, so
# cyw43_poll never drained RX again).
#
# The bridge opens machine.UART once, binds a dual-stack AF_INET6 listener,
# and pumps bytes between the UART and one TCP client per cooperative poll
# cycle. It does NOT touch os.dupterm and shares no state with _repl_accept.

import asyncio
import machine
import select
import socket
import sys
from micropython import const

from . import _rp2_pinmap as _pins

# Poll cadence while a client is attached. At 115200 baud the UART RX FIFO
# fills at ~11.5 KB/s; 5 ms lets at most ~58 bytes accumulate before a drain,
# well within the RP2350 hardware FIFO (32 bytes) plus the machine.UART RX
# buffer. Tighten this if RX-FIFO overrun appears under sustained output.
_UART_POLL_MS = const(5)
# Poll cadence while no client is attached - matches the REPL accept cadence.
_IDLE_POLL_MS = const(150)
# Max bytes to forward from TCP -> UART per cycle; matches a typical terminal
# keystroke burst.
_TCP_RECV_MAX = const(256)
# Max bytes forwarded UART->TCP per send call. Kept well under one MSS (1460 B)
# so that a single tcp_write() call cannot exhaust the lwIP pbuf pool while the
# USB/IP forwarder is also allocating pbufs, reducing the probability of the
# ERR_MEM retry path inside lwip_tcp_send triggering a multi-second busy delay.
_TCP_SEND_MAX = const(512)

# Dead-client poll bitmask. POLLNVAL (0x20) is MP_STREAM_POLL_NVAL; it is not
# exported by MicroPython's select module so its numeric value is OR'd in.
_POLL_DEAD = select.POLLHUP | select.POLLERR | 0x20


def bind(port: int):
    """Bind the dual-stack UART listener and return the bound port.

    Returns the port integer on success, or None if the bind fails. The caller
    uses the return value to gate mDNS advertising: if None, the uart-port TXT
    key is omitted so a client never follows a dangling advertise.

    The listener socket is stored module-level so serve() can use it without a
    second bind. Call bind() once from main() before asyncio.create_task().
    """
    global _server_sock
    try:
        s = socket.socket(socket.AF_INET6)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("::", port))
        s.listen(1)
        s.setblocking(False)
        _server_sock = s
        return port
    except Exception as e:
        try:
            sys.print_exception(e)
        except Exception:
            pass
        _server_sock = None
        return None


_server_sock = None


async def serve(uart_port: int, uart_cfg: dict = None):
    """Background task: accept one TCP client and bridge it to the DUT UART.

    uart_port: TCP port (returned by bind()).
    uart_cfg: dict with optional keys baudrate, bits, parity, stop (all have
              defaults matching 115200-8N1). Pin numbers come from _rp2_pinmap.

    The UART is opened once at task start and stays open for the task lifetime.
    The listener was already bound by bind() in main() before this task was
    created; serve() simply accepts connections on that socket.

    A second connection while a client is attached is refused with a one-line
    notice (BUSY) and closed without evicting the live session, matching the
    _repl_accept discipline.

    Each poll cycle:
      1. Dead-client detection (HUP/ERR/NVAL on the live poller).
      2. Accept a waiting connection if idle.
      3. If a client is attached: drain UART->TCP (uart.any()/uart.read(n)/
         client.send) and TCP->UART (poll readable, client.recv, uart.write).
      4. await asyncio.sleep_ms(_UART_POLL_MS while attached, _IDLE_POLL_MS
         while idle).

    Any exception inside the cycle is caught, printed, and the cycle continues;
    the task never exits, so the pod's listener never silently disappears.
    """
    global _server_sock
    if uart_cfg is None:
        uart_cfg = {}

    baudrate = uart_cfg.get("baudrate", 115200)
    bits = uart_cfg.get("bits", 8)
    parity = uart_cfg.get("parity", None)
    stop = uart_cfg.get("stop", 1)

    s = _server_sock
    if s is None:
        print("uart_bridge: no server socket - bridge not started")
        return

    try:
        uart = machine.UART(
            _pins.DUT_UART_NUM,
            baudrate=baudrate,
            bits=bits,
            parity=parity,
            stop=stop,
            tx=machine.Pin(_pins.DUT_UART_TX),
            rx=machine.Pin(_pins.DUT_UART_RX),
        )
    except Exception as e:
        sys.print_exception(e)
        print("uart_bridge: UART open failed, closing listener")
        try:
            s.close()
        except Exception:
            pass
        _server_sock = None
        return
    print("uart_bridge: UART%d open, %d baud, GP%d/GP%d, listening on port %d"
          % (_pins.DUT_UART_NUM, baudrate, _pins.DUT_UART_TX,
             _pins.DUT_UART_RX, uart_port))

    poller = select.poll()
    poller.register(s, select.POLLIN)
    live = select.poll()
    cur = None
    pending = b""  # unsent UART->TCP tail from a short send

    while True:
        try:
            # Dead-client detection.
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
                    try:
                        cur.close()
                    except Exception:
                        pass
                    cur = None
                    pending = b""
                    print("uart_bridge: client disconnected")

            # Accept a new connection when idle.
            if poller.poll(0):
                try:
                    cli, addr = s.accept()
                except OSError:
                    cli = None
                if cli is not None:
                    cli.setblocking(False)
                    if cur is None:
                        cur = cli
                        live.register(cli, select.POLLIN | select.POLLOUT)
                        print("uart_bridge: client", addr)
                    else:
                        try:
                            cli.send(
                                b"annealage-pod: BUSY - UART bridge in use by another client\r\n"
                            )
                        except Exception:
                            pass
                        try:
                            cli.close()
                        except Exception:
                            pass
                        print("uart_bridge: BUSY, refused", addr)

            # Bridge bytes while a client is attached.
            if cur is not None:
                # Drain UART -> TCP. Gate each send on POLLOUT so that a full
                # TCP send buffer is never handed to lwip_tcp_send while it has
                # no window: that path can busy-delay up to 10 s inside the
                # lwIP ERR_MEM retry loop, stalling the asyncio event loop.
                # When POLLOUT is not ready, pending is retained and retried
                # next cycle. Chunk size is also capped to _TCP_SEND_MAX to
                # reduce the chance of exhausting the lwIP pbuf pool in a
                # single call.
                writable = False
                try:
                    for _o, ev in live.poll(0):
                        if ev & select.POLLOUT:
                            writable = True
                except Exception:
                    pass
                if writable:
                    if pending:
                        try:
                            chunk = pending[:_TCP_SEND_MAX]
                            sent = cur.send(chunk)
                            pending = pending[sent:]
                        except OSError:
                            pending = b""
                    if not pending:
                        n = uart.any()
                        if n:
                            data = uart.read(min(n, _TCP_SEND_MAX))
                            if data:
                                try:
                                    sent = cur.send(data)
                                    if sent < len(data):
                                        pending = data[sent:]
                                except OSError:
                                    pass

                # Drain TCP -> UART (non-blocking: only if readable).
                try:
                    for _sock, ev in live.poll(0):
                        if ev & select.POLLIN:
                            rx = cur.recv(_TCP_RECV_MAX)
                            if rx:
                                uart.write(rx)
                except OSError:
                    pass

        except Exception as e:
            try:
                sys.print_exception(e)
            except Exception:
                pass

        await asyncio.sleep_ms(_UART_POLL_MS if cur is not None else _IDLE_POLL_MS)
