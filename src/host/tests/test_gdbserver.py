"""Offline unit tests for pod.gdbserver.

Covers the binary wire codec (PodLink against a fake socket), the RSP framing
helpers, the gdb-regnum <-> pod-regsel register map, the g/G/p/P handlers, the
memory path with SW-breakpoint filtering and flash-write rejection, the
breakpoint realisation policy, and the run-control + interrupt loop with stop
reply signal selection. No hardware, no real gdb, no network.
"""

import struct
import pytest

from pod.gdbserver import (
    PodLink,
    PodError,
    GdbServer,
    rsp_checksum,
    build_packet,
    parse_packet,
    rsp_unescape,
    rsp_escape,
    rsp_rle_expand,
    encode_u32_le_hex,
    decode_u32_le_hex,
    popcount,
    PROTOCOL_VERSION,
    MAX_DATA,
    FLASH_TOP,
    ALL_REGS_MASK,
    REG_MAP,
    STATUS_OK,
    STATUS_XFER,
    STATUS_NOTHALTED,
    STATUS_TIMEOUT,
    STATUS_WRPROT,
    OP_PING,
    OP_INFO,
    OP_HALT,
    OP_RESUME,
    OP_STEP,
    OP_READ_REG,
    OP_WRITE_REG,
    OP_READ_REGS,
    OP_WRITE_REGS,
    OP_READ_MEM,
    OP_WRITE_MEM,
    OP_RESUME_WAIT,
    OP_BP_SET,
    INTERRUPT_BYTE,
    SIGINT,
    SIGTRAP,
    SIGSEGV,
    S_HALT,
    S_LOCKUP,
    DFSR_BKPT,
)


# ── fakes ─────────────────────────────────────────────────────────────────


def frame(status, data=b"", reserved=0):
    """Build a pod response frame for the fake socket to hand back."""
    return struct.pack("<BBH", status, reserved, len(data)) + data


class FakeSocket:
    """A scripted socket: sendall() records requests, recv() drains a queue.

    Response frames are appended to .responses (bytes); recv() yields them in
    order, in <= n byte slices. sent records every sendall() payload.
    """

    def __init__(self, responses=b""):
        self._rx = bytearray(responses)
        self.sent = bytearray()

    def queue(self, data):
        self._rx += data

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self._rx:
            return b""
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def setsockopt(self, *a):
        pass

    def close(self):
        pass


class FakePodLink:
    """Records calls and returns canned values; substitutes for PodLink.

    GdbServer-level tests inject this so no real socket is needed. resume_wait
    pops scripted results from .wait_results; read_regs returns .regs; read_mem
    returns scripted memory; bp_set raises PodError when addr in .bp_fail.
    """

    def __init__(self):
        self.calls = []
        self.regs = {}
        self.mem = bytearray(0x40000000)   # sparse-ish backing store
        self.wait_results = []
        self.bp_fail = set()
        self.bp_set_addrs = []
        self.bp_clear_addrs = []
        self.bp_clear_all_count = 0
        self.written = []                   # (addr, bytes)
        self.write_regs_calls = []
        self.write_reg_calls = []
        self.interrupts = 0
        self._run_state = (S_HALT, 0)
        self.reset_calls = []
        self.fail_status = None             # if set, reads raise PodError(status)

    def run_state(self):
        self.calls.append(("run_state",))
        return self._run_state

    def reset(self, mode):
        self.reset_calls.append(mode)
        return S_HALT

    def read_regs(self, mask):
        self.calls.append(("read_regs", mask))
        if self.fail_status is not None:
            raise PodError(self.fail_status)
        return {regsel: self.regs.get(regsel, 0)
                for regsel in range(32) if mask & (1 << regsel)}

    def write_regs(self, mask, values):
        self.write_regs_calls.append((mask, dict(values)))

    def read_reg(self, regsel):
        self.calls.append(("read_reg", regsel))
        return self.regs.get(regsel, 0)

    def write_reg(self, regsel, value):
        self.write_reg_calls.append((regsel, value))

    def read_mem(self, addr, length):
        self.calls.append(("read_mem", addr, length))
        if self.fail_status is not None:
            raise PodError(self.fail_status)
        return bytes(self.mem[addr:addr + length])

    def write_mem(self, addr, data):
        self.written.append((addr, bytes(data)))
        self.mem[addr:addr + len(data)] = data

    def resume(self):
        self.calls.append(("resume",))

    def step(self, maskints=True):
        self.calls.append(("step", maskints))
        return (S_HALT, DFSR_BKPT)

    def resume_wait(self, window_ms, already_running=False, interrupt=False):
        self.calls.append(("resume_wait", window_ms, already_running, interrupt))
        if interrupt:
            self.interrupts += 1
        return self.wait_results.pop(0)

    def bp_set(self, addr):
        self.bp_set_addrs.append(addr)
        if addr in self.bp_fail:
            raise PodError(STATUS_XFER)
        return 0

    def bp_clear(self, addr):
        self.bp_clear_addrs.append(addr)

    def bp_clear_all(self):
        self.bp_clear_all_count += 1


