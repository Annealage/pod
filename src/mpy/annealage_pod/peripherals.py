# Curated builtin-`machine` peripheral helpers, driven from the host REPL.
#
# A thin layer over MicroPython's machine module so the host `pod` tool (and
# agents) can stand up common DUT-facing peripherals without hand-writing the
# lifecycle for the cases that need it. This is a convenience layer, not a gate:
# anything not covered here is just exec'd directly against the REPL (`pod exec`
# / the dut_exec MCP tool), which already reaches the whole machine module.
#
# Instances that must outlive a single REPL call - an I2C target services the
# bus autonomously in the background - are held in a module-level registry so a
# later call can read, update, or release them. Under a persistent REPL the
# module is imported once and `_INST` survives across calls, which is exactly
# what keeps a target alive between `pod` commands.
#
# On the RP2350 `machine.I2CTarget` in mem-mode is the hardware realisation of
# the ESP32-S3 `slaveio` register-table responder (annealage_pod.slave): the
# backing buffer is the register file the controller reads and writes, with
# address-match / ACK / clock-stretch / repeated-start all handled in silicon.

import machine

from . import _rp2_pinmap as _pins

# name -> (obj, extra). obj has .deinit(); extra carries helper state (e.g. the
# I2C target's backing buffer) or None.
_INST = {}


def _drop(name):
    rec = _INST.pop(name, None)
    if rec is None:
        return False
    try:
        rec[0].deinit()
    except Exception:
        pass
    return True


def release(name="*"):
    """Release one named instance, or every instance when name == '*'."""
    if name == "*":
        names = list(_INST)
        for n in names:
            _drop(n)
        return {"ok": True, "released": names}
    return {"ok": True, "released": [name] if _drop(name) else []}


def instances():
    """List the live named peripheral instances."""
    return {"ok": True, "instances": list(_INST)}


# -- I2C target (hardware machine.I2CTarget, mem-backed register file) --------


def i2c_target(addr=0x42, regs=None, bus=_pins.I2C_TARGET_BUS,
               scl=_pins.I2C_TARGET_SCL, sda=_pins.I2C_TARGET_SDA,
               size=256, name="i2c_target"):
    """Bring up a persistent hardware I2C target with a mem-backed register file.

    The controller reads and writes the backing buffer as an auto-addressed
    register file (e.g. readfrom_mem(addr, off, n) returns buf[off:off+n]). The
    buffer persists, so each side sees the other's writes. `regs` is an optional
    iterable of initial byte values from offset 0. Re-calling with the same
    `name` replaces the existing target.

    Pin defaults are the bench wiring: I2C1, SCL=GP11, SDA=GP10.
    """
    _drop(name)
    buf = bytearray(size)
    if regs:
        for i, b in enumerate(regs):
            if i < size:
                buf[i] = b & 0xFF
    tgt = machine.I2CTarget(bus, addr, scl=machine.Pin(scl),
                            sda=machine.Pin(sda), mem=buf)
    _INST[name] = (tgt, buf)
    return {"ok": True, "name": name, "addr": addr, "bus": bus,
            "scl": scl, "sda": sda, "size": size}


def i2c_target_regs(off=0, length=None, write=None, name="i2c_target"):
    """Read or write the I2C target's register buffer from the pod side.

    With `write` (an iterable of bytes) set, write it at `off` first; then return
    the window buf[off:off+length] (length defaults to the rest of the buffer).
    Lets a test seed registers or inspect what the controller wrote.
    """
    rec = _INST.get(name)
    if rec is None or rec[1] is None:
        return {"ok": False, "err": "no such i2c target: %s" % name}
    buf = rec[1]
    if write is not None:
        for i, b in enumerate(write):
            if off + i < len(buf):
                buf[off + i] = b & 0xFF
    if length is None:
        length = len(buf) - off
    return {"ok": True, "regs": list(buf[off:off + length])}


# -- SPI target (PIO, stream/counter or regfile personality) -----------------


