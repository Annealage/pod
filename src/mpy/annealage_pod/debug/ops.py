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

from . import swd_dap, swd_pio, flash_nrf52, netutil, dbgsrv
from .. import _rp2_pinmap

_dp = None
_ap = None
_cm = None
_flash = None
_fpb = None
_dwt = None
_flm = None


# Selectable flash backend. "native" is the per-family NVM path
# (flash_nrf52.NRF52Flash), driven directly through the NVMC; "flm" is the
# generic CMSIS flash-algorithm runner (flm.FLMFlasher + flm_<part>.FLASH_ALGO)
# that runs a standard algorithm blob on the target and so generalises to any
# chip with a CMSIS pack. Both are hardware-validated (nRF52840, 2026-07-09:
# program + full-chip erase). Default per operation: the flat-binary flash_stream
# path defaults to "native" (nRF fast-path, preserves prior behaviour), while the
# ELF flash path and mass-erase (erase_all) default to "flm" so they generalise
# across pack targets. _select_loader keeps the choice out of the hot path.
def _flm_algo():
    # The CMSIS flash algorithm for the connected DUT. nRF52840 is the only
    # extracted pack present; a real multi-DUT pod would pick this from the
    # discovered part id. Imported lazily so the native path never loads it.
    from . import flm_nrf52840
    return flm_nrf52840.FLASH_ALGO


def _ensure(clkdiv=swd_pio.DEFAULT_CLKDIV):
    global _dp, _ap, _cm, _flash, _fpb, _dwt, _flm
    if _dp is not None and _dp.swd.clkdiv != clkdiv:
        # A prior call built the SWD stack at a different clock. A running PIO
        # state machine's divisor can't be retuned in place, so drop the whole
        # cached stack and rebuild below at the requested clkdiv - otherwise the
        # clkdiv argument would be silently ignored on a warm stack. The DUT's
        # halt / breakpoint / watchpoint state lives in the target's debug
        # hardware (DHCSR / FPB / DWT), not in these Python objects, so it
        # survives the rebuild; unlike close() we deliberately do NOT resume the
        # core here. Release only the SWD PIO program (the arbiter claim is
        # re-taken idempotently below).
        try:
            _dp.swd.release()
        except Exception:
            pass
        _dp = _ap = _cm = _flash = _fpb = _dwt = _flm = None
    if _dp is None:
        from . import pio_arbiter
        pio_arbiter.claim("swd", 1, 0)   # PIO1 sm0 (PIO2 = CYW43; PIO0 = SPI/LA)
        _dp = swd_dap.DebugPort(swdio=_rp2_pinmap.SWD_SWDIO,
                                swclk=_rp2_pinmap.SWD_SWCLK, sm_id=4, clkdiv=clkdiv)
        _ap = swd_dap.MEMAP(_dp)
        _cm = swd_dap.CortexM(_ap)
        _flash = flash_nrf52.NRF52Flash(_ap, _cm)
        _fpb = swd_dap.FPB(_ap)
        _dwt = swd_dap.DWT(_ap)
    _dp.connect()
    return _dp, _ap, _cm, _flash


def _select_loader(loader):
    # Return the flash backend for this operation. "native" (default) is the
    # cached NRF52Flash created by _ensure; "flm" lazily builds and caches an
    # FLMFlasher over the same MEM-AP / CortexM so repeated FLM flashes reuse
    # the loaded algorithm session. _ensure must have run first.
    global _flm
    if loader == "native":
        return _flash
    if loader == "flm":
        from . import flm
        if _flm is None:
            _flm = flm.FLMFlasher(_ap, _cm, _flm_algo())
        return _flm
    raise ValueError("unknown loader %r (use 'native' or 'flm')" % loader)


def _flm_erase_range(flm_fl, addr, length):
    # Erase the page-aligned region covering [addr, addr+length) by sectors,
    # using one Init(erase)/UnInit bracket. Mirrors NRF52Flash.erase_range so
    # the FLM flash_file / flash_stream paths erase once up front and then
    # program with erase=False, matching the native path's behaviour.
    page = flm_fl.page_size
    flm_fl.load()
    flm_fl.init(1)                                 # operation 1 = erase
    try:
        p = addr & ~(page - 1)
        end = addr + length
        while p < end:
            flm_fl.erase_sector(p)
            p += page
    finally:
        flm_fl.uninit(1)