def make_server(link=None):
    s = GdbServer(link=link or FakePodLink(), resume_window_ms=200)
    return s


# ── PodLink wire codec ──────────────────────────────────────────────────────


class TestPodLinkCodec:
    def test_ping_request_and_response(self):
        sock = FakeSocket(frame(STATUS_OK, struct.pack("<I", PROTOCOL_VERSION)))
        link = PodLink(sock)
        assert link.ping() == PROTOCOL_VERSION
        # request frame: opcode, flags=0, arg_len=0
        assert sock.sent == struct.pack("<BBH", OP_PING, 0, 0)

    def test_info_unpacks_five_words(self):
        payload = struct.pack("<5I", 1, 2, 3, 4, 5)
        sock = FakeSocket(frame(STATUS_OK, payload))
        link = PodLink(sock)
        assert link.info() == (1, 2, 3, 4, 5)

    def test_halt_returns_dhcsr(self):
        sock = FakeSocket(frame(STATUS_OK, struct.pack("<I", S_HALT)))
        link = PodLink(sock)
        assert link.halt() == S_HALT
        assert sock.sent == struct.pack("<BBH", OP_HALT, 0, 0)

    def test_resume_sends_opcode(self):
        sock = FakeSocket(frame(STATUS_OK))
        link = PodLink(sock)
        link.resume()
        assert sock.sent == struct.pack("<BBH", OP_RESUME, 0, 0)

    def test_step_packs_maskints_and_unpacks_pair(self):
        sock = FakeSocket(frame(STATUS_OK, struct.pack("<2I", S_HALT, DFSR_BKPT)))
        link = PodLink(sock)
        dhcsr, dfsr = link.step(maskints=True)
        assert (dhcsr, dfsr) == (S_HALT, DFSR_BKPT)
        assert sock.sent == struct.pack("<BBH", OP_STEP, 0, 1) + b"\x01"

    def test_step_maskints_false(self):
        sock = FakeSocket(frame(STATUS_OK, struct.pack("<2I", 0, 0)))
        link = PodLink(sock)
        link.step(maskints=False)
        assert sock.sent[-1] == 0

    def test_read_reg(self):
        sock = FakeSocket(frame(STATUS_OK, struct.pack("<I", 0xdeadbeef)))
        link = PodLink(sock)
        assert link.read_reg(15) == 0xdeadbeef
        assert sock.sent == struct.pack("<BBH", OP_READ_REG, 0, 1) + b"\x0f"

    def test_write_reg(self):
        sock = FakeSocket(frame(STATUS_OK))
        link = PodLink(sock)
        link.write_reg(0, 0x12345678)
        assert sock.sent == (struct.pack("<BBH", OP_WRITE_REG, 0, 5)
                             + struct.pack("<BI", 0, 0x12345678))

    def test_read_regs_mask_popcount_ascending(self):
        mask = (1 << 0) | (1 << 15) | (1 << 16)
        # pod returns values in ascending regsel order
        payload = struct.pack("<3I", 0xaa, 0xbb, 0xcc)
        sock = FakeSocket(frame(STATUS_OK, payload))
        link = PodLink(sock)
        out = link.read_regs(mask)
        assert out == {0: 0xaa, 15: 0xbb, 16: 0xcc}
        # request body is the 4-byte mask
        body = sock.sent[4:]
        assert struct.unpack("<I", body)[0] == mask

    def test_write_regs_orders_ascending(self):
        mask = (1 << 16) | (1 << 0)
        sock = FakeSocket(frame(STATUS_OK))
        link = PodLink(sock)
        link.write_regs(mask, {0: 0x11, 16: 0x22})
        body = sock.sent[4:]
        got_mask = struct.unpack_from("<I", body, 0)[0]
        v0, v1 = struct.unpack_from("<2I", body, 4)
        assert got_mask == mask
        assert (v0, v1) == (0x11, 0x22)   # ascending regsel: 0 then 16

    def test_read_mem_single_chunk(self):
        data = bytes(range(16))
        sock = FakeSocket(frame(STATUS_OK, data))
        link = PodLink(sock)
        assert link.read_mem(0x20000000, 16) == data
        body = sock.sent[4:]
        addr, length = struct.unpack("<II", body)
        assert (addr, length) == (0x20000000, 16)

    def test_read_mem_chunks_at_max_data(self):
        # request 2 * MAX_DATA + 10 bytes; expect 3 READ_MEM frames
        total = 2 * MAX_DATA + 10
        responses = b""
        for chunk in (MAX_DATA, MAX_DATA, 10):
            responses += frame(STATUS_OK, b"\x00" * chunk)
        sock = FakeSocket(responses)
        link = PodLink(sock)
        out = link.read_mem(0x1000, total)
        assert len(out) == total
        # three requests sent (each 12-byte: 4 hdr + 8 args)
        assert len(sock.sent) == 3 * 12

    def test_write_mem_chunks_under_max(self):
        # data larger than MAX_DATA - 8 must split into multiple frames
        data = b"\xab" * (MAX_DATA + 100)
        responses = frame(STATUS_OK) + frame(STATUS_OK)
        sock = FakeSocket(responses)
        link = PodLink(sock)
        link.write_mem(0x20000000, data)
        # two frames, neither arg_len exceeding MAX_DATA
        # parse the sent stream
        offset = 0
        frames = 0
        while offset < len(sock.sent):
            opcode, flags, arg_len = struct.unpack_from("<BBH", sock.sent, offset)
            assert arg_len <= MAX_DATA
            offset += 4 + arg_len
            frames += 1
        assert frames == 2

    def test_status_maps_to_poderror(self):
        sock = FakeSocket(frame(STATUS_NOTHALTED))
        link = PodLink(sock)
        with pytest.raises(PodError) as exc:
            link.read_reg(0)
        assert exc.value.status == STATUS_NOTHALTED

    def test_write_protected_status(self):
        sock = FakeSocket(frame(STATUS_WRPROT))
        link = PodLink(sock)
        with pytest.raises(PodError) as exc:
            link.write_mem(0x1000, b"\x00\x00")
        assert exc.value.status == STATUS_WRPROT

    def test_resume_wait_timeout_is_return_not_raise(self):
        sock = FakeSocket(frame(STATUS_TIMEOUT, struct.pack("<I", 0)))
        link = PodLink(sock)
        result = link.resume_wait(200, already_running=False)
        assert result["state"] == "timeout"
        # flags byte: already_running == 0
        assert sock.sent[-1] == 0

    def test_resume_wait_halt(self):
        sock = FakeSocket(frame(STATUS_OK, struct.pack("<2I", S_HALT, DFSR_BKPT)))
        link = PodLink(sock)
        result = link.resume_wait(200, already_running=False)
        assert result["state"] == "halt"
        assert result["dhcsr"] == S_HALT
        assert result["dfsr"] == DFSR_BKPT
        assert result["host_halted"] is False

    def test_resume_wait_host_halted_flag(self):
        sock = FakeSocket(
            frame(STATUS_OK, struct.pack("<2I", S_HALT, 0), reserved=1))
        link = PodLink(sock)
        result = link.resume_wait(200, already_running=True)
        assert result["host_halted"] is True
        # flags byte: already_running == 1
        body = sock.sent[4:]
        window, flags = struct.unpack("<IB", body)
        assert (window, flags) == (200, 1)

    def test_resume_wait_interrupt_flag_set_in_request(self):
        # The host Ctrl-C path carries the interrupt as a framed RESUME_WAIT
        # flag bit, never a bare 0x03 byte (which would corrupt pod framing).
        sock = FakeSocket(
            frame(STATUS_OK, struct.pack("<2I", S_HALT, 0), reserved=1))
        link = PodLink(sock)
        result = link.resume_wait(200, already_running=True, interrupt=True)
        assert result["host_halted"] is True
        body = sock.sent[4:]
        window, flags = struct.unpack("<IB", body)
        # both already_running (bit0) and interrupt (bit1) set
        assert (window, flags) == (200, 0b11)

    def test_bp_set_returns_slot(self):
        sock = FakeSocket(frame(STATUS_OK, b"\x02"))
        link = PodLink(sock)
        assert link.bp_set(0x1000) == 2
        body = sock.sent[4:]
        assert struct.unpack("<I", body)[0] == 0x1000

    def test_recv_exact_eof_raises(self):
        sock = FakeSocket(b"\x00\x00")   # short header
        link = PodLink(sock)
        with pytest.raises(PodError):
            link.ping()

    def test_request_rejects_oversize_args(self):
        sock = FakeSocket()
        link = PodLink(sock)
        with pytest.raises(ValueError):
            link._request(OP_WRITE_MEM, b"\x00" * (MAX_DATA + 1))


