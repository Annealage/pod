"""Tests for the persistent streaming REPL session (pod.session), the
session_* MCP tools, and the `pod open` / `pod open-raw` CLI (streaming +
chained mount/exec/cp vs. raw-terminal passthrough), all against a fake
transport so no real ampremote/device is needed."""

import sys
import io
import threading
import time

import pytest

from pod.session import ReplSession


# ── fakes ─────────────────────────────────────────────────────────────────


class _FakeSerial:
    """pyserial-ish stream: read() drains a fed buffer (b'' once empty, after a
    short sleep to emulate the read timeout); write() records bytes."""

    def __init__(self):
        self._in = bytearray()
        self.written = bytearray()
        self._lock = threading.Lock()
        self.closed = False

    def feed(self, data):
        with self._lock:
            self._in += data

    def read(self, n):
        with self._lock:
            if self._in:
                chunk = bytes(self._in[:n])
                del self._in[:n]
                return chunk
        time.sleep(0.005)
        return b""

    def write(self, data):
        with self._lock:
            self.written += bytes(data)
        return len(data)

    def close(self):
        self.closed = True


class _OneShotSerial(_FakeSerial):
    """Delivers its fed bytes, then raises on the next read to simulate a
    dropped link (pyserial's socket read raises, vs b'' on an idle timeout)."""

    def read(self, n):
        with self._lock:
            if self._in:
                chunk = bytes(self._in[:n])
                del self._in[:n]
                return chunk
        raise OSError("simulated link drop")


class _FakeIntercept:
    """Mimics mpremote's SerialIntercept demux: a 0x18 byte + the next byte are
    an fs-RPC frame (consumed, never surfaced); other bytes are plain stdout
    exposed via inWaiting()/read(n)."""

    def __init__(self, orig):
        self.orig = orig
        self.buf = bytearray()

    def _pump(self):
        while True:
            c = self.orig.read(1)
            if not c:
                break
            if c == b"\x18":
                self.orig.read(1)        # drop the RPC command byte
            else:
                self.buf += c

    def inWaiting(self):
        self._pump()
        return len(self.buf)

    def read(self, n):
        if len(self.buf) < n:
            self._pump()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def write(self, data):
        return self.orig.write(data)

    def close(self):
        self.orig.close()


class _FakeTransport:
    def __init__(self, serial):
        self.serial = serial
        self.mounted = False
        self.in_raw_repl = False
        self.calls = []

    def enter_raw_repl(self, soft_reset=False):
        self.in_raw_repl = True
        self.calls.append(("enter_raw_repl", soft_reset))

    def exit_raw_repl(self):
        self.in_raw_repl = False
        self.calls.append(("exit_raw_repl",))

    def mount_local(self, path, unsafe_links=False):
        self.calls.append(("mount_local", path, unsafe_links))
        self._orig = self.serial
        self.serial = _FakeIntercept(self.serial)
        self.mounted = True

    def umount_local(self):
        self.calls.append(("umount_local",))
        self.serial = getattr(self, "_orig", self.serial)
        self.mounted = False

    def close(self):
        self.serial.close()


def _factory(serial):
    return lambda target, timeout: _FakeTransport(serial)


def _seq_factory(transports):
    """A transport_factory returning the given transports in order, one per
    (re)connect - so a drop can hand the reader a fresh transport."""
    it = iter(transports)
    return lambda target, timeout: next(it)