def _flm_program_file(flm_fl, addr, fileobj, verify):
    # Program target flash from an open binary file through the FLM backend,
    # holding only one page in RAM at a time. Erases the covered region once,
    # then programs page by page with erase=False. Returns bytes written.
    if addr % 4:
        raise ValueError("program addr not word-aligned")
    try:
        fileobj.seek(0, 2)
        length = fileobj.tell()
        fileobj.seek(0)
    except (OSError, AttributeError):
        raise ValueError("length required for non-seekable file")
    _flm_erase_range(flm_fl, addr, length)
    page = flm_fl.page_size
    n = 0
    while True:
        buf = fileobj.read(page)
        if not buf:
            break
        flm_fl.program(addr + n, buf, erase=False, verify=verify)
        n += len(buf)
    return n


def info(clkdiv=swd_pio.DEFAULT_CLKDIV):
    dp, ap, cm, fl = _ensure(clkdiv)
    return {
        "dpidr": dp.dpidr,
        "cpuid": cm.cpuid(),
        "part": ap.read32(0x10000100),
        "flash_kb": ap.read32(0x10000110),
        "ram_kb": ap.read32(0x1000010C),
    }


def discover(clkdiv=swd_pio.DEFAULT_CLKDIV):
    # Identify the connected DUT with architecturally-generic ADIv5/Cortex-M
    # reads only - the DP IDCODE, the MEM-AP IDR, the Cortex-M CPUID, and the
    # debug ROM-table base. No core halt and no family-specific memory probe
    # (unlike info(), whose part/flash/ram reads are nRF52 FICR / RP2350 SYSINFO
    # and only valid per family). The host decodes core/designer/family from
    # these raw ids. Returns {"ok": False, "err": ...} if SWD does not connect.
    try:
        dp, ap, cm, fl = _ensure(clkdiv)
        return {
            "ok": True,
            "dpidr": dp.dpidr,
            "ap_idr": ap.idr(),
            "cpuid": cm.cpuid(),
            "rom_base": ap.read_debug_base(),
        }
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        return {"ok": False, "err": repr(e)}


def flash_file(addr, path, clkdiv=swd_pio.DEFAULT_CLKDIV, verify=True, chunk_words=256,
               loader="native"):
    # Program target flash from a pod-side file, bounded memory, then resume.
    # loader: "native" (default, validated NVMC path) or "flm" (generic CMSIS
    # algorithm). See _select_loader.
    dp, ap, cm, fl = _ensure(clkdiv)
    fl = _select_loader(loader)
    f = open(path, "rb")
    try:
        t0 = time.ticks_ms()
        if loader == "native":
            n = fl.program_file(addr, f, erase=True, verify=verify,
                                chunk_words=chunk_words)
        else:
            # FLMFlasher has no file/streaming API. Seek to size, erase the
            # whole covered region once (so a multi-page image is not partially
            # erased), then feed the image in page-sized chunks with erase=False
            # so the whole file is never resident in pod RAM.
            n = _flm_program_file(fl, addr, f, verify)
        dt = time.ticks_diff(time.ticks_ms(), t0)
    finally:
        f.close()
    cm.resume()
    return {"ok": True, "addr": addr, "bytes": n, "ms": dt, "loader": loader}


def flash_stream(addr, total_len, port=3333, chunk=4096, clkdiv=swd_pio.DEFAULT_CLKDIV, verify=True,
                 loader="native"):
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
    fl = _select_loader(loader)
    if loader == "native":
        fl.prepare()
    page = fl.page_size
    err = None

    # AF_INET6 + "::" = dual-stack (v4+v6) via modlwip's listen() promotion.
    srv = socket.socket(socket.AF_INET6)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    cl = None
    try:
        srv.bind(("::", port))
        srv.listen(1)
        cl, _ = netutil.accept(srv, 30)   # bounded + Ctrl-C-interruptible

        # erase the covered region once (host is connected; its stream buffers
        # in TCP while we erase)
        start = addr & ~(page - 1)
        span = ((addr & (page - 1)) + total_len + page - 1) & ~(page - 1)
        if loader == "native":
            fl.erase_range(start, span)
        else:
            _flm_erase_range(fl, start, span)

        buf = bytearray(chunk)
        mv = memoryview(buf)
        a = addr
        left = total_len
        while left > 0:
            n = chunk if left > chunk else left
            got = netutil.recv_into(cl, mv[:n])
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
                netutil.send_all(cl, b"\x01" if err is None else b"\x00", timeout_s=5)
            except Exception:
                pass
            cl.close()
        srv.close()
        cm.resume()
    return {"ok": err is None, "addr": addr, "bytes": total_len, "err": err,
            "loader": loader}


