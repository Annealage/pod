"""Persistent, auto-reconnecting streaming REPL session (built on ampremote).

A long-lived connection that streams the target's stdout to a log file and an
in-memory ring buffer (so a console or an agent can tail it) while letting the
caller inject commands into the target's stdin asynchronously - the friendly /
aiorepl passthrough, i.e. "type a line, it runs on the REPL". It is stateful and
survives transient drops: like ampremote, the reader distinguishes a dropped
link (the transport read raises) from an idle gap (a read timeout returns no
bytes) and transparently reconnects with backoff, re-applying any mount, so
long-running logging continues across Wi-Fi blips and target reboots. Reconnect
boundaries are marked inline in the stream/log.

Distinct from Pod.exec, which is a one-shot raw-REPL round-trip: a held session
occupies the pod's single socket-REPL slot for its lifetime, so while it is open
the session IS the way to run commands (send + read), not a separate exec.

The transport is mpremote's SerialTransport over a pyserial socket:// stream
(`ampremote connect socket://HOST:PORT` speaks the same protocol). The session
does NOT enter the raw REPL - it is a byte passthrough: whatever the device
prints is teed out, and whatever is sent is written to its stdin verbatim (with
an optional trailing newline so a bare command line is submitted to the REPL).

The reader runs on a background thread. Output fans out to, in order:
  1. the log file (append, unbuffered) - the complete record, never truncated;
  2. an optional on_output(bytes) callback - e.g. the CLI console echo;
  3. an in-memory ring buffer (capped) that read_since() tails by cursor.
The ring buffer can drop the oldest bytes under sustained output; the log file
is the lossless copy, and read_since reports how many bytes it skipped.
"""

import threading
import time

DEFAULT_BUFFER_BYTES = 256 * 1024     # ring-buffer cap for read_since tailing
DEFAULT_READ_TIMEOUT = 0.2            # serial read timeout; bounds stop latency
DEFAULT_RECONNECT_MIN = 0.5           # backoff floor between reconnect attempts
DEFAULT_RECONNECT_MAX = 5.0           # backoff ceiling
DEFAULT_SEND_WAIT = 5.0               # how long send() waits out a reconnect
DEFAULT_REPL_PORT = 8266