def spi_target(mode=0, bits=8, miso=_pins.DUT_SPI_MISO, mosi=_pins.DUT_SPI_MOSI,
               sck=_pins.DUT_SPI_SCK, cs=_pins.DUT_SPI_CS, size=1024,
               personality="stream", table_size=256, name="spi_target"):
    """Bring up a persistent PIO SPI target on the pod.

    The pod becomes the SPI peripheral, SPI mode 0-3, 8-bit, MSB-first.
    `personality='stream'` (default): MISO replays a 0..255 counter for any
    transfer length (unbounded, no growing buffer), MOSI is captured into a
    `size`-byte overwrite ring (rounded up to a power of two).
    `personality='regfile'`: a [reg_ptr][data...] register-file responder
    over `table_size` bytes each way (see spi_target_regs and
    docs/esp32-s3/design/slaveio.md section 4); `size` still bounds the MOSI
    capture ring, grown automatically to retain a full transaction.
    `spi_target_status` reads the byte count, captured ring, and (regfile)
    the register-pointer state. Re-calling with the same `name` replaces the
    existing target. Bench default: MISO=GP16, MOSI=GP19, SCK=GP18, CS=GP17
    (SUGGESTED-untested; see _rp2_pinmap.py).
    """
    _drop(name)
    from .spi_target import SpiTarget   # lazy import: keeps rp2/DMA off the
                                         # peripherals import path for callers
                                         # that never touch the SPI target
    try:
        tgt = SpiTarget(miso=miso, mosi=mosi, sck=sck, cs=cs, mode=mode,
                        bits=bits, size=size, personality=personality,
                        table_size=table_size, name=name)
    except Exception as e:
        # Validation (mode/bits/size/personality/table_size out of range) and
        # a PIO0 PioConflict surface as a result, not a raised exception, so
        # the host CLI/MCP report a clean error instead of a REPL traceback.
        return {"ok": False, "name": name, "err": repr(e)}
    _INST[name] = (tgt, None)
    return {"ok": True, "name": name, "mode": mode, "bits": bits,
            "personality": personality, "miso": miso, "mosi": mosi, "sck": sck,
            "cs": cs, "size": tgt.size, "table_size": tgt.table_size}


def spi_target_status(name="spi_target"):
    """Read the pod SPI target's status: byte count, transfer count, captured ring."""
    rec = _INST.get(name)
    if rec is None:
        return {"ok": False, "err": "no such spi target: %s" % name}
    d = rec[0].status()
    d["name"] = name
    return d


def spi_target_regs(off=0, length=None, write=None, table="read",
                    name="spi_target"):
    """Read or write the pod SPI target's regfile backing table from the pod side.

    `table` is 'read' (served on MISO) or 'write' (filled from MOSI). With
    `write` set (an iterable of bytes), write it at `off` first; then return
    the window [off:off+length] (length defaults to the rest of the table).
    Only valid for a `personality='regfile'` instance; mirrors i2c_target_regs.
    """
    rec = _INST.get(name)
    if rec is None:
        return {"ok": False, "err": "no such spi target: %s" % name}
    try:
        regs = rec[0].regs(table, off=off, length=length, write=write)
    except Exception as e:
        return {"ok": False, "err": repr(e)}
    return {"ok": True, "table": table, "regs": regs}


# -- stateless convenience one-liners (GPIO, ADC) ----------------------------


def gpio(pin, value=None, mode="out", pull=None):
    """Read or drive a GPIO.

    With `value` set, drive the pin as an output; otherwise read it as an input.
    `pull` is 'up', 'down', or None (input only). Returns the resulting level.
    """
    if value is None:
        pulls = {"up": machine.Pin.PULL_UP, "down": machine.Pin.PULL_DOWN,
                 None: None}
        p = machine.Pin(pin, machine.Pin.IN, pulls.get(pull))
        return {"ok": True, "pin": pin, "value": p.value()}
    p = machine.Pin(pin, machine.Pin.OUT)
    p.value(1 if value else 0)
    return {"ok": True, "pin": pin, "value": p.value()}


def adc(pin):
    """Sample an ADC channel. Returns the raw 16-bit reading and a 3.3V-ref volts."""
    a = machine.ADC(machine.Pin(pin))
    raw = a.read_u16()
    return {"ok": True, "pin": pin, "u16": raw, "volts": raw * 3.3 / 65535}