def erase_all(clkdiv=swd_pio.DEFAULT_CLKDIV, loader="flm"):
    # Erase the entire DUT flash, returning timing and loader info. loader="flm"
    # runs the generic CMSIS FLMFlasher.erase_all() (halts core, runs the
    # algorithm blob, resumes); loader="native" uses NRF52Flash.mass_erase()
    # directly through the NVMC. The core is always resumed in the finally.
    dp, ap, cm, fl = _ensure(clkdiv)
    err = None
    t0 = time.ticks_ms()
    try:
        fl = _select_loader(loader)
        if loader == "native":
            fl.prepare()     # halt the core
            fl.mass_erase()
        else:
            if not cm.is_halted():
                cm.halt()
            fl.erase_all()
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        err = repr(e)
    finally:
        try:
            cm.resume()
        except Exception:
            pass
    dt = time.ticks_diff(time.ticks_ms(), t0)
    return {"ok": err is None, "ms": dt, "loader": loader, "err": err}


def write_mem_stream(addr, total_len, port=3333, chunk=4096, clkdiv=swd_pio.DEFAULT_CLKDIV,
                     protect=None):
    # Write a raw byte stream received over TCP directly into target memory via
    # the MEM-AP. No erase, no flash involvement. Uses the same dual-stack
    # socket setup and \x01/\x00 status-byte protocol as flash_stream, and the
    # same protect check as write_mem (applied up front against the whole range).
    import socket

    # Protect check against the whole [addr, addr+total_len) range before
    # binding the socket (mirrors write_mem:387-402).
    end = addr + total_len
    if protect:
        for lo, hi in protect:
            if addr < hi and end > lo:
                return {"ok": False, "addr": addr, "bytes": 0,
                        "err": "addr 0x%08x..0x%08x overlaps a write-protected "
                               "range 0x%08x..0x%08x" % (addr, end, lo, hi)}
    elif addr < dbgsrv.FLASH_TOP:
        return {"ok": False, "addr": addr, "bytes": 0,
                "err": "addr 0x%08x is in the code/flash region "
                       "(< 0x%08x, the Cortex-M SRAM base) and is not "
                       "word-writable" % (addr, dbgsrv.FLASH_TOP)}

    dp, ap, cm, fl = _ensure(clkdiv)
    err = None

    # AF_INET6 + "::" = dual-stack (v4+v6) via modlwip's listen() promotion.
    srv = socket.socket(socket.AF_INET6)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    cl = None
    try:
        srv.bind(("::", port))
        srv.listen(1)
        cl, _ = netutil.accept(srv, 30)   # bounded + Ctrl-C-interruptible

        buf = bytearray(chunk)
        mv = memoryview(buf)
        a = addr
        left = total_len
        while left > 0:
            n = chunk if left > chunk else left
            got = netutil.recv_into(cl, mv[:n])
            if got == 0:
                err = "short read"
                break
            dbgsrv._write_mem(ap, a, bytes(mv[:got]))
            a += got
            left -= got
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        err = repr(e)
    finally:
        if cl is not None:
            try:
                netutil.send_all(cl, b"\x01" if err is None else b"\x00", timeout_s=5)
            except Exception:
                pass
            cl.close()
        srv.close()
        cm.resume()
    return {"ok": err is None, "addr": addr, "bytes": total_len, "err": err}


def dump_stream(addr, length, port=3334, clkdiv=swd_pio.DEFAULT_CLKDIV):
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
    # AF_INET6 + "::" = dual-stack (v4+v6) via modlwip's listen() promotion.
    srv = socket.socket(socket.AF_INET6)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    cl = None
    try:
        srv.bind(("::", port))
        srv.listen(1)
        cl, _ = netutil.accept(srv, 30)   # bounded + Ctrl-C-interruptible
        a = addr
        left = length
        while left > 0:
            nwords = 256 if left >= 1024 else (left + 3) // 4
            buf = b"".join(struct.pack("<I", w)
                           for w in ap.read_block32(a, nwords))
            if len(buf) > left:
                buf = buf[:left]
            netutil.send_all(cl, buf)   # EAGAIN-robust: raw sendall aborts under backpressure
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