# ── RSP framing helpers ──────────────────────────────────────────────────────


class TestRspFraming:
    def test_checksum(self):
        assert rsp_checksum(b"OK") == "%02x" % ((ord("O") + ord("K")) & 0xFF)

    def test_build_packet_roundtrip(self):
        pkt = build_packet("OK")
        payload, ok, consumed = parse_packet(pkt)
        assert payload == b"OK"
        assert ok is True
        assert consumed == len(pkt)

    def test_parse_incomplete_returns_none(self):
        payload, ok, consumed = parse_packet(b"$OK")
        assert payload is None
        assert consumed == 0

    def test_parse_bad_checksum(self):
        payload, ok, consumed = parse_packet(b"$OK#00")
        assert payload == b"OK"
        assert ok is False

    def test_parse_skips_leading_garbage(self):
        payload, ok, consumed = parse_packet(b"+$OK#9a")
        assert payload == b"OK"
        assert ok is True

    def test_unescape(self):
        # 0x7d 0x5d decodes to 0x7d
        assert rsp_unescape(b"\x7d\x5d") == b"\x7d"
        # 0x7d 0x03 decodes to 0x23 ('#')
        assert rsp_unescape(b"\x7d\x03") == b"#"

    def test_escape_roundtrip(self):
        raw = bytes([0x23, 0x24, 0x7d, 0x2a, 0x00, 0xff])
        assert rsp_unescape(rsp_escape(raw)) == raw

    def test_rle_expand_basic(self):
        # 'a' '*' (count char) repeats the preceding 'a' (count_char - 29) times.
        # ' ' is 0x20 = 32, so count = 32 - 29 = 3 extra 'a' -> 4 total.
        assert rsp_rle_expand(b"a* ") == b"aaaa"

    def test_rle_expand_no_star_passthrough(self):
        assert rsp_rle_expand(b"01020304") == b"01020304"

    def test_rle_expand_in_g_body(self):
        # A run-length-compressed run of zero bytes inside a payload expands
        # before the hex decode. "0*\"" repeats '0': count = 0x22 - 29 = 5 extra,
        # so '0' x6 then "00" -> eight '0' chars = one all-zero LE word.
        assert rsp_rle_expand(b"0*\"00") == b"0" * 8

    def test_rle_expand_leading_star_kept(self):
        # A '*' with no preceding byte is not a run; it is passed through.
        assert rsp_rle_expand(b"*x") == b"*x"

    def test_u32_le_hex_roundtrip(self):
        assert encode_u32_le_hex(0xdeadbeef) == "efbeadde"
        assert decode_u32_le_hex("efbeadde") == 0xdeadbeef

    def test_popcount(self):
        assert popcount(0) == 0
        assert popcount(0b1011) == 3
        assert popcount(ALL_REGS_MASK) == len(REG_MAP)