class ReplSession:
    """A persistent, auto-reconnecting streaming connection to one target's REPL.

    Args:
        target: an mpremote device string - the pod's "socket://HOST:PORT" REPL
            (the default via Pod.open_session) or any device (e.g. a DUT
            "/dev/ttyACM0" CDC tty).
        log_path: file to append all received bytes to (the lossless record).
            None disables file logging.
        on_output: optional callback(bytes) invoked for each received chunk on
            the reader thread (the CLI uses it to echo to the console).
        buffer_bytes: ring-buffer cap for read_since().
        read_timeout: serial read timeout in seconds; also bounds how quickly
            close() stops the reader.
        reconnect: auto-reconnect after a dropped link (default True). When
            False, a drop ends the session (running becomes False).
        reconnect_min/reconnect_max: reconnect backoff bounds (seconds).
        mount: a host directory to mount on the target for the session's
            lifetime (mpremote `mount`). The fs hook is installed over THIS
            connection (it RPCs back over it), so it must live on the session
            rather than a throwaway process, and is re-applied on every
            reconnect. Installing it briefly enters the raw REPL (like
            `mpremote mount`), which interrupts a running app's foreground.
        unsafe_links: passed to mount_local (follow symlinks out of the root).
        transport_factory: test seam - callable(target, timeout) -> a transport
            exposing `.serial` (read/write/close), and, when `mount` is used,
            `mounted`/`in_raw_repl`/`enter_raw_repl`/`exit_raw_repl`/
            `mount_local`/`umount_local`. Called once per (re)connect. Defaults
            to mpremote's SerialTransport, imported lazily so the module loads
            without it.
    """

    def __init__(self, target, *, log_path=None, on_output=None,
                 buffer_bytes=DEFAULT_BUFFER_BYTES,
                 read_timeout=DEFAULT_READ_TIMEOUT, reconnect=True,
                 reconnect_min=DEFAULT_RECONNECT_MIN,
                 reconnect_max=DEFAULT_RECONNECT_MAX, mount=None,
                 unsafe_links=False, transport_factory=None):
        self.target = target
        self.log_path = log_path
        self._on_output = on_output
        self._buffer_bytes = buffer_bytes
        # A None timeout would make the reader block forever and never observe
        # _stop; force a bounded poll.
        self._read_timeout = (read_timeout if read_timeout is not None
                              else DEFAULT_READ_TIMEOUT)
        self._reconnect = reconnect
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._send_wait = DEFAULT_SEND_WAIT
        self._mount = mount
        self._unsafe_links = unsafe_links
        self._transport_factory = transport_factory

        self._buf = bytearray()
        self._total = 0                 # total bytes ever received (cursor space)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._thread = None
        self._logf = None
        self._transport = None
        self.error = None               # repr of the last drop / reconnect error
        self.reconnects = 0             # successful reconnects since open()

    # ── connection lifecycle ─────────────────────────────────────────────

    def open(self):
        """Open the transport, start the reader thread. Returns self.

        The INITIAL connect is synchronous and propagates the transport's error
        (mpremote's TransportError) so a dead target surfaces immediately, and
        so does an initial mount failure. Later drops are handled by the
        reader's reconnect loop, not raised.
        """
        self._connect()                 # initial connect; raises on failure
        try:
            self._ensure_mounted(strict=True)   # initial mount; raises on failure
        except Exception:
            self._teardown_transport()
            raise
        if self.log_path:
            # Unbuffered append: the log is the lossless record even if the
            # process is killed; nothing about the stream is held only in RAM.
            self._logf = open(self.log_path, "ab", buffering=0)
        self._connected.set()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._reader, name="repl-session", daemon=True)
        self._thread.start()
        return self

    def _make_transport(self):
        if self._transport_factory is not None:
            return self._transport_factory(self.target, self._read_timeout)
        # The ampremote/mpremote distribution installs as the `mpremote` import
        # package (see pyproject); SerialTransport over a pyserial socket:// URL.
        from mpremote.transport_serial import SerialTransport
        return SerialTransport(self.target, timeout=self._read_timeout)

    def _connect(self):
        """Open the bare transport (the socket only). No raw-REPL entry, so a
        flapping link does not interrupt the target's app on every attempt;
        mounting (which must enter the raw REPL) is a separate, once-per-live-
        connection step in _ensure_mounted. Raises on socket-open failure."""
        self._transport = self._make_transport()

    def _ensure_mounted(self, strict):
        """Apply the mount on the current (live) transport, at most once per
        connection. The mount fs hook RPCs back over this link so it must live
        on the session; installing it enters the raw REPL, which interrupts the
        target's foreground (so this runs once per restored link, not per
        reconnect attempt). strict=True (initial open) re-raises a failure;
        strict=False (reconnect) notes it and continues streaming UNMOUNTED so a
        persistently-failing mount cannot storm the app with Ctrl-C.
        """
        if not self._mount or getattr(self._transport, "mounted", False):
            return
        try:
            self._apply_mount()
        except Exception as exc:        # noqa: BLE001 - degrade, don't storm
            if strict:
                raise
            self._note("mount failed (%r); continuing unmounted" % exc)

    def _apply_mount(self):
        """Install the mount fs hook over this connection, then return to the
        friendly REPL so the passthrough streams the app's stdout. The
        SerialIntercept that mount_local leaves in place services the device's
        filesystem RPC inline under the friendly REPL (see _reader). Done before
        the reader thread reads this transport, so there is only ever one reader
        of the stream.
        """
        t = self._transport
        entered = not getattr(t, "in_raw_repl", False)
        if entered:
            t.enter_raw_repl(soft_reset=False)
        try:
            t.mount_local(self._mount, unsafe_links=self._unsafe_links)
        finally:
            if entered:                 # only undo the state we changed
                t.exit_raw_repl()

    def _teardown_transport(self):
        """Close and drop the current transport (used for a dead/dropped link)."""
        t = self._transport
        self._transport = None
        if t is not None:
            try:
                t.close()
            except Exception:           # noqa: BLE001 - closing a dead socket
                pass

    @property
    def serial(self):
        """The underlying pyserial stream (read/write/close), or None if down."""
        return self._transport.serial if self._transport is not None else None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def connected(self):
        """True while a live transport is attached (False mid-reconnect)."""
        return self._connected.is_set()

    @property
    def mounted(self):
        """Whether a mount is currently active on the live transport (the real
        state, which may be False if a reconnect could not re-apply it)."""
        return bool(getattr(self._transport, "mounted", False))

    def close(self):
        """Stop the reader, unmount, close the transport and log. Leaves the
        target running. Returns {ok, received, reconnects, error}. Idempotent.
        """
        # Was the link live at close time? (vs mid-reconnect). Captured before
        # _stop so the reader's own _connected.clear() on exit can't confuse it.
        alive = self._connected.is_set()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._read_timeout + 2)
            if self._thread.is_alive():
                # Reader stuck in a blocking read (e.g. SerialIntercept's own
                # timeout); force the socket closed to abort it, then reap. This
                # forfeits a clean unmount, so skip it below.
                alive = False
                if self._transport is not None:
                    try:
                        self._transport.serial.close()
                    except Exception:   # noqa: BLE001
                        pass
                self._thread.join(timeout=2)
        if self._transport is not None:
            # Best-effort unmount so the device's /remote does not linger
            # RPC-dead (a stale mount would EEXIST the next mount). Only when the
            # link was live at close - over a dropped/dead link the unmount RPC
            # would just block out to the transport timeout before failing.
            if alive and getattr(self._transport, "mounted", False):
                try:
                    if not getattr(self._transport, "in_raw_repl", False):
                        self._transport.enter_raw_repl(soft_reset=False)
                    self._transport.umount_local()
                    self._transport.exit_raw_repl()
                except Exception:       # noqa: BLE001 - stale mount is tolerable
                    pass
            try:
                self._transport.close()
            except Exception:           # noqa: BLE001 - closing a dead socket
                pass
            self._transport = None
        self._connected.clear()
        if self._logf is not None:
            try:
                self._logf.close()
            except Exception:           # noqa: BLE001
                pass
            self._logf = None
        return {"ok": self.error is None, "received": self._total,
                "reconnects": self.reconnects, "error": self.error}

    # ── reader thread (with reconnect) ────────────────────────────────────

    def _reader(self):
        try:
            while not self._stop.is_set():
                if self._transport is None:
                    # Need a (re)connection. open() supplies the first one.
                    if not self._reconnect or not self._reconnect_loop():
                        break           # reconnect disabled, or stopped/gave up
                if not self._read_until_drop():
                    break               # clean stop (transport left intact)
                # A drop: tear the dead transport down and (maybe) reconnect.
                self._connected.clear()
                self._teardown_transport()
                if self._stop.is_set() or not self._reconnect:
                    break
                self._note("connection dropped, reconnecting to %s ..."
                           % self.target)
        finally:
            self._connected.clear()

    def _read_until_drop(self):
        """Read the current transport until stop or a dropped link.

        Returns False on a clean stop (leaving the transport intact for close()
        to unmount), True on a drop (the caller tears it down / reconnects). A
        mounted transport's serial is a SerialIntercept whose read(n) blocks for
        n PLAIN bytes (servicing 0x18-framed fs-RPC inline), so a blind
        read(4096) would stall; mirror mpremote's repl loop instead -
        inWaiting() (non-blocking) then read(only what's ready) - so fs-RPC is
        handled invisibly and only stdout is teed.
        """
        mounted = getattr(self._transport, "mounted", False)
        ser = self._transport.serial
        while not self._stop.is_set():
            try:
                if mounted:
                    n = ser.inWaiting()
                    if not n:
                        time.sleep(self._read_timeout)
                        continue
                    data = ser.read(n)
                else:
                    data = ser.read(4096)
            except Exception as exc:    # noqa: BLE001 - dropped link
                self.error = repr(exc)
                return True
            if data:
                self._absorb(data)
        return False

    def _reconnect_loop(self):
        """Reopen the transport with exponential backoff. Loops until it
        reconnects (returns True) or close()/_stop ends it (returns False);
        there is no attempt cap - a session reconnects for as long as it is
        held open. Re-applies the mount once on the restored link (best-effort:
        a failed re-mount continues unmounted rather than storming the app).
        """
        delay = self._reconnect_min
        while not self._stop.is_set():
            try:
                self._connect()
            except Exception as exc:    # noqa: BLE001 - target still down
                self.error = repr(exc)
                if self._stop.wait(delay):   # interruptible backoff
                    return False
                delay = min(delay * 2, self._reconnect_max)
                continue
            self._connected.set()
            self.reconnects += 1
            self._ensure_mounted(strict=False)
            self._note("reconnected to %s" % self.target)
            return True
        return False

    def _note(self, msg):
        """Inject a session marker into the stream (log + buffer + callback)."""
        self._absorb(("\n[pod-repl: %s]\n" % msg).encode("utf-8", "replace"))

    def _absorb(self, data):
        if self._logf is not None:
            try:
                self._logf.write(data)
            except Exception:           # noqa: BLE001 - logging must not kill it
                pass
        if self._on_output is not None:
            try:
                self._on_output(data)
            except Exception:           # noqa: BLE001 - a bad sink must not kill it
                pass
        with self._lock:
            self._total += len(data)
            self._buf += data
            overflow = len(self._buf) - self._buffer_bytes
            if overflow > 0:
                del self._buf[:overflow]

    # ── tail / inject ────────────────────────────────────────────────────

    def tell(self):
        """Current cursor (total bytes received) - a cheap marker for read_since."""
        with self._lock:
            return self._total

    def read_since(self, cursor=None):
        """Tail buffered output after `cursor`.

        cursor is a byte offset in the total received stream (the value returned
        by a prior call). None (or a cursor newer than what's buffered) starts
        from the oldest buffered byte. Returns {text, cursor, dropped}: `cursor`
        is the new offset to pass next time; `dropped` is how many bytes were
        evicted from the ring buffer before `cursor` could be served (they are
        still in the log file). Always available, even mid-reconnect.
        """
        with self._lock:
            total = self._total
            buf_start = total - len(self._buf)
            if cursor is None or cursor > total:
                cursor = buf_start
            dropped = 0
            if cursor < buf_start:
                dropped = buf_start - cursor
                cursor = buf_start
            data = bytes(self._buf[cursor - buf_start:])
        return {"text": data.decode("utf-8", "replace"), "cursor": total,
                "dropped": dropped}

    def send(self, data, newline=True):
        """Write to the target's stdin. str is utf-8 encoded.

        newline=True appends CR-LF unless `data` already ends in one, so a bare
        command line is submitted to the REPL. If the link is mid-reconnect this
        waits briefly for it to come back; raises ConnectionError if it does not
        (or the write fails). Returns the byte count written.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        if newline and not data.endswith((b"\r", b"\n")):
            data += b"\r\n"
        if not self._connected.is_set():
            self._connected.wait(self._send_wait)
        transport = self._transport
        if not self._connected.is_set() or transport is None:
            raise ConnectionError("REPL session not connected (reconnecting)")
        try:
            return transport.serial.write(data)
        except Exception as exc:        # noqa: BLE001 - surface as a connect error
            raise ConnectionError("write failed: %r" % exc)

    def interrupt(self):
        """Send Ctrl-C (0x03) to interrupt a running REPL command / loop."""
        return self.send(b"\x03", newline=False)