def flash_crc(addr, length, clkdiv=swd_pio.DEFAULT_CLKDIV):
    # CRC32 of a DUT flash region, read over SWD - the end-to-end integrity check
    # the streaming program path lacks. flash_stream verifies each chunk it
    # programs, but cannot see a chunk lost mid-stream (Wi-Fi reset) or a flaky
    # per-chunk verify; re-reading the whole region and folding a CRC catches any
    # such hole. Reads in blocks via the MEM-AP so the region is never held whole
    # in pod RAM (mirrors dump_stream). The host compares this against the CRC of
    # the source image.
    import binascii
    import struct
    dp, ap, cm, fl = _ensure(clkdiv)
    if not cm.is_halted():
        cm.halt()
    err = None
    crc = 0
    try:
        a = addr
        left = length
        while left > 0:
            nwords = 256 if left >= 1024 else (left + 3) // 4
            buf = b"".join(struct.pack("<I", w)
                           for w in ap.read_block32(a, nwords))
            if len(buf) > left:
                buf = buf[:left]
            crc = binascii.crc32(buf, crc)
            a += len(buf)
            left -= len(buf)
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        err = repr(e)
    finally:
        cm.resume()
    return {"ok": err is None, "crc": crc & 0xFFFFFFFF, "addr": addr,
            "length": length, "err": err}


def reset(mode="sysreset", clkdiv=swd_pio.DEFAULT_CLKDIV):
    dp, ap, cm, fl = _ensure(clkdiv)
    if mode == "halt":
        cm.reset_and_halt()
    else:
        cm.sysreset()
        cm.resume()
    return {"ok": True, "mode": mode}


# -- Single-shot register / memory access over the SWD debug interface --------
# The peek/poke surface the host pod tool (and its MCP tools) drive over the
# REPL, distinct from gdb_serve: each call reuses the cached DP/AP/CM session,
# touches the target once, and returns - no TCP server, no gdb session, and (the
# key difference from gdb_serve) it never auto-resumes the core, so a halt holds
# across calls until resume() is called.
#
# Core registers are read/written through the debug DCRSR/DCRDR, which require a
# halted core; these helpers report STATUS-style {"ok": False} rather than
# halting behind the caller's back (mirrors dbgsrv's _require_halted). Memory
# goes over the MEM-AP and works whether the core runs or is halted - a live bus
# access (a read of a location the running core is mutating may be non-coherent;
# halt() first for a coherent snapshot). The aligned/unaligned memory access and
# the flash-region write guard are reused from dbgsrv so there is one
# implementation of each.

def halt(clkdiv=swd_pio.DEFAULT_CLKDIV):
    # Halt the core where it is and hold it (no auto-resume). Required before a
    # register read/write; also freezes the DUT (incl. its USB) for the duration.
    try:
        dp, ap, cm, fl = _ensure(clkdiv)
        cm.halt()
        return {"ok": True, "halted": True, "dhcsr": cm.read_dhcsr()}
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        return {"ok": False, "err": repr(e)}


def resume(clkdiv=swd_pio.DEFAULT_CLKDIV):
    # Resume a core halted by halt() / reset(mode="halt").
    try:
        dp, ap, cm, fl = _ensure(clkdiv)
        cm.resume()
        return {"ok": True, "halted": False}
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        return {"ok": False, "err": repr(e)}


def read_reg(regsel, clkdiv=swd_pio.DEFAULT_CLKDIV):
    # Validate the (cheap) argument before _ensure, which line-resets the DP.
    if regsel < 0 or regsel > dbgsrv.REGSEL_MAX:
        return {"ok": False, "err": "regsel out of range 0..%d" % dbgsrv.REGSEL_MAX}
    try:
        dp, ap, cm, fl = _ensure(clkdiv)
        if not cm.is_halted():
            return {"ok": False, "err": "core is running; halt() it first "
                    "(registers need a halted core)"}
        return {"ok": True, "regsel": regsel, "value": cm.read_core_reg(regsel)}
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        return {"ok": False, "err": repr(e)}