# ── register map: g / G / p / P ──────────────────────────────────────────────


class TestRegisterMap:
    def test_all_regs_mask_covers_map(self):
        for _gdb, regsel, _n in REG_MAP:
            assert ALL_REGS_MASK & (1 << regsel)

    def test_g_emits_gdbnum_order_le(self):
        link = FakePodLink()
        # distinct value per pod regsel so order is observable
        link.regs = {regsel: regsel + 0x100 for _g, regsel, _n in REG_MAP}
        server = make_server(link)
        out = server.handle_packet(b"g")
        # the g-packet is concatenated 8-hex-char LE words in gdb-regnum order
        words = [out[i:i + 8] for i in range(0, len(out), 8)]
        assert len(words) == len(REG_MAP)
        for idx, (_gdb, regsel, _n) in enumerate(REG_MAP):
            assert words[idx] == encode_u32_le_hex(regsel + 0x100)

    def test_g_xpsr_lands_at_regnum_19_slot(self):
        # gdb regnum 19 (xpsr) must read pod regsel 16.
        link = FakePodLink()
        link.regs = {16: 0x21000000}        # xpsr value lives at regsel 16
        server = make_server(link)
        out = server.handle_packet(b"g")
        # find the slot index for gdb regnum 19
        idx = [i for i, (g, _r, _n) in enumerate(REG_MAP) if g == 19][0]
        word = out[idx * 8:idx * 8 + 8]
        assert word == encode_u32_le_hex(0x21000000)

    def test_g_msp_psp_slots(self):
        link = FakePodLink()
        link.regs = {17: 0x20008000, 18: 0x20007000}
        server = make_server(link)
        out = server.handle_packet(b"g")
        msp_idx = [i for i, (g, _r, _n) in enumerate(REG_MAP) if g == 17][0]
        psp_idx = [i for i, (g, _r, _n) in enumerate(REG_MAP) if g == 18][0]
        assert out[msp_idx * 8:msp_idx * 8 + 8] == encode_u32_le_hex(0x20008000)
        assert out[psp_idx * 8:psp_idx * 8 + 8] == encode_u32_le_hex(0x20007000)

    def test_G_roundtrips_to_write_regs(self):
        link = FakePodLink()
        server = make_server(link)
        values = {regsel: regsel + 0x10 for _g, regsel, _n in REG_MAP}
        body = "".join(encode_u32_le_hex(values[regsel])
                       for _g, regsel, _n in REG_MAP)
        reply = server.handle_packet(("G" + body).encode("ascii"))
        assert reply == "OK"
        assert len(link.write_regs_calls) == 1
        mask, written = link.write_regs_calls[0]
        assert mask == ALL_REGS_MASK
        assert written == values

    def test_p_maps_gdbnum_to_regsel(self):
        # gdb regnum 19 (xpsr) -> pod regsel 16
        link = FakePodLink()
        link.regs = {16: 0x01000000}
        server = make_server(link)
        out = server.handle_packet(b"p13")  # hex 0x13 == 19
        assert out == encode_u32_le_hex(0x01000000)

    def test_p_unknown_regnum_errors(self):
        # Unknown register -> error reply (matches P), not a fabricated zero.
        server = make_server()
        out = server.handle_packet(b"p99")
        assert out == "E01"

    def test_P_writes_through(self):
        link = FakePodLink()
        server = make_server(link)
        # P f=<value>  -> gdb regnum 15 (pc) -> pod regsel 15
        reply = server.handle_packet(("Pf=" + encode_u32_le_hex(0x1234)).encode())
        assert reply == "OK"
        assert link.write_reg_calls == [(15, 0x1234)]

    def test_P_unknown_regnum_errors(self):
        server = make_server()
        reply = server.handle_packet(b"P99=00000000")
        assert reply == "E01"


