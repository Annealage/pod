# Read-only control listener: answers "who holds what" without taking the REPL.
#
# The REPL is the contended channel, so asking it who holds the REPL is a
# bootstrap knot: the caller that most needs the answer is exactly the one that
# cannot get in. This is a second listener that holds no slot and keeps no
# client, so the answer arrives while the REPL is occupied and while usbip has
# the pod busy.
#
# Line protocol, one request line in and one JSON line out, connection closed
# after. Deliberately tiny: it is a diagnostic surface, not a second control
# plane, and anything that mutates pod state belongs on the REPL where a human
# can see it.
#
#   who            -> {"ok": true, "holders": {...}, "counts": {...}}
#   who <resource> -> the same, filtered to one resource
#
# `counts` is the cumulative note/drop tally. It exists because a holder record
# that appears to come and go is otherwise indistinguishable from a reader that
# is racing, and the tally settles that without another round of guessing.
#   ping           -> {"ok": true, "pong": true}
#   anything else  -> {"ok": false, "err": "..."}
#
# Follows the uart_bridge pattern: bind() synchronously from netboot.main()
# before the first mDNS advertise so the advertise reflects the real bind state,
# then serve() as a task that never exits.

import json
import select
import socket
import sys

import asyncio

from . import holders

_POLL_DEAD = select.POLLHUP | select.POLLERR | 0x20
_IDLE_POLL_MS = 100
_MAX_REQUEST = 256

_server_sock = None


def bind(port):
    """Bind the dual-stack control listener; return the port, or None on failure.

    The caller gates the mDNS control-port TXT key on this return value, so a
    client never follows a dangling advertise.
    """
    global _server_sock
    try:
        s = socket.socket(socket.AF_INET6)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("::", port))
        s.listen(4)
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


def handle(line):
    """Map a request line to its response dict (pure; the tests drive this)."""
    parts = (line or "").strip().split()
    if not parts:
        return {"ok": False, "err": "empty request"}
    verb = parts[0].lower()
    if verb == "ping":
        return {"ok": True, "pong": True}
    if verb == "who":
        resource = parts[1] if len(parts) > 1 else None
        return {"ok": True, "holders": holders.who(resource),
                "counts": dict(holders._STATS)}
    return {"ok": False, "err": "unknown verb %r (want: who [resource], ping)"
            % verb}


async def serve(port):
    """Accept one request per connection, answer it, close.

    Never holds a client between requests, so it cannot itself become a resource
    someone has to wait for. Any exception inside a cycle is caught and the cycle
    continues: this task never exits, because a listener that silently vanished
    would be worse than one that occasionally errors.
    """
    s = _server_sock
    if s is None:
        print("control: no server socket - listener not started")
        return
    print("control: holder listener on port", port)

    while True:
        try:
            # accept() directly rather than gating on poll(): a listening socket
            # is not reliably reported readable here, and a connection that is
            # never accepted sits in the backlog forever, so a few of them wedge
            # the listener permanently. EAGAIN simply means nobody is waiting.
            cli = None
            try:
                cli, addr = s.accept()
            except OSError:
                cli = None
            if cli is not None:
                try:
                    cli.setblocking(False)
                    # One short read: the request is a single line and a client
                    # that sends nothing must not stall the loop.
                    req = b""
                    for _ in range(100):
                        try:
                            chunk = cli.recv(_MAX_REQUEST)
                        except OSError:
                            chunk = None
                        if chunk:
                            req += chunk
                            if b"\n" in req or len(req) >= _MAX_REQUEST:
                                break
                        await asyncio.sleep_ms(10)
                    if not req:
                        # Nothing arrived in the window. Saying "empty request"
                        # would be a wrong answer rather than no answer, and a
                        # caller cannot tell those apart.
                        body = {"ok": False, "err": "no request received"}
                    else:
                        body = handle(req.decode("utf-8", "replace"))
                    cli.send((json.dumps(body) + "\n").encode())
                except Exception as e:  # noqa: BLE001 - answer or drop, never die
                    try:
                        sys.print_exception(e)
                    except Exception:
                        pass
                finally:
                    if cli is not None:
                        try:
                            cli.close()
                        except Exception:
                            pass
        except Exception as e:  # noqa: BLE001
            try:
                sys.print_exception(e)
            except Exception:
                pass
        await asyncio.sleep_ms(_IDLE_POLL_MS)
