# High-level on-pod debug operations: the entry points the host `pod` tool drives
# over the REPL (USB-CDC in development, the Wi-Fi socket REPL in deployment).
#
# A single DebugPort / MEM-AP / flasher is created lazily and held in module
# globals for the VM lifetime, so repeated host commands reuse it instead of
# re-creating PIO state machines (re-creating leaks PIO instruction memory under
# a persistent REPL). Flashing reads its image from a pod-side file in bounded
# chunks, so the whole image is never held in pod RAM; the host transfers the
# image with a normal file copy and never streams target contents back unless it
# asks for an explicit dump.

import struct
import time

from . import swd_dap, flash_nrf52

_dp = None
_ap = None
_cm = None
_flash = None
_fpb = None


def _ensure(clkdiv=8):
    global _dp, _ap, _cm, _flash, _fpb
    if _dp is None:
        _dp = swd_dap.DebugPort(swdio=14, swclk=15, sm_id=4, clkdiv=clkdiv)
        _ap = swd_dap.MEMAP(_dp)
        _cm = swd_dap.CortexM(_ap)
        _flash = flash_nrf52.NRF52Flash(_ap, _cm)
        _fpb = swd_dap.FPB(_ap)
    _dp.connect()
    return _dp, _ap, _cm, _flash


def info(clkdiv=8):
    dp, ap, cm, fl = _ensure(clkdiv)
    return {
        "dpidr": dp.dpidr,
        "cpuid": cm.cpuid(),
        "part": ap.read32(0x10000100),
        "flash_kb": ap.read32(0x10000110),
        "ram_kb": ap.read32(0x1000010C),
    }


def flash_file(addr, path, clkdiv=8, verify=True, chunk_words=256):
    # Program target flash from a pod-side file, bounded memory, then resume.
    dp, ap, cm, fl = _ensure(clkdiv)
    f = open(path, "rb")
    try:
        t0 = time.ticks_ms()
        n = fl.program_file(addr, f, erase=True, verify=verify,
                            chunk_words=chunk_words)
        dt = time.ticks_diff(time.ticks_ms(), t0)
    finally:
        f.close()
    cm.resume()
    return {"ok": True, "addr": addr, "bytes": n, "ms": dt}


def flash_stream(addr, total_len, port=3333, chunk=4096, clkdiv=8, verify=True):
    # Flash a DUT image streamed over TCP straight into pod RAM, no filesystem.
    # The image is received into a RAM buffer a chunk at a time and programmed
    # to the DUT over SWD; the whole image is never resident (only one chunk
    # plus the socket receive queue). The Wi-Fi receive and the SWD write
    # overlap naturally: while the CPU programs the current chunk, lwIP fills
    # the next one into the socket receive buffer in the background, so the
    # following recv returns immediately. (An explicit two-buffer split with the
    # SWD write on core 1 was tried; MicroPython threading + PIO from the second
    # core deadlocked, and since Wi-Fi is much faster than SWD the gain was
    # marginal, so this single-buffer-plus-lwIP form is used.)
    import socket

    dp, ap, cm, fl = _ensure(clkdiv)
    fl.prepare()
    page = fl.page_size
    err = None

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    cl = None
    try:
        srv.bind(("0.0.0.0", port))
        srv.listen(1)
        cl, _ = srv.accept()

        # erase the covered region once (host is connected; its stream buffers
        # in TCP while we erase)
        start = addr & ~(page - 1)
        span = ((addr & (page - 1)) + total_len + page - 1) & ~(page - 1)
        fl.erase_range(start, span)

        buf = bytearray(chunk)
        mv = memoryview(buf)
        a = addr
        left = total_len
        while left > 0:
            n = chunk if left > chunk else left
            got = 0
            while got < n:
                r = cl.readinto(mv[got:n])
                if not r:
                    break
                got += r
            if got == 0:
                err = "short read"
                break
            fl.program(a, bytes(mv[:got]), erase=False, verify=verify)
            a += got
            left -= got
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        err = repr(e)
    finally:
        if cl is not None:
            try:
                cl.send(b"\x01" if err is None else b"\x00")
            except Exception:
                pass
            cl.close()
        srv.close()
        cm.resume()
    return {"ok": err is None, "addr": addr, "bytes": total_len, "err": err}