# ── memory: m / M / X, cache, SW-bp filter, flash rejection ──────────────────


class TestMemory:
    def test_m_issues_read_mem(self):
        link = FakePodLink()
        link.mem[0x20000000:0x20000004] = b"\x01\x02\x03\x04"
        server = make_server(link)
        out = server.handle_packet(b"m20000000,4")
        assert out == "01020304"

    def test_m_cache_coalesces_overlapping_reads(self):
        link = FakePodLink()
        link.mem[0x20000000:0x20000040] = bytes(range(0x40))
        server = make_server(link)
        server.handle_packet(b"m20000000,4")
        server.handle_packet(b"m20000004,4")
        # both reads land in the same 64-byte cache line -> one READ_MEM
        read_calls = [c for c in link.calls if c[0] == "read_mem"]
        assert len(read_calls) == 1

    def test_m_cache_invalidated_on_step(self):
        link = FakePodLink()
        link.mem[0x20000000:0x20000040] = bytes(range(0x40))
        link.regs = {7: 0, 13: 0, 14: 0, 15: 0}
        server = make_server(link)
        server.handle_packet(b"m20000000,4")
        server.handle_packet(b"s")            # step invalidates the cache
        server.handle_packet(b"m20000000,4")
        read_calls = [c for c in link.calls if c[0] == "read_mem"]
        assert len(read_calls) == 2

    def test_M_flash_rejected(self):
        link = FakePodLink()
        server = make_server(link)
        reply = server.handle_packet(b"M1000,4:00000000")
        assert reply == "E01"
        assert link.written == []             # no WRITE_MEM sent

    def test_M_ram_writes(self):
        link = FakePodLink()
        server = make_server(link)
        reply = server.handle_packet(b"M20000000,4:01020304")
        assert reply == "OK"
        assert link.written == [(0x20000000, b"\x01\x02\x03\x04")]

    def test_X_flash_rejected(self):
        link = FakePodLink()
        server = make_server(link)
        reply = server.handle_packet(b"X1000,4:\x00\x00\x00\x00")
        assert reply == "E01"
        assert link.written == []

    def test_X_ram_de_escapes(self):
        link = FakePodLink()
        server = make_server(link)
        # write 0x7d via the 0x7d 0x5d escape, into RAM
        pkt = b"X20000000,1:" + b"\x7d\x5d"
        reply = server.handle_packet(pkt)
        assert reply == "OK"
        assert link.written == [(0x20000000, b"\x7d")]

    def test_sw_bp_filtered_from_m_read(self):
        link = FakePodLink()
        # RAM code at 0x20001000: original halfword 0x4770 (bx lr)
        link.mem[0x20001000:0x20001002] = b"\x70\x47"
        server = make_server(link)
        # Z0 in RAM -> SW breakpoint patches 0xBE00 in place
        assert server.handle_packet(b"Z0,20001000,2") == "OK"
        assert link.mem[0x20001000:0x20001002] == b"\x00\xbe"
        # m read of the patched bytes returns the original, not 0xBE00
        out = server.handle_packet(b"m20001000,2")
        assert out == "7047"

    def test_sw_bp_filter_straddle(self):
        link = FakePodLink()
        link.mem[0x20001000:0x20001008] = bytes([1, 2, 3, 4, 5, 6, 7, 8])
        server = make_server(link)
        server.handle_packet(b"Z0,20001002,2")   # patch bytes at +2,+3
        out = server.handle_packet(b"m20001000,8")
        # bytes 2 and 3 must read as the saved originals (3, 4), not 0x00 0xbe
        assert out == "0102030405060708"


# ── breakpoint policy ────────────────────────────────────────────────────────


