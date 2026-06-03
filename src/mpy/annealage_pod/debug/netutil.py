# Interruptible, bounded socket accept for pod servers driven over the REPL.
#
# A blocking lwIP accept()/recv() cannot be broken by Ctrl-C, so a host that
# never connects (wrong IP, Wi-Fi drop, killed client) wedges the REPL until a
# power-cycle - this cost real debugging time on the logic-analyser stream. A
# non-blocking socket polled in short slices returns to the Python bytecode
# loop every slice, where MicroPython raises any pending KeyboardInterrupt, so
# Ctrl-C breaks in promptly; a deadline bounds the wait independently.
#
# Use this in place of `srv.settimeout(t); cl, _ = srv.accept()` for every pod
# TCP server that blocks the REPL (flash_stream / dump_stream / la_stream, and
# anything reusing this pattern).

import select
import time


class AcceptTimeout(OSError):
    pass


def accept(srv, timeout_s=30, slice_ms=100):
    """Accept one connection on `srv`, interruptibly and within `timeout_s`.

    Returns the accepted (client_socket, addr). Raises AcceptTimeout (an OSError
    with errno ETIMEDOUT) if no client connects in time. Ctrl-C raised during
    the wait propagates out at the next slice boundary (<= slice_ms).
    """
    srv.setblocking(False)
    poller = select.poll()
    poller.register(srv, select.POLLIN)
    try:
        deadline = time.ticks_add(time.ticks_ms(), int(timeout_s * 1000))
        while True:
            if poller.poll(slice_ms):
                return srv.accept()
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise AcceptTimeout(110)   # ETIMEDOUT
    finally:
        try:
            poller.unregister(srv)
        except Exception:
            pass
