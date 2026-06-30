# Stateless binary DAP RPC over TCP for the host GDB RSP server (Phase 3).
#
# The pod holds no RSP state. This server is a remote DAP RPC: fixed-opcode,
# little-endian, one request frame in, one response frame out, dispatched
# straight onto the swd_dap primitives. All GDB protocol semantics (target.xml,
# the g-packet map, software-breakpoint memory filtering, the Z/z manager, and
# the stop-reply signal decode) live on the host, where they are unit-testable
# offline and never baked into firmware.
#
# The one exception to one-request/one-response is RESUME_WAIT (0x16): the pod
# resumes the core (clearing DFSR first) and then loops, polling DHCSR.S_HALT for
# up to a bounded wait-window. This collapses a Wi-Fi busy-poll into one
# round-trip per window. The host Ctrl-C path is the framed RW_FLAG_INTERRUPT bit
# on a RESUME_WAIT request (set on the next re-arm when gdb sends 0x03), so the
# interrupt rides as a proper request frame and never as a bare byte between
# frames where _read_exact would misread it as a header. The wait loop still
# select()s the socket, but only to detect a true EOF (peer closed); stray bytes
# are drained and ignored.
#
# Memory is bounded: the only buffers are the 4-byte header, one args frame
# (<= MAX_DATA), and one READ_MEM output (<= MAX_DATA). Nothing scales with
# session length. The socket is closed on all paths. Unlike flash_stream /
# dump_stream this server never auto-resumes the core; it leaves it in whatever
# run state the last command set (the host's detach decides run-vs-halt).

import struct
import socket
import select
import time

from . import swd_dap

PROTOCOL_VERSION = 1
MAX_DATA = 4096
FLASH_TOP = 0x20000000          # writes below this are flash (NVMC) -> rejected

# Status codes (response u8).
STATUS_OK = 0
STATUS_XFER = 1                 # SWD/ADIv5 TransferError
STATUS_BADOP = 2                # unknown opcode
STATUS_NOTHALTED = 3            # register/memory op while the core is running
STATUS_TIMEOUT = 4             # RESUME_WAIT window elapsed, core still running
STATUS_BADARGS = 5             # malformed/short args, bad regsel, len > MAX_DATA
STATUS_WRPROT = 6              # WRITE_MEM targeting the flash region

# Opcodes (request u8). See the frozen wire contract for full semantics.
OP_PING = 0x01
OP_INFO = 0x02
OP_HALT = 0x10
OP_RESUME = 0x11
OP_STEP = 0x12
OP_RUN_STATE = 0x13
OP_RESET = 0x14
OP_RESUME_WAIT = 0x16
OP_READ_REG = 0x20
OP_WRITE_REG = 0x21
OP_READ_REGS = 0x22
OP_WRITE_REGS = 0x23
OP_READ_MEM = 0x30
OP_WRITE_MEM = 0x31
OP_BP_SET = 0x40
OP_BP_CLEAR = 0x41
OP_BP_CLEAR_ALL = 0x42
OP_WATCH_SET = 0x43
OP_WATCH_CLEAR = 0x44

REGSEL_MAX = 18                 # 0..15 + xPSR(16) + MSP(17) + PSP(18)
POLL_SLEEP_MS = 1               # brief on-pod spin between selects in RESUME_WAIT

# RESUME_WAIT request flags (low byte of the u8 flags arg).
RW_FLAG_ALREADY_RUNNING = 1 << 0   # do NOT resume; just wait (re-arm a window)
RW_FLAG_INTERRUPT = 1 << 1         # halt the core now and reply (host Ctrl-C)

# Socket timeouts (seconds). A dead host must not wedge the server holding the
# SWD session: accept() and the per-frame reads time out and serve() returns
# cleanly to the REPL. The wait-window inside RESUME_WAIT provides its own
# liveness during a continue; these guard the idle-between-commands and accept.
ACCEPT_TIMEOUT_S = 300          # wait this long for a gdb client before giving up
FRAME_TIMEOUT_S = 120           # idle gap between framed requests before EOF


def _read_exact(sock, n):
    # Reassemble exactly n bytes from the stream; None on EOF (peer closed) or a
    # socket timeout (a dead host stalls the read). Treating the timeout as EOF
    # lets serve() return cleanly to the REPL rather than wedging while it holds
    # the SWD session and the single dupterm.
    buf = bytearray(n)
    mv = memoryview(buf)
    got = 0
    while got < n:
        try:
            r = sock.readinto(mv[got:])
        except OSError:
            return None
        if not r:
            return None
        got += r
    return bytes(buf)