class TestBreakpointPolicy:
    def test_flash_z0_uses_fpb(self):
        link = FakePodLink()
        server = make_server(link)
        reply = server.handle_packet(b"Z0,1000,2")
        assert reply == "OK"
        assert link.bp_set_addrs == [0x1000]
        assert link.written == []

    def test_flash_z1_uses_fpb(self):
        link = FakePodLink()
        server = make_server(link)
        reply = server.handle_packet(b"Z1,2000,2")
        assert reply == "OK"
        assert link.bp_set_addrs == [0x2000]

    def test_flash_z0_fpb_failure_returns_error(self):
        link = FakePodLink()
        link.bp_fail = {0x3000}
        server = make_server(link)
        reply = server.handle_packet(b"Z0,3000,2")
        assert reply == "E01"

    def test_ram_z1_falls_back_to_sw(self):
        link = FakePodLink()
        link.mem[0x20002000:0x20002002] = b"\x70\x47"
        link.bp_fail = {0x20002000}            # FPBv1 cannot break in RAM
        server = make_server(link)
        reply = server.handle_packet(b"Z1,20002000,2")
        assert reply == "OK"
        # tried FPB, fell back to SW patch
        assert link.bp_set_addrs == [0x20002000]
        assert link.mem[0x20002000:0x20002002] == b"\x00\xbe"
        # the promotion is recorded so z1 clears SW
        assert 0x20002000 in server._promoted

    def test_ram_z1_promotion_teardown(self):
        link = FakePodLink()
        link.mem[0x20002000:0x20002002] = b"\x70\x47"
        link.bp_fail = {0x20002000}
        server = make_server(link)
        server.handle_packet(b"Z1,20002000,2")
        reply = server.handle_packet(b"z1,20002000,2")
        assert reply == "OK"
        # original restored, promotion cleared
        assert link.mem[0x20002000:0x20002002] == b"\x70\x47"
        assert 0x20002000 not in server._promoted

    def test_ram_z0_uses_sw(self):
        link = FakePodLink()
        link.mem[0x20003000:0x20003002] = b"\x00\xbf"   # nop
        server = make_server(link)
        reply = server.handle_packet(b"Z0,20003000,2")
        assert reply == "OK"
        assert link.bp_set_addrs == []                  # no FPB used
        assert link.mem[0x20003000:0x20003002] == b"\x00\xbe"

    def test_flash_bp_clear_uses_fpb(self):
        link = FakePodLink()
        server = make_server(link)
        server.handle_packet(b"Z0,1000,2")
        reply = server.handle_packet(b"z0,1000,2")
        assert reply == "OK"
        assert link.bp_clear_addrs == [0x1000]


# ── run control + interrupt + stop reply ─────────────────────────────────────


class TestRunControl:
    def _halted_regs(self, link):
        link.regs = {7: 0x7, 13: 0x20008000, 14: 0xfff, 15: 0x1234}

    def test_continue_halt_on_breakpoint(self):
        link = FakePodLink()
        self._halted_regs(link)
        link.wait_results = [
            {"state": "halt", "dhcsr": S_HALT, "dfsr": DFSR_BKPT,
             "host_halted": False},
        ]
        server = make_server(link)
        reply = server.handle_packet(b"c")
        assert reply.startswith("T05")
        assert "thread:1;" in reply
        # the T reply carries pc (gdb regnum 15) value
        assert encode_u32_le_hex(0x1234) in reply

    def test_continue_timeout_rearms_already_running(self):
        link = FakePodLink()
        self._halted_regs(link)
        link.wait_results = [
            {"state": "timeout", "dhcsr": 0},
            {"state": "timeout", "dhcsr": 0},
            {"state": "halt", "dhcsr": S_HALT, "dfsr": DFSR_BKPT,
             "host_halted": False},
        ]
        server = make_server(link)
        server.handle_packet(b"c")
        waits = [c for c in link.calls if c[0] == "resume_wait"]
        # first call resumes (already_running False), subsequent re-arm True;
        # no interrupt flag because no gdb 0x03 was seen
        assert waits[0] == ("resume_wait", 200, False, False)
        assert waits[1] == ("resume_wait", 200, True, False)
        assert waits[2] == ("resume_wait", 200, True, False)

    def test_continue_host_halted_is_sigint(self):
        link = FakePodLink()
        self._halted_regs(link)
        link.wait_results = [
            {"state": "halt", "dhcsr": S_HALT, "dfsr": 0, "host_halted": True},
        ]
        server = make_server(link)
        reply = server.handle_packet(b"c")
        assert reply.startswith("T%02x" % SIGINT)

    def test_lockup_is_sigsegv(self):
        link = FakePodLink()
        self._halted_regs(link)
        link.wait_results = [
            {"state": "halt", "dhcsr": S_HALT | S_LOCKUP, "dfsr": 0,
             "host_halted": False},
        ]
        server = make_server(link)
        reply = server.handle_packet(b"c")
        assert reply.startswith("T%02x" % SIGSEGV)

    def test_interrupt_forwarded_to_pod(self):
        # A 0x03 byte present on the gdb socket during a timeout cycle must be
        # forwarded to the pod as the framed RESUME_WAIT interrupt flag on the
        # next re-arm (FakePodLink counts an interrupt when called with
        # interrupt=True).
        link = FakePodLink()
        self._halted_regs(link)
        link.wait_results = [
            {"state": "timeout", "dhcsr": 0},
            {"state": "halt", "dhcsr": S_HALT, "dfsr": 0, "host_halted": True},
        ]
        server = make_server(link)
        server._gdb_sock = FakeGdbInterruptSocket()
        server.handle_packet(b"c")
        assert link.interrupts == 1

    def test_step_returns_stop_reply(self):
        link = FakePodLink()
        self._halted_regs(link)
        server = make_server(link)
        reply = server.handle_packet(b"s")
        assert reply.startswith("T05")
        # stepi must let the target's interrupts fire: maskints=False
        assert ("step", False) in link.calls

    def test_question_mark_stop_reply(self):
        link = FakePodLink()
        self._halted_regs(link)
        link._run_state = (S_HALT, DFSR_BKPT)
        server = make_server(link)
        reply = server.handle_packet(b"?")
        assert reply.startswith("T05")


