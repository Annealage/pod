"""DUT UART streaming, over a pod's TCP bridge or a host serial device."""

import select
import socket
import sys
import time

from pod import locks


class _SerialConn:
    """A pyserial port with the recv/sendall/close surface pump() expects."""

    def __init__(self, port):
        self._port = port

    def recv(self, n):
        return self._port.read(n)

    def sendall(self, data):
        self._port.write(data)

    def close(self):
        self._port.close()


def pump(conn, duration=None, on_output=None, interactive=False, out_path=None,
         eof_on_empty=True):
    """Copy DUT output from `conn` to on_output / a file / stdout until the peer
    closes, `duration` elapses or the caller interrupts. With `interactive`,
    host stdin is forwarded to the DUT. `eof_on_empty` treats an empty read as
    the peer closing (a socket) rather than an idle timeout (a serial port).
    `conn` needs recv (returning b"" or
    raising socket.timeout when idle), sendall and close; it is closed on exit.
    Returns {ok, bytes_received}.
    """
    out_file = open(out_path, "wb") if out_path is not None else None  # noqa: SIM115
    bytes_received = 0
    t0 = time.monotonic()
    try:
        while True:
            try:
                chunk = conn.recv(4096)
                if chunk == b"" and eof_on_empty:
                    break  # a socket's empty read is the peer closing
            except socket.timeout:
                chunk = b""
            if chunk:
                bytes_received += len(chunk)
                if on_output is not None:
                    on_output(chunk)
                elif out_file is not None:
                    out_file.write(chunk)
                else:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            if interactive:
                r, _, _ = select.select([sys.stdin.buffer], [], [], 0)
                if r:
                    data = sys.stdin.buffer.read1(4096)
                    if data:
                        conn.sendall(data)
            if duration is not None and (time.monotonic() - t0) >= duration:
                break
    finally:
        conn.close()
        if out_file is not None:
            out_file.close()
    return {"ok": True, "bytes_received": bytes_received}


def stream_tty(tty, baud=115200, caller=None, force=False, **kwargs):
    """Stream a host serial device, holding its host lock for the duration."""
    import serial

    if caller is None:
        from pod.client import resolve_caller
        caller = resolve_caller()
    with locks.hold("tty-" + tty, caller, force):
        port = serial.Serial(tty, baud, timeout=0.1)
        return pump(_SerialConn(port), eof_on_empty=False, **kwargs)