def _reply(sock, status, data=b"", reserved=0):
    sock.sendall(struct.pack("<BBH", status, reserved, len(data)) + data)


def _read_mem(ap, addr, length):
    # Unaligned head/tail via read8; the aligned middle via read_block32.
    out = bytearray()
    while length and (addr & 3):
        out.append(ap.read8(addr))
        addr += 1
        length -= 1
    nwords = length >> 2
    if nwords:
        for w in ap.read_block32(addr, nwords):
            out += struct.pack("<I", w)
        addr += nwords * 4
        length -= nwords * 4
    while length:
        out.append(ap.read8(addr))
        addr += 1
        length -= 1
    return bytes(out)


def _write_mem(ap, addr, data):
    # Halfword-aligned head/tail via write16, byte-unaligned via write8, the
    # aligned middle via write32. Halfword writes (the common case for a 2-byte
    # SW-breakpoint patch at a Thumb-aligned address) collapse to one SWD
    # transfer instead of two. The flash guard is applied by the dispatcher
    # before this runs.
    i = 0
    n = len(data)
    # Byte-align to a halfword boundary if needed.
    if (addr & 1) and i < n:
        ap.write8(addr, data[i])
        addr += 1
        i += 1
    # Halfword head until word-aligned.
    while (addr & 3) and n - i >= 2:
        ap.write16(addr, struct.unpack_from("<H", data, i)[0])
        addr += 2
        i += 2
    if (addr & 3) and i < n:
        ap.write8(addr, data[i])
        addr += 1
        i += 1
    while n - i >= 4:
        ap.write32(addr, struct.unpack_from("<I", data, i)[0])
        addr += 4
        i += 4
    while n - i >= 2:
        ap.write16(addr, struct.unpack_from("<H", data, i)[0])
        addr += 2
        i += 2
    while i < n:
        ap.write8(addr, data[i])
        addr += 1
        i += 1


def _resume_wait(sock, cm, window_ms, flags):
    # Bounded, interruptible continue. Returns a (kind, dhcsr, dfsr) tuple; kind
    # is one of "halt", "host", "timeout", "eof".
    #
    # The host Ctrl-C path is the framed RW_FLAG_INTERRUPT bit on this request,
    # not a bare 0x03 byte between frames (which would corrupt _read_exact's
    # framing). When the flag is set the core is halted immediately and the
    # reply carries the host_halted marker.
    if flags & RW_FLAG_INTERRUPT:
        cm.halt()
        return ("host", cm.read_dhcsr(), cm.read_dfsr())
    if not (flags & RW_FLAG_ALREADY_RUNNING):
        cm.clear_dfsr()
        cm.resume()
    deadline = time.ticks_add(time.ticks_ms(), window_ms)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        # A readable socket mid-wait is either EOF (peer closed) or, defensively,
        # a stray byte. A true EOF ends the session; a stray non-0x03 byte is
        # drained and ignored so a benign glitch on the shared socket is
        # tolerated rather than fatal. The framed interrupt is handled above.
        r, _, _ = select.select([sock], [], [], 0)
        if r:
            b = sock.recv(1)
            if not b:
                return ("eof", 0, 0)
            # Drain and ignore any stray byte; keep waiting.
        dhcsr = cm.read_dhcsr()
        if dhcsr & swd_dap.S_HALT:
            return ("halt", dhcsr, cm.read_dfsr())
        # Brief spin between selects keeps the DHCSR poll tight without
        # saturating the link; the PIO SWD read dominates this loop anyway.
        time.sleep_ms(POLL_SLEEP_MS)
    return ("timeout", cm.read_dhcsr(), 0)


def _require_halted(cm):
    if not (cm.read_dhcsr() & swd_dap.S_HALT):
        raise _NotHalted()


class _NotHalted(Exception):
    pass


