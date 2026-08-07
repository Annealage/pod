# Unit tests for annealage_pod.debug.netutil send_all / recv_into: the
# EAGAIN-retry loops that keep the bulk pod streams (dump_stream, flash_stream,
# write_mem_stream) robust to non-blocking-socket backpressure. The accepted
# stream socket is non-blocking, so send()/readinto() signal a full/empty buffer
# rather than blocking; both helpers must retry (yielding via sleep_ms) instead
# of aborting, and bound a stalled peer with a timeout.
#
# CPython has no time.ticks_*/sleep_ms, so a fake clock shims them - which also
# lets the timeout path run deterministically with no real sleeping (sleep_ms
# just advances the fake clock).

import pytest


@pytest.fixture
def netutil(monkeypatch):
    import time

    clock = {"t": 0}
    monkeypatch.setattr(time, "ticks_ms", lambda: clock["t"], raising=False)
    monkeypatch.setattr(time, "ticks_add", lambda a, b: a + b, raising=False)
    monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)

    def _sleep_ms(ms):
        clock["t"] += ms

    monkeypatch.setattr(time, "sleep_ms", _sleep_ms, raising=False)
    import annealage_pod.debug.netutil as nu

    return nu


class _SendSock:
    # send() plays a script of outcomes: "eagain" -> raise OSError(EAGAIN),
    # None/0 -> backpressure (no bytes accepted), int -> accept up to N bytes.
    # Outcomes past the script accept everything remaining.
    def __init__(self, script=()):
        self.script = list(script)
        self.out = bytearray()

    def send(self, mv):
        data = bytes(mv)
        if self.script:
            op = self.script.pop(0)
            if op == "eagain":
                raise OSError(11)
            if op is None or op == 0:
                return op
            data = data[:op]
        self.out += data
        return len(data)


def test_send_all_retries_through_eagain(netutil):
    s = _SendSock(["eagain", "eagain", 4])  # two EAGAINs, then 4 bytes/call
    payload = b"abcdefghij"
    assert netutil.send_all(s, payload, timeout_s=5, slice_ms=1) == len(payload)
    assert bytes(s.out) == payload


def test_send_all_handles_none_and_partial(netutil):
    s = _SendSock([None, 3, 0, 100])  # None + zero backpressure + partial write
    payload = b"0123456789"
    assert netutil.send_all(s, payload, timeout_s=5, slice_ms=1) == len(payload)
    assert bytes(s.out) == payload


def test_send_all_delivers_when_socket_never_blocks(netutil):
    s = _SendSock()  # accepts everything in one go
    payload = b"x" * 300
    assert netutil.send_all(s, payload, timeout_s=5, slice_ms=1) == 300
    assert bytes(s.out) == payload


def test_send_all_times_out_on_stalled_receiver(netutil):
    s = _SendSock(["eagain"] * 10000)  # receiver never drains
    with pytest.raises(OSError) as ei:
        netutil.send_all(s, b"y" * 16, timeout_s=1, slice_ms=100)
    assert ei.value.args[0] == 110  # ETIMEDOUT
    assert bytes(s.out) == b""  # nothing sent


def test_send_all_non_eagain_oserror_propagates(netutil):
    class _Broken:
        def send(self, mv):
            raise OSError(9)  # EBADF, not EAGAIN

    with pytest.raises(OSError) as ei:
        netutil.send_all(_Broken(), b"z", timeout_s=5, slice_ms=1)
    assert ei.value.args[0] == 9


class _RecvSock:
    # readinto() plays a script: None -> no data yet, 0 -> EOF, int -> fill N.
    def __init__(self, script=()):
        self.script = list(script)
        self.fill = 0

    def readinto(self, mv):
        if not self.script:
            return 0
        op = self.script.pop(0)
        if op is None:
            return None
        if op == 0:
            return 0
        n = min(op, len(mv))
        for i in range(n):
            mv[i] = (self.fill + i) & 0xFF
        self.fill += n
        return n


def test_recv_into_retries_on_none(netutil):
    s = _RecvSock([None, 2, None, 3])  # data arrives with gaps
    buf = bytearray(5)
    assert netutil.recv_into(s, memoryview(buf), timeout_s=5, slice_ms=1) == 5


def test_recv_into_eof_returns_short(netutil):
    s = _RecvSock([2, 0])  # two bytes, then the peer closes
    buf = bytearray(5)
    assert netutil.recv_into(s, memoryview(buf), timeout_s=5, slice_ms=1) == 2


def test_recv_into_times_out_on_stalled_peer(netutil):
    s = _RecvSock([None] * 10000)  # open but never sends
    buf = bytearray(4)
    assert netutil.recv_into(s, memoryview(buf), timeout_s=1, slice_ms=100) == 0