def write_reg(regsel, value, clkdiv=swd_pio.DEFAULT_CLKDIV):
    if regsel < 0 or regsel > dbgsrv.REGSEL_MAX:
        return {"ok": False, "err": "regsel out of range 0..%d" % dbgsrv.REGSEL_MAX}
    value &= 0xFFFFFFFF
    try:
        dp, ap, cm, fl = _ensure(clkdiv)
        if not cm.is_halted():
            return {"ok": False, "err": "core is running; halt() it first "
                    "(registers need a halted core)"}
        cm.write_core_reg(regsel, value)
        return {"ok": True, "regsel": regsel, "value": value}
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        return {"ok": False, "err": repr(e)}


def read_mem(addr, length, clkdiv=swd_pio.DEFAULT_CLKDIV):
    import binascii
    if length < 0 or length > dbgsrv.MAX_DATA:
        return {"ok": False, "err": "length out of range 0..%d "
                "(use read_dut/dump_stream for bulk)" % dbgsrv.MAX_DATA}
    try:
        dp, ap, cm, fl = _ensure(clkdiv)
        data = dbgsrv._read_mem(ap, addr, length)
        return {"ok": True, "addr": addr, "length": length,
                "hex": binascii.hexlify(data).decode()}
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        return {"ok": False, "err": repr(e)}


def write_mem(addr, data_hex, protect=None, clkdiv=swd_pio.DEFAULT_CLKDIV):
    import binascii
    try:
        data = binascii.unhexlify(data_hex)
    except ValueError as e:
        return {"ok": False, "err": "bad hex data: %r" % e}
    if len(data) > dbgsrv.MAX_DATA:
        return {"ok": False, "err": "data exceeds %d bytes" % dbgsrv.MAX_DATA}
    # `protect` is the host's authoritative set of write-protected [lo, hi)
    # ranges (the declared DUT flash geometry + the Cortex-M code-region floor).
    # Without it (a direct REPL caller), fall back to the coarse code-region
    # backstop: everything below the architectural Cortex-M SRAM base is
    # flash/ROM and not word-writable.
    end = addr + len(data)
    if protect:
        for lo, hi in protect:
            if addr < hi and end > lo:
                return {"ok": False, "err": "addr 0x%08x..0x%08x overlaps a "
                        "write-protected range 0x%08x..0x%08x"
                        % (addr, end, lo, hi)}
    elif addr < dbgsrv.FLASH_TOP:
        return {"ok": False, "err": "addr 0x%08x is in the code/flash region "
                "(< 0x%08x, the Cortex-M SRAM base) and is not word-writable"
                % (addr, dbgsrv.FLASH_TOP)}
    try:
        dp, ap, cm, fl = _ensure(clkdiv)
        dbgsrv._write_mem(ap, addr, data)
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        return {"ok": False, "err": repr(e)}
    return {"ok": True, "addr": addr, "length": len(data)}


def gdb_serve(port=3335, clkdiv=swd_pio.DEFAULT_CLKDIV, reset_halt=True):
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
    # detach, socket drop, EOF, exception): the FPB comparators and the DWT
    # data watchpoints are unconditionally disabled and cleared and the core is
    # resumed. Without this, an abnormal teardown (gdb killed, Wi-Fi drop) would
    # leave the core halted with live FPB comparators in flash or DWT
    # comparators armed, which a later flash_stream (it never inits the FPB/DWT)
    # would inherit and spuriously trap on. The host 'D' detach still issues its
    # own clear/resume; this is the backstop for the paths 'D' never reaches.
    dp, ap, cm, fl = _ensure(clkdiv)
    if reset_halt:
        cm.reset_and_halt()
    elif not cm.is_halted():
        cm.halt()
    err = None
    try:
        dbgsrv.serve(dp, ap, cm, _fpb, _dwt, port=port)
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
            if _dwt is not None:
                _dwt.disable()
        except Exception:
            pass
        try:
            cm.resume()
        except Exception:
            pass
    return {"ok": err is None, "port": port, "err": err}


def close():
    # Resume the target, fully release the SWD PIO (so PIO1 is reclaimable, e.g.
    # by the analyser swap), and drop the cached session (next call re-creates it).
    global _dp, _ap, _cm, _flash, _fpb, _dwt, _flm
    try:
        if _cm is not None:
            _cm.resume()
    except Exception:
        pass
    try:
        if _dp is not None:
            _dp.swd.release()
    except Exception:
        pass
    try:
        from . import pio_arbiter
        pio_arbiter.release("swd")
    except Exception:
        pass
    _dp = None
    _ap = None
    _cm = None
    _flash = None
    _fpb = None
    _dwt = None
    _flm = None
    return {"ok": True}