def _dispatch(cl, opcode, args, dp, ap, cm, fpb, dwt):
    # Flat opcode dispatch onto the primitives. struct.unpack_from raises on a
    # short args frame; that and out-of-range regsel become STATUS_BADARGS via
    # the serve loop's (ValueError, IndexError) handler.
    if opcode == OP_PING:
        _reply(cl, STATUS_OK, struct.pack("<I", PROTOCOL_VERSION))

    elif opcode == OP_INFO:
        # dpidr and cpuid are architectural and target-independent. The trailing
        # part/flash_kb/ram_kb reads target RP2350 SYSINFO/ROM addresses and are
        # NOT valid on the nRF52840 (the hardware-validated target); they are
        # RP2350-only and unvalidated elsewhere. No RSP handler consumes them
        # (target geometry comes from target.xml), so they are diagnostic only.
        dpidr = dp.dpidr if dp.dpidr is not None else 0
        _reply(cl, STATUS_OK, struct.pack(
            "<IIIII", dpidr, cm.cpuid(), ap.read32(0x10000100),
            ap.read32(0x10000110), ap.read32(0x1000010C)))

    elif opcode == OP_HALT:
        cm.halt()
        _reply(cl, STATUS_OK, struct.pack("<I", cm.read_dhcsr()))

    elif opcode == OP_RESUME:
        cm.clear_dfsr()
        cm.resume()
        _reply(cl, STATUS_OK)

    elif opcode == OP_STEP:
        _require_halted(cm)             # stepping a running core is undefined
        maskints = bool(args[0])
        cm.clear_dfsr()
        cm.step(maskints=maskints)
        _reply(cl, STATUS_OK, struct.pack("<II", cm.read_dhcsr(), cm.read_dfsr()))

    elif opcode == OP_RUN_STATE:
        _reply(cl, STATUS_OK, struct.pack("<II", cm.read_dhcsr(), cm.read_dfsr()))

    elif opcode == OP_RESET:
        mode = args[0]
        if mode == 0:
            cm.sysreset()
            cm.clear_dfsr()             # match RESUME/STEP: no stale halt cause
            cm.resume()
        else:
            cm.reset_and_halt()
            cm.clear_dfsr()             # fresh cause for the host's first read
        _reply(cl, STATUS_OK, struct.pack("<I", cm.read_dhcsr()))

    elif opcode == OP_RESUME_WAIT:
        window_ms, flags = struct.unpack_from("<IB", args, 0)
        kind, dhcsr, dfsr = _resume_wait(cl, cm, window_ms, flags)
        if kind == "halt":
            _reply(cl, STATUS_OK, struct.pack("<II", dhcsr, dfsr), reserved=0)
        elif kind == "host":
            # header.reserved bit0 = host_halted flag.
            _reply(cl, STATUS_OK, struct.pack("<II", dhcsr, dfsr), reserved=1)
        elif kind == "timeout":
            _reply(cl, STATUS_TIMEOUT, struct.pack("<I", dhcsr))
        else:  # "eof": client gone or framing violation
            raise _Disconnect()

    elif opcode == OP_READ_REG:
        _require_halted(cm)
        regsel = args[0]
        if regsel > REGSEL_MAX:
            raise ValueError("regsel")
        _reply(cl, STATUS_OK, struct.pack("<I", cm.read_core_reg(regsel)))

    elif opcode == OP_WRITE_REG:
        _require_halted(cm)
        regsel, value = struct.unpack_from("<BI", args, 0)
        if regsel > REGSEL_MAX:
            raise ValueError("regsel")
        cm.write_core_reg(regsel, value)
        _reply(cl, STATUS_OK)

    elif opcode == OP_READ_REGS:
        _require_halted(cm)
        (mask,) = struct.unpack_from("<I", args, 0)
        # Any set bit above REGSEL_MAX is a bad request; validate before any work
        # so we never partially reply.
        if mask >> (REGSEL_MAX + 1):
            raise ValueError("regsel mask")
        out = bytearray()
        for regsel in range(REGSEL_MAX + 1):
            if mask & (1 << regsel):
                out += struct.pack("<I", cm.read_core_reg(regsel))
        _reply(cl, STATUS_OK, bytes(out))

    elif opcode == OP_WRITE_REGS:
        _require_halted(cm)
        (mask,) = struct.unpack_from("<I", args, 0)
        if mask >> (REGSEL_MAX + 1):
            raise ValueError("regsel mask")
        off = 4
        for regsel in range(REGSEL_MAX + 1):
            if mask & (1 << regsel):
                (value,) = struct.unpack_from("<I", args, off)
                cm.write_core_reg(regsel, value)
                off += 4
        _reply(cl, STATUS_OK)

    elif opcode == OP_READ_MEM:
        _require_halted(cm)
        addr, length = struct.unpack_from("<II", args, 0)
        if length > MAX_DATA:
            raise ValueError("len")
        _reply(cl, STATUS_OK, _read_mem(ap, addr, length))

    elif opcode == OP_WRITE_MEM:
        _require_halted(cm)
        addr, length = struct.unpack_from("<II", args, 0)
        if length > MAX_DATA:
            raise ValueError("len")
        data = args[8:8 + length]
        if len(data) != length:
            raise ValueError("short data")
        # Fatal flaw 1.3: reject any write touching the flash region. NVMC is
        # bit-clear-only with page-erase semantics; a sub-word write would
        # silently corrupt. The host's region policy is the primary guard; this
        # is the backstop. A write wholly at/above FLASH_TOP is allowed.
        if addr < FLASH_TOP:
            _reply(cl, STATUS_WRPROT)
        else:
            _write_mem(ap, addr, data)
            _reply(cl, STATUS_OK)

    elif opcode == OP_BP_SET:
        (addr,) = struct.unpack_from("<I", args, 0)
        slot = fpb.set_breakpoint(addr)       # TransferError -> STATUS_XFER
        _reply(cl, STATUS_OK, struct.pack("<B", slot & 0xFF))

    elif opcode == OP_BP_CLEAR:
        (addr,) = struct.unpack_from("<I", args, 0)
        fpb.clear_breakpoint(addr)
        _reply(cl, STATUS_OK)

    elif opcode == OP_BP_CLEAR_ALL:
        fpb.clear_all()
        _reply(cl, STATUS_OK)

    elif opcode == OP_WATCH_SET:
        addr, length, func = struct.unpack_from("<IBB", args, 0)
        slot = dwt.set_watchpoint(addr, length, func)   # TransferError -> STATUS_XFER
        _reply(cl, STATUS_OK, struct.pack("<B", slot & 0xFF))

    elif opcode == OP_WATCH_CLEAR:
        addr, length, func = struct.unpack_from("<IBB", args, 0)
        dwt.clear_watchpoint(addr, length, func)
        _reply(cl, STATUS_OK)

    else:
        # Unknown / reserved opcode.
        _reply(cl, STATUS_BADOP)


