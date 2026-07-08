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


def recv_into(cl, mv, timeout_s=30, slice_ms=1):
    """Fill the memoryview `mv` from socket `cl`; return bytes read (0 = EOF).

    The accepted socket inherits the listener's non-blocking mode, so
    `readinto` returns None when no data has arrived yet. Treating None as EOF
    (the naive `if not r: break`) truncates a stream whenever the receiver
    outruns the sender - fatal for a receiver that reads immediately with no
    prior delay (write_mem_stream), and a latent race for flash_stream. Retry
    on None with a short sleep_ms yield: that keeps the single event loop
    servicing lwIP/cyw43 (unlike a blocking recv, which starves it and drops the
    connection on a long transfer), and does NOT misread a quiet gap as EOF. A
    genuinely closed peer returns 0, which ends the fill. `timeout_s` bounds a
    stalled-but-open peer so a dead sender cannot spin forever.
    """
    n = len(mv)
    got = 0
    deadline = time.ticks_add(time.ticks_ms(), int(timeout_s * 1000))
    while got < n:
        r = cl.readinto(mv[got:n])
        if r is None:                       # non-blocking: no data yet
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break                       # stalled peer; caller sees a short read
            time.sleep_ms(slice_ms)         # yield to lwIP/cyw43, avoid a tight spin
            continue
        if r == 0:                          # peer closed: EOF
            break
        got += r
        deadline = time.ticks_add(time.ticks_ms(), int(timeout_s * 1000))
    return got