# -- Logic analyser (Track 2): capture DUT pins on PIO0 -----------------------
# The analyser runs on PIO0 (the free block; SWD is PIO1, CYW43 Wi-Fi is PIO2).
# It must never touch PIO2: building a state machine there while Wi-Fi is live
# corrupts the running CYW43 SM and hard-wedges the chip (the bug that made
# la_stream hang over Wi-Fi).
#
# la_capture / la_stream do NOT close the SWD session: PIO0 (LA) and PIO1 (SWD)
# are independent blocks and coexist (hardware-validated - all three of LA/SWD/
# Wi-Fi held at once, SWD reads unchanged across a capture), so a capture can run
# mid-debug-session without losing halt/breakpoint state. Call close() explicitly
# if you do want to tear the SWD session down (e.g. to free PIO1 or resume the DUT).

_la_last = None   # last capture buffer, kept for in-pod inspection


def la_capture(base_pin, width=1, rate=1000000, depth=8000, trigger=None,
               sm_id=1):
    # Capture into pod RAM and return a summary (the buffer is kept in _la_last
    # rather than shipped over the REPL). Use la_stream for the host path.
    #
    # Default PIO0 sm1 (not sm0): sm0 is the SPI target's default, so the LA and a
    # live SPI target coexist on PIO0 out of the box (per-SM arbiter + per-program
    # teardown). Any PIO0 SM works - the FIFO/DREQ addresses derive from sm_id.
    global _la_last
    from . import logic_analyser, pio_arbiter
    pio_arbiter.claim("la", sm_id // 4, sm_id % 4)
    a = logic_analyser.LogicAnalyser(base_pin, width=width, sm_id=sm_id)
    try:
        r = a.capture(rate, depth, trigger)
    finally:
        a.release()
        pio_arbiter.release("la")
    _la_last = r.pop("buf", None)
    return r


def la_stream(base_pin, width=1, rate=1000000, depth=8000, trigger=None,
              port=3336, sm_id=1, accept_timeout=20):
    # sm_id defaults to PIO0 sm1 so the LA coexists with the SPI target (sm0) on
    # PIO0 by default (see la_capture).
    # Capture and stream the raw packed words to the host over TCP, no pod FS.
    # Wire: a 16-byte little-endian header (words, width, clkdiv, complete) then
    # words*4 bytes of samples. The host computes rate = 150e6 / clkdiv.
    #
    # accept() is bounded by accept_timeout: a blocking lwIP accept cannot be
    # interrupted by Ctrl-C, so without a timeout a host that never connects
    # (wrong IP, Wi-Fi drop) would wedge the REPL until a power-cycle. On
    # timeout the capture is skipped and the call returns an error.
    import socket
    import struct as _struct
    from . import logic_analyser, pio_arbiter
    pio_arbiter.claim("la", sm_id // 4, sm_id % 4)
    a = logic_analyser.LogicAnalyser(base_pin, width=width, sm_id=sm_id)
    err = None
    r = None
    # AF_INET6 + "::" = dual-stack (v4+v6) via modlwip's listen() promotion.
    srv = socket.socket(socket.AF_INET6)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    cl = None
    try:
        srv.bind(("::", port))
        srv.listen(1)
        cl, _ = netutil.accept(srv, accept_timeout)   # bounded + Ctrl-C-interruptible
        # Bound the data phase too: a half-open / flaky Wi-Fi connection could
        # otherwise wedge sendall forever. A capture streams in well under this.
        cl.settimeout(accept_timeout)
        r = a.capture(rate, depth, trigger)
        cl.sendall(_struct.pack("<IIII", r["words"], r["width"], r["clkdiv"],
                                1 if r["complete"] else 0))
        cl.sendall(bytes(memoryview(r["buf"])))
    except Exception as e:  # noqa: BLE001 - return as a result, not a raise
        err = repr(e)
    finally:
        try:
            a.release()
        except Exception:
            pass
        pio_arbiter.release("la")
        if cl is not None:
            cl.close()
        srv.close()
    out = {"ok": err is None, "err": err, "port": port}
    if r is not None:
        out.update({"width": r["width"], "rate": r["rate"], "clkdiv": r["clkdiv"],
                    "depth": r["depth"], "words": r["words"],
                    "complete": r["complete"]})
    return out