def dump_stream(addr, length, port=3334, clkdiv=8):
    # Explicit read of target memory streamed to the host over TCP, no
    # filesystem (the reverse of flash_stream). Reads the DUT in bounded
    # 256-word blocks and sends each over the socket; nothing is written to the
    # pod FS and nothing is held whole in pod RAM. This is the only path that
    # returns target contents, and only when explicitly invoked.
    import socket
    import struct

    dp, ap, cm, fl = _ensure(clkdiv)
    if not cm.is_halted():
        cm.halt()
    err = None
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    cl = None
    try:
        srv.bind(("0.0.0.0", port))
        srv.listen(1)
        cl, _ = srv.accept()
        a = addr
        left = length
        while left > 0:
            nwords = 256 if left >= 1024 else (left + 3) // 4
            buf = b"".join(struct.pack("<I", w)
                           for w in ap.read_block32(a, nwords))
            if len(buf) > left:
                buf = buf[:left]
            cl.sendall(buf)
            a += len(buf)
            left -= len(buf)
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        err = repr(e)
    finally:
        if cl is not None:
            cl.close()
        srv.close()
        cm.resume()
    return {"ok": err is None, "addr": addr, "bytes": length, "err": err}


def reset(mode="sysreset", clkdiv=8):
    dp, ap, cm, fl = _ensure(clkdiv)
    if mode == "halt":
        cm.reset_and_halt()
    else:
        cm.sysreset()
        cm.resume()
    return {"ok": True, "mode": mode}


def gdb_serve(port=3335, clkdiv=8, reset_halt=True):
    # Bring the DP up ONCE, halt, and hand the live session to the binary debug
    # server (dbgsrv). The dbgsrv loop runs against this session and never calls
    # _ensure/connect again: a per-command _dp.connect() would line-reset the DP
    # mid-session. _ensure's per-call connect() is tolerable only because this
    # entry point calls it exactly once at session start.
    #
    # Blocks for the session, then returns a result dict the host scrapes from
    # the REPL stdout (as flash_stream does).
    #
    # The finally puts the DUT in a defined state on EVERY exit path (clean
    # detach, socket drop, EOF, exception): the FPB comparators are unconditionally
    # disabled and cleared and the core is resumed. Without this, an abnormal
    # teardown (gdb killed, Wi-Fi drop) would leave the core halted with live FPB
    # comparators in flash, which a later flash_stream (it never calls FPB.init)
    # would inherit and spuriously trap on. The host 'D' detach still issues its
    # own clear/resume; this is the backstop for the paths 'D' never reaches.
    from . import dbgsrv
    dp, ap, cm, fl = _ensure(clkdiv)
    if reset_halt:
        cm.reset_and_halt()
    elif not cm.is_halted():
        cm.halt()
    err = None
    try:
        dbgsrv.serve(dp, ap, cm, _fpb, port=port)
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        err = repr(e)
    finally:
        # Defined post-session DUT state, independent of how the session ended.
        try:
            if _fpb is not None:
                _fpb.disable()
                _fpb.clear_all()
        except Exception:
            pass
        try:
            cm.resume()
        except Exception:
            pass
    return {"ok": err is None, "port": port, "err": err}


def close():
    # Resume the target and drop the cached session (next call re-creates it).
    global _dp, _ap, _cm, _flash, _fpb
    try:
        if _cm is not None:
            _cm.resume()
    except Exception:
        pass
    _dp = None
    _ap = None
    _cm = None
    _flash = None
    _fpb = None
    return {"ok": True}