def _wait_until(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


class _FakeSession:
    """Stand-in for an open ReplSession, for the MCP / CLI delegation tests."""

    def __init__(self, target="socket://x:8266"):
        self.target = target
        self.running = True
        self.connected = True
        self.mounted = False
        self._cursor = 0
        self.sent = []
        self.interrupted = False
        self.closed = False

    def tell(self):
        return self._cursor

    def send(self, data, newline=True):
        self.sent.append((data, newline))
        self._cursor += len(data)
        return len(data)

    def read_since(self, since=None):
        return {"text": "out", "cursor": self._cursor, "dropped": 0}

    def interrupt(self):
        self.interrupted = True
        return 1

    def close(self):
        self.closed = True
        self.running = False
        return {"ok": True, "received": self._cursor, "error": None}


# ── ReplSession ─────────────────────────────────────────────────────────────


@pytest.fixture
def fake_serial():
    return _FakeSerial()


@pytest.fixture
def sess(fake_serial, tmp_path):
    chunks = []
    s = ReplSession("socket://x:8266", log_path=str(tmp_path / "log.txt"),
                    on_output=chunks.append, read_timeout=0.05,
                    transport_factory=_factory(fake_serial))
    s.open()
    s._chunks = chunks
    yield s
    s.close()


class TestReplSession:
    def test_streams_to_buffer_file_and_callback(self, sess, fake_serial):
        fake_serial.feed(b"hello\r\n")
        assert _wait_until(lambda: sess.tell() >= 7)
        out = sess.read_since(None)
        assert out["text"] == "hello\r\n"
        assert out["dropped"] == 0
        assert b"".join(sess._chunks) == b"hello\r\n"
        assert sess.read_since(out["cursor"])["text"] == ""

    def test_log_file_written(self, fake_serial, tmp_path):
        log = tmp_path / "l.txt"
        s = ReplSession("socket://x", log_path=str(log), read_timeout=0.05,
                        transport_factory=_factory(fake_serial))
        s.open()
        try:
            fake_serial.feed(b"abc")
            assert _wait_until(lambda: s.tell() >= 3)
        finally:
            s.close()
        assert log.read_bytes() == b"abc"

    def test_send_appends_newline(self, sess, fake_serial):
        sess.send("print(1)")
        assert fake_serial.written == b"print(1)\r\n"

    def test_send_raw_no_newline(self, sess, fake_serial):
        sess.send(b"\x01", newline=False)
        assert fake_serial.written == b"\x01"

    def test_interrupt_sends_ctrl_c(self, sess, fake_serial):
        sess.interrupt()
        assert fake_serial.written == b"\x03"

    def test_read_since_reports_dropped(self, fake_serial):
        s = ReplSession("socket://x", buffer_bytes=8, read_timeout=0.05,
                        transport_factory=_factory(fake_serial))
        s.open()
        try:
            fake_serial.feed(b"0123456789ABCDEF")   # 16 bytes, ring cap 8
            assert _wait_until(lambda: s.tell() >= 16)
            out = s.read_since(0)
            assert out["dropped"] == 8
            assert out["text"] == "89ABCDEF"
            assert out["cursor"] == 16
        finally:
            s.close()

    def test_close_stops_and_closes(self, fake_serial):
        s = ReplSession("socket://x", read_timeout=0.05,
                        transport_factory=_factory(fake_serial))
        s.open()
        assert s.running
        res = s.close()
        assert res["ok"] is True
        assert fake_serial.closed is True
        assert not s.running

    def test_auto_reconnect_after_drop(self, tmp_path):
        s1 = _OneShotSerial()
        s1.feed(b"first\r\n")
        s2 = _FakeSerial()
        t1, t2 = _FakeTransport(s1), _FakeTransport(s2)
        s = ReplSession("socket://x:8266", read_timeout=0.02,
                        reconnect_min=0.01, reconnect_max=0.05,
                        log_path=str(tmp_path / "l.txt"),
                        transport_factory=_seq_factory([t1, t2]))
        s.open()
        try:
            assert _wait_until(lambda: s.reconnects >= 1, timeout=3)
            assert s1.closed is True                  # dead transport torn down
            s2.feed(b"second\r\n")                     # streams from the new link
            assert _wait_until(
                lambda: "second" in s.read_since(0)["text"], timeout=3)
            text = s.read_since(0)["text"]
            assert "first" in text                    # pre-drop output retained
            assert "reconnect" in text.lower()        # boundary marked inline
        finally:
            s.close()

    def test_no_reconnect_when_disabled(self):
        s1 = _OneShotSerial()
        s1.feed(b"bye\r\n")
        s = ReplSession("socket://x", read_timeout=0.02, reconnect=False,
                        transport_factory=_seq_factory([_FakeTransport(s1)]))
        s.open()
        try:
            assert _wait_until(lambda: not s.running, timeout=3)  # reader exits
            assert s.connected is False
        finally:
            s.close()

    def test_mounted_reconnect_reapplies_mount(self):
        # A mounted link that drops on the first read; the reader must detect the
        # drop (the intercept's underlying read raises) and re-apply the mount on
        # the fresh transport.
        s1, s2 = _OneShotSerial(), _FakeSerial()
        t1, t2 = _FakeTransport(s1), _FakeTransport(s2)
        s = ReplSession("socket://x", mount="/host/dir", read_timeout=0.02,
                        reconnect_min=0.01, reconnect_max=0.05,
                        transport_factory=_seq_factory([t1, t2]))
        s.open()
        try:
            assert _wait_until(lambda: s.reconnects >= 1, timeout=3)
            assert ("mount_local", "/host/dir", False) in t2.calls
            assert s.mounted is True
        finally:
            s.close()

    def test_send_during_reconnect_raises(self):
        s1 = _OneShotSerial()                 # drops immediately

        def factory(target, timeout):
            if not factory.calls:
                factory.calls.append(1)
                return _FakeTransport(s1)
            raise OSError("link still down")  # reconnect keeps failing
        factory.calls = []

        s = ReplSession("socket://x", read_timeout=0.02, reconnect_min=0.01,
                        reconnect_max=0.02, transport_factory=factory)
        s.open()
        s._send_wait = 0.05                   # don't wait the full 5s in the test
        try:
            assert _wait_until(lambda: not s.connected, timeout=2)
            with pytest.raises(ConnectionError):
                s.send("print(1)")
        finally:
            s.close()

    def test_mount_sequence_and_demux(self, fake_serial, tmp_path):
        s = ReplSession("socket://x", mount="/host/dir", read_timeout=0.02,
                        log_path=str(tmp_path / "l.txt"),
                        transport_factory=_factory(fake_serial))
        s.open()
        t = s._transport
        try:
            # enter raw repl -> mount_local -> exit raw repl, in that order
            assert [c[0] for c in t.calls] == [
                "enter_raw_repl", "mount_local", "exit_raw_repl"]
            assert ("mount_local", "/host/dir", False) in t.calls
            assert t.mounted is True
            # a stream mixing stdout with a 0x18-framed fs-RPC frame: RPC dropped
            fake_serial.feed(b"AB\x18\x07CD")
            assert _wait_until(lambda: s.tell() >= 4)
            assert s.read_since(None)["text"] == "ABCD"
        finally:
            s.close()
        # close() unmounts when mounted
        assert ("umount_local",) in t.calls


# ── MCP repl_* tools ──────────────────────────────────────────────────────


@pytest.fixture
def mcp_repl(monkeypatch):
    import pod.mcp_server as m
    monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
    fake = _FakeSession()
    opened = {}

    class FakePod:
        def open_session(self, **kwargs):
            opened.update(kwargs)
            fake.mounted = bool(kwargs.get("mount"))
            return fake

    monkeypatch.setattr(m.Pod, "from_entry", classmethod(lambda cls, e: FakePod()))
    m._SESSIONS.clear()
    yield m, fake, opened
    m._SESSIONS.clear()


class TestMcpRepl:
    def test_open_send_read_list_close(self, mcp_repl):
        m, fake, opened = mcp_repl
        info = m._open_session("lab")
        assert info["running"] is True
        assert info["target"] == fake.target
        assert info["log_path"]                       # defaulted to a temp file

        r = m._session_write("lab:pod", "print(1)", wait=0)
        assert fake.sent == [("print(1)", True)]
        assert r["sent"] == len("print(1)")

        assert m.handle_session_read("lab:pod")["text"] == "out"
        assert m._open_sessions("lab")[0]["label"] == "lab"

        res = m.handle_session_close("lab:pod")
        assert res["ok"] is True and fake.closed is True
        assert m._open_sessions("lab") == []

    def test_open_builds_chain(self, mcp_repl):
        m, _, opened = mcp_repl
        info = m._open_session("lab", mount="/d", exec="import x",
                                  cp=["a.py", ":a.py"], soft_reset=True)
        assert opened["mount"] == "/d"
        assert opened["pre_exec"] == ["import x"]
        assert opened["pre_cp"] == [("a.py", ":a.py")]
        assert opened["soft_reset"] is True
        assert info["mounted"] is True

    def test_open_chain_list_forms(self, mcp_repl):
        m, _, opened = mcp_repl
        m._open_session("lab", exec=["a", "b"], cp=[["x", ":x"], ["y", ":y"]])
        assert opened["pre_exec"] == ["a", "b"]
        assert opened["pre_cp"] == [("x", ":x"), ("y", ":y")]

    def test_open_is_idempotent(self, mcp_repl):
        m, _, _ = mcp_repl
        m._open_session("lab")
        assert m._open_session("lab").get("already_open") is True

    def test_read_without_open_raises(self, mcp_repl):
        m, _, _ = mcp_repl
        with pytest.raises(KeyError):
            m.handle_session_read("nope")

    def test_interrupt(self, mcp_repl):
        m, fake, _ = mcp_repl
        m._open_session("lab")
        m._session_interrupt("lab:pod", wait=0)
        assert fake.interrupted is True

    def test_close_without_open_is_ok(self, mcp_repl):
        m, _, _ = mcp_repl
        assert m.handle_session_close("nope")["ok"] is True

    def test_session_send_without_control_writes_data(self, mcp_repl):
        """session_send with no `control` writes `data` to the session's stdin."""
        m, fake, _ = mcp_repl
        m._open_session("lab")
        r = m.handle_session_send("lab:pod", data="print(1)", wait=0)
        assert fake.sent == [("print(1)", True)]
        assert fake.interrupted is False
        assert r["sent"] == len("print(1)")

    def test_session_send_control_c_interrupts(self, mcp_repl):
        """control='c' interrupts, returning _session_interrupt's result (no `sent` key)."""
        m, fake, _ = mcp_repl
        m._open_session("lab")
        r = m.handle_session_send("lab:pod", control="c", wait=0)
        assert fake.interrupted is True
        assert fake.sent == []
        assert "sent" not in r

    def test_session_send_control_b_sends_ctrl_b(self, mcp_repl):
        """control='b' writes a raw Ctrl-B (0x02) with no trailing newline."""
        m, fake, _ = mcp_repl
        m._open_session("lab")
        m.handle_session_send("lab:pod", control="b", wait=0)
        assert fake.sent == [("\x02", False)]

    def test_session_send_control_d_sends_ctrl_d(self, mcp_repl):
        """control='d' writes a raw Ctrl-D (0x04) with no trailing newline."""
        m, fake, _ = mcp_repl
        m._open_session("lab")
        m.handle_session_send("lab:pod", control="d", wait=0)
        assert fake.sent == [("\x04", False)]

    def test_session_send_requires_data_when_no_control(self, mcp_repl):
        m, _, _ = mcp_repl
        m._open_session("lab")
        with pytest.raises(ValueError):
            m.handle_session_send("lab:pod")

    def test_session_send_rejects_unknown_control(self, mcp_repl):
        m, _, _ = mcp_repl
        m._open_session("lab")
        with pytest.raises(ValueError):
            m.handle_session_send("lab:pod", control="x")

    def test_pod_open_targets_pod_socket_repl(self, mcp_repl):
        """pod_open omits device, targeting the pod's own socket REPL."""
        m, _, opened = mcp_repl
        m.handle_pod_open("lab")
        assert opened["device"] is None

    def test_dut_open_targets_given_device(self, mcp_repl):
        """dut_open passes device through, targeting the DUT's CDC tty."""
        m, _, opened = mcp_repl
        m.handle_dut_open("lab", "/dev/ttyACM0")
        assert opened["device"] == "/dev/ttyACM0"


class TestMcpPodInfo:
    """pod_info returns the registry entry for a label plus every session
    open in this process, which is the only view of open sessions the
    surface offers."""

    def test_merges_registry_entry_with_open_sessions(self, mcp_repl):
        m, _, _ = mcp_repl
        m._open_session("lab")
        info = m.handle_pod_info("lab")
        assert info["label"] == "lab"
        assert info["addr4"] == "10.0.0.1"
        assert info["sessions"] == m._open_sessions("lab")
        assert info["sessions"][0]["label"] == "lab"

    def test_no_open_sessions_gives_empty_list(self, mcp_repl):
        m, _, _ = mcp_repl
        info = m.handle_pod_info("lab")
        assert info["sessions"] == []

    def test_unknown_label_raises(self, mcp_repl, monkeypatch):
        m, _, _ = mcp_repl
        monkeypatch.setattr(m, "get_pod", lambda label: None)
        with pytest.raises(KeyError):
            m.handle_pod_info("nope")


# ── `pod open` CLI ─────────────────────────────────────────────────────────


def _repl_args(**over):
    base = {"label": "lab", "raw": False, "log": None, "device": None,
            "mount": None, "exec": None, "cp": None, "soft_reset": False,
            "unsafe_links": False, "no_reconnect": False}
    base.update(over)
    return type("A", (), base)()


class TestCliPodOpen:
    def _patch(self, monkeypatch, fakepod):
        import pod.cli as c
        monkeypatch.setattr(c, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        monkeypatch.setattr(c.Pod, "from_entry",
                            classmethod(lambda cls, e: fakepod))
        return c

    def test_stream_sends_stdin_then_eof_closes(self, monkeypatch):
        fake = _FakeSession()
        opened = {}

        class FakePod:
            def open_session(self, **kwargs):
                opened.update(kwargs)
                return fake

        c = self._patch(monkeypatch, FakePod())
        monkeypatch.setattr("sys.stdin", io.StringIO("print(1)\nprint(2)\n"))
        assert c.cmd_repl(_repl_args()) == 0
        assert fake.sent == [("print(1)", True), ("print(2)", True)]
        assert fake.closed is True

    def test_stream_builds_chain(self, monkeypatch):
        opened = {}

        class FakePod:
            def open_session(self, **kwargs):
                opened.update(kwargs)
                return _FakeSession()

        c = self._patch(monkeypatch, FakePod())
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        c.cmd_repl(_repl_args(mount="/d", exec=["import x"],
                              cp=[["a", ":a"]], soft_reset=True))
        assert opened["mount"] == "/d"
        assert opened["pre_exec"] == ["import x"]
        assert opened["pre_cp"] == [("a", ":a")]
        assert opened["soft_reset"] is True

    def test_raw_dispatches_to_passthrough(self, monkeypatch):
        called = {}

        class FakePod:
            def repl(self):
                called["repl"] = True

            def open_session(self, **kwargs):
                called["open_session"] = True
                return _FakeSession()

        c = self._patch(monkeypatch, FakePod())
        assert c.cmd_repl_raw(_repl_args()) == 0
        assert called.get("repl") is True
        assert "open_session" not in called

    def test_raw_rejects_chain_flags_at_the_cli(self, monkeypatch):
        """`pod open-raw` has no --mount/--exec/--cp/--soft-reset options; a
        chained setup flag is an argparse error, not an application-level
        one, since raw passthrough cannot run pre-connect setup."""
        import pod.cli as c
        monkeypatch.setattr(sys, "argv",
                            ["pod", "open-raw", "lab", "--mount", "/d"])
        with pytest.raises(SystemExit):
            c.main()