class FakeGdbInterruptSocket:
    """A fake gdb socket that reports one pending 0x03 interrupt byte.

    Exposes poll_interrupt() so GdbServer._gdb_poll() can drive the interrupt
    path without a real select() on a file descriptor.
    """

    def __init__(self):
        self._pending = True

    def poll_interrupt(self):
        if self._pending:
            self._pending = False
            return True
        return False


# ── misc handlers ────────────────────────────────────────────────────────────


class TestMiscHandlers:
    def test_qsupported(self):
        server = make_server()
        reply = server.handle_packet(b"qSupported:multiprocess+")
        assert "qXfer:features:read+" in reply
        assert "QStartNoAckMode+" in reply
        # swbreak+/hwbreak+ are deliberately NOT advertised: the stop reply does
        # not emit the swbreak:/hwbreak: fields the negotiated contract requires,
        # so gdb falls back to PC matching (works because m-reads are filtered).
        assert "hwbreak+" not in reply
        assert "swbreak+" not in reply

    def test_qstartnoackmode_sets_flag(self):
        server = make_server()
        assert server.no_ack is False
        reply = server.handle_packet(b"QStartNoAckMode")
        assert reply == "OK"
        assert server.no_ack is True

    def test_qxfer_target_xml_slicing(self):
        server = make_server()
        first = server.handle_packet(b"qXfer:features:read:target.xml:0,20")
        assert first[:1] == b"m"             # more to come
        # request from a large offset returns the 'l' (last) prefix
        last = server.handle_packet(b"qXfer:features:read:target.xml:0,100000")
        assert last[:1] == b"l"
        assert b"m-profile" in last

    def test_qattached(self):
        assert make_server().handle_packet(b"qAttached") == "1"

    def test_qc(self):
        assert make_server().handle_packet(b"qC") == "QC1"

    def test_thread_info(self):
        server = make_server()
        assert server.handle_packet(b"qfThreadInfo") == "m1"
        assert server.handle_packet(b"qsThreadInfo") == "l"

    def test_vcont_query(self):
        assert make_server().handle_packet(b"vCont?") == "vCont;c;C;s;S"

    def test_vcont_step(self):
        link = FakePodLink()
        link.regs = {7: 0, 13: 0, 14: 0, 15: 0}
        server = make_server(link)
        reply = server.handle_packet(b"vCont;s:1")
        assert reply.startswith("T05")
        assert ("step", False) in link.calls

    def test_detach_clears_and_resumes(self):
        link = FakePodLink()
        server = make_server(link)
        reply = server.handle_packet(b"D")
        assert reply == "OK"
        assert link.bp_clear_all_count == 1
        assert ("resume",) in link.calls

    def test_vcont_continue(self):
        link = FakePodLink()
        link.regs = {7: 0, 13: 0, 14: 0, 15: 0}
        link.wait_results = [
            {"state": "halt", "dhcsr": S_HALT, "dfsr": DFSR_BKPT,
             "host_halted": False},
        ]
        server = make_server(link)
        reply = server.handle_packet(b"vCont;c:1")
        assert reply.startswith("T05")

    def test_unknown_packet_empty_reply(self):
        assert make_server().handle_packet(b"vUnknownThing") == ""

    def test_kill_returns_none(self):
        assert make_server().handle_packet(b"k") is None

    def test_extended_restart_resets_no_reply(self):
        # 'R XX' resets the DUT through the debugger and sends no reply.
        link = FakePodLink()
        server = make_server(link)
        reply = server.handle_packet(b"R00")
        assert reply is None
        assert link.reset_calls == [1]

    def test_bang_extended_mode_ok(self):
        assert make_server().handle_packet(b"!") == "OK"

    def test_monitor_reset_run(self):
        # qRcmd,<hex 'reset'> -> reset mode 0 (sysreset+run)
        link = FakePodLink()
        server = make_server(link)
        reply = server.handle_packet(b"qRcmd," + b"reset".hex().encode())
        assert reply == "OK"
        assert link.reset_calls == [0]

    def test_monitor_reset_halt(self):
        link = FakePodLink()
        server = make_server(link)
        reply = server.handle_packet(b"qRcmd," + b"reset halt".hex().encode())
        assert reply == "OK"
        assert link.reset_calls == [1]


class TestPacketValidation:
    def test_M_length_mismatch_rejected(self):
        link = FakePodLink()
        server = make_server(link)
        # declared length 4, but only 2 bytes of hex data supplied
        reply = server.handle_packet(b"M20000000,4:0102")
        assert reply == "E01"
        assert link.written == []

    def test_X_length_mismatch_rejected(self):
        link = FakePodLink()
        server = make_server(link)
        # declared length 4, but 1 binary byte supplied
        reply = server.handle_packet(b"X20000000,4:\x01")
        assert reply == "E01"
        assert link.written == []

    def test_G_short_body_rejected(self):
        link = FakePodLink()
        server = make_server(link)
        reply = server.handle_packet(b"G0102")     # far short of the full block
        assert reply == "E01"
        assert link.write_regs_calls == []