class _Disconnect(Exception):
    # Internal signal to break the serve loop (RESUME_WAIT saw EOF / framing).
    pass


def serve(dp, ap, cm, fpb, dwt, port=3335):
    # Own the socket lifecycle: bind, accept one client, frame loop, close on all
    # paths. The dp/ap/cm/fpb/dwt session is created once by ops.gdb_serve and
    # passed in (the PIO SM is not recreated, avoiding PIO instruction-memory
    # leaks under the persistent REPL). The core is left in whatever state the
    # last command set; this server never auto-resumes.
    # AF_INET6 + "::" = dual-stack (v4+v6) via modlwip's listen() promotion.
    srv = socket.socket(socket.AF_INET6)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    cl = None
    try:
        srv.bind(("::", port))
        srv.listen(1)
        # Bound accept() so a session that never connects does not block the REPL
        # forever; a timeout here returns cleanly with no client.
        srv.settimeout(ACCEPT_TIMEOUT_S)
        try:
            cl, _ = srv.accept()
        except OSError:
            return {"ok": True, "port": port, "accepted": False}
        cl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Bound per-frame reads so a dead host (Wi-Fi half-open, gdb killed
        # without a FIN) is detected as EOF instead of wedging readinto.
        cl.settimeout(FRAME_TIMEOUT_S)
        while True:
            hdr = _read_exact(cl, 4)
            if hdr is None:
                break
            opcode, flags, arg_len = struct.unpack("<BBH", hdr)
            if arg_len > MAX_DATA:
                break                       # framing violation -> drop conn
            args = _read_exact(cl, arg_len) if arg_len else b""
            if arg_len and args is None:
                break
            try:
                _dispatch(cl, opcode, args, dp, ap, cm, fpb, dwt)
            except _Disconnect:
                break
            except _NotHalted:
                _reply(cl, STATUS_NOTHALTED)
            except swd_dap.TransferError:
                _reply(cl, STATUS_XFER)
            except (ValueError, IndexError):
                # MicroPython's struct raises ValueError on a short buffer; a
                # bad regsel raises ValueError; both map to bad-args.
                _reply(cl, STATUS_BADARGS)
    finally:
        if cl is not None:
            cl.close()
        srv.close()
    return {"ok": True, "port": port}
