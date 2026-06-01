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


def _ensure(clkdiv=8):
    global _dp, _ap, _cm, _flash
    if _dp is None:
        _dp = swd_dap.DebugPort(swdio=14, swclk=15, sm_id=4, clkdiv=clkdiv)
        _ap = swd_dap.MEMAP(_dp)
        _cm = swd_dap.CortexM(_ap)
        _flash = flash_nrf52.NRF52Flash(_ap, _cm)
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


def dump_to_file(addr, length, path, clkdiv=8, chunk_words=256):
    # Explicit read of target memory to a pod-side file in bounded chunks; the
    # host copies the file back. This is the only path that returns target
    # contents, and only when explicitly invoked.
    dp, ap, cm, fl = _ensure(clkdiv)
    if not cm.is_halted():
        cm.halt()
    f = open(path, "wb")
    try:
        written = fl.dump(addr, length, f, chunk_words=chunk_words)
    finally:
        f.close()
    cm.resume()
    return {"ok": True, "addr": addr, "bytes": written}


def reset(mode="sysreset", clkdiv=8):
    dp, ap, cm, fl = _ensure(clkdiv)
    if mode == "halt":
        cm.reset_and_halt()
    else:
        cm.sysreset()
        cm.resume()
    return {"ok": True, "mode": mode}


def close():
    # Resume the target and drop the cached session (next call re-creates it).
    global _dp, _ap, _cm, _flash
    try:
        if _cm is not None:
            _cm.resume()
    except Exception:
        pass
    return {"ok": True}