# ── serve loop: framing, ack/retransmit, PodError boundary, EOF ──────────────


class ScriptedGdbSocket:
    """A fake gdb socket scripted with inbound byte chunks for _rsp_loop.

    recv() yields the queued chunks in order then b'' (EOF). sendall() records
    everything the server writes back so acks and reply packets can be asserted.
    poll_event() (used by GdbServer._gdb_poll during a continue) reports EOF once
    the scripted chunks are exhausted, simulating a gdb disconnect mid-continue.
    """

    def __init__(self, chunks, poll_eof=False):
        self._chunks = list(chunks)
        self.sent = bytearray()
        self._poll_eof = poll_eof

    def recv(self, n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def poll_event(self):
        # Only used while a continue is running. Report EOF when enabled and the
        # inbound script is drained (the peer has closed).
        if self._poll_eof and not self._chunks:
            return "eof"
        return None

    def sendall(self, data):
        self.sent += data

    def setsockopt(self, *a):
        pass

    def close(self):
        pass


class TestServeLoop:
    def test_poderror_maps_to_enn_not_teardown(self):
        # A handler that raises PodError mid-session must reply 'Enn' to gdb, not
        # tear down the connection (blocker 1/4). Pin: read_regs raises, the loop
        # answers E01 and keeps serving until EOF.
        link = FakePodLink()
        link.fail_status = STATUS_XFER          # read_regs/read_mem raise -> E01
        server = make_server(link)
        # send a 'g' (which reads regs) then EOF
        pkt = build_packet("g")
        sock = ScriptedGdbSocket([bytes(pkt)])
        server._rsp_loop(sock)
        # ack '+' then an error reply packet $E01#..
        assert b"+" in sock.sent
        assert b"$E01#" in sock.sent

    def test_bad_checksum_retransmit(self):
        # gdb '-' (nak) re-sends the last packet; a bad inbound checksum is naked.
        link = FakePodLink()
        link.regs = {regsel: 0 for _g, regsel, _n in REG_MAP}
        server = make_server(link)
        good = build_packet("g")
        # first deliver a good g (gets '+', reply), then a '-' which retransmits
        sock = ScriptedGdbSocket([bytes(good), b"-"])
        server._rsp_loop(sock)
        # the reply packet was sent at least twice (original + retransmit)
        reply_count = sock.sent.count(b"#")
        # one ack '+' (no '#') + two identical reply packets each with a '#'
        assert reply_count >= 2

    def test_inbound_nak_on_bad_checksum(self):
        link = FakePodLink()
        server = make_server(link)
        # a packet with a deliberately wrong checksum
        sock = ScriptedGdbSocket([b"$g#00"])
        server._rsp_loop(sock)
        assert sock.sent.startswith(b"-")

    def test_leading_interrupt_byte_stripped(self):
        # a stray 0x03 between packets must not desync framing.
        link = FakePodLink()
        link.regs = {regsel: 0 for _g, regsel, _n in REG_MAP}
        server = make_server(link)
        good = build_packet("g")
        sock = ScriptedGdbSocket([bytes([INTERRUPT_BYTE]) + bytes(good)])
        server._rsp_loop(sock)
        assert b"+" in sock.sent
        # a g reply (40 hex chars for the register block) was produced
        assert b"$" in sock.sent

    def test_partial_packet_reassembly(self):
        link = FakePodLink()
        link.regs = {regsel: 0 for _g, regsel, _n in REG_MAP}
        server = make_server(link)
        good = bytes(build_packet("g"))
        # split the packet across two recv() boundaries
        sock = ScriptedGdbSocket([good[:3], good[3:]])
        server._rsp_loop(sock)
        assert b"+" in sock.sent
        assert sock.sent.count(b"$") == 1       # exactly one reply

    def test_continue_eof_ends_session_cleanly(self):
        # gdb disconnect during a continue (EOF on the gdb socket while the pod
        # keeps timing out) must end the session, not spin forever (blocker 2).
        link = FakePodLink()
        link.regs = {7: 0, 13: 0, 14: 0, 15: 0}
        # an unbounded stream of timeouts; only an EOF can end the loop
        link.wait_results = [{"state": "timeout", "dhcsr": 0}] * 1000
        server = make_server(link)
        cont = bytes(build_packet("c"))
        # deliver 'c', then the gdb peer closes (poll reports EOF mid-continue)
        sock = ScriptedGdbSocket([cont], poll_eof=True)
        server._gdb_sock = sock
        # must return (not hang); the EOF poll on the gdb socket raises
        # _GdbDisconnect inside _continue, caught by the loop as session end.
        server._rsp_loop(sock)
        # the loop consumed fewer than all scripted timeouts (it broke out)
        assert len(link.wait_results) > 0
