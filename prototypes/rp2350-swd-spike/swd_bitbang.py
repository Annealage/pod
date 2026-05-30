# SWD bit-bang spike for the Annealage Pod RP2350 pivot.
#
# Purpose: prove SWD wiring + line protocol on a pico2-w acting as the debugger,
# by reading the target's DPIDR (IDCODE). This is the throwaway protocol-proof
# step before the PIO port. Pure machine.Pin, no PIO, slow but easy to debug.
#
# Wiring (defaults below): pod GPIO -> DUT SWD header, plus common GND.
#   SWCLK = GP2  -> DUT SWCLK
#   SWDIO = GP3  -> DUT SWDIO
#   GND          -> DUT GND
# No level translator in the spike: GPIO drives the DUT directly (3v3 both ends).
#
# Phase convention (matches pico_debug PIO side-set semantics):
#   - write: data set while SWCLK low, rising edge latches into target.
#   - read:  sample SWDIO while SWCLK high (target drove it on prior falling edge).
# If IDCODE reads back as garbage, the sample point is the first thing to flip.

from machine import Pin
import time

SWD_OK = 1      # ACK = 0b001, LSB-first on the wire
SWD_WAIT = 2    # 0b010
SWD_FAULT = 4   # 0b100


def _parity32(v):
    v ^= v >> 16
    v ^= v >> 8
    v ^= v >> 4
    v ^= v >> 2
    v ^= v >> 1
    return v & 1


class SWD:
    def __init__(self, swclk=2, swdio=3, delay_us=1):
        self.clk = Pin(swclk, Pin.OUT, value=0)
        self.io_num = swdio
        self.io = Pin(swdio, Pin.OUT, value=1)
        self.delay_us = delay_us
        self._dir_out = True

    def _d(self):
        if self.delay_us:
            time.sleep_us(self.delay_us)

    # --- direction control for the shared SWDIO line ---
    def _set_out(self):
        if not self._dir_out:
            self.io.init(Pin.OUT)
            self._dir_out = True

    def _set_in(self):
        if self._dir_out:
            self.io.init(Pin.IN, Pin.PULL_UP)
            self._dir_out = False

    # --- bit primitives (LSB-first) ---
    def write_bits(self, value, n):
        self._set_out()
        io, clk = self.io, self.clk
        for _ in range(n):
            clk.value(0)
            io.value(value & 1)
            self._d()
            clk.value(1)
            self._d()
            value >>= 1
        clk.value(0)

    def read_bits(self, n):
        self._set_in()
        clk, io = self.clk, self.io
        value = 0
        for i in range(n):
            clk.value(0)
            self._d()
            clk.value(1)
            value |= io.value() << i
            self._d()
        clk.value(0)
        return value

    # Turnaround length, in SWCLK cycles, at the read->write boundary. The
    # write->read boundary needs none: read_bits' first clock-low phase absorbs
    # it. Empirically the read->write handoff needs 2 cycles on this bit-bang
    # (proven on nRF52840: trail=2 survives back-to-back reads, mid=2 completes
    # the DP power-up handshake; 1 desyncs the DP).
    TRN_CYCLES = 2

    def _trn(self):
        self._set_in()
        for _ in range(self.TRN_CYCLES):
            self.clk.value(0)
            self._d()
            self.clk.value(1)
            self._d()
            self.clk.value(0)

    turnaround = _trn

    # --- reset / switch sequences ---
    def line_reset(self, clocks=60):
        # >= 50 cycles with SWDIO high
        self.write_bits((1 << clocks) - 1, clocks)

    def idle(self, n=8):
        self.write_bits(0, n)

    def jtag_to_swd(self):
        # JTAG-to-SWD select: 0xE79E, 16 bits LSB-first
        self.line_reset()
        self.write_bits(0xE79E, 16)
        self.line_reset()
        self.idle(2)

    def dormant_to_swd(self):
        # Dormant -> SWD (DPv2 multidrop parts: RP2040/RP2350).
        # 8 high, 0x6209F392 0x86852D95 0xE3DDAFE9 0x19BC0EA2 (128-bit selection
        # alert), 4 low, then 0x1A activation code, then line reset.
        self.write_bits(0xFF, 8)
        for word in (0x6209F392, 0x86852D95, 0xE3DDAFE9, 0x19BC0EA2):
            self.write_bits(word, 32)
        self.write_bits(0x00, 4)
        self.write_bits(0x1A, 8)
        self.line_reset()
        self.idle(2)

    # --- DP/AP transfers ---
    def _request(self, apndp, rnw, addr):
        # addr is the 2-bit A[3:2] field (i.e. reg>>2 & 3)
        a2 = (addr >> 2) & 1
        a3 = (addr >> 3) & 1
        bits = (apndp & 1) | ((rnw & 1) << 1) | (a2 << 2) | (a3 << 3)
        par = (apndp ^ rnw ^ a2 ^ a3) & 1
        # start=1, [apndp,rnw,a2,a3], parity, stop=0, park=1
        return 1 | (bits << 1) | (par << 5) | (0 << 6) | (1 << 7)

    def read(self, apndp, addr):
        req = self._request(apndp, 1, addr)
        self.write_bits(req, 8)
        ack = self.read_bits(3)          # write->read trn absorbed
        if ack != SWD_OK:
            self._trn()
            return ack, None
        value = self.read_bits(32)
        par = self.read_bits(1)
        self._trn()                      # read->write turnaround
        if _parity32(value) != par:
            return SWD_OK, ("PARITY", value)
        return SWD_OK, value

    def write(self, apndp, addr, value):
        req = self._request(apndp, 0, addr)
        self.write_bits(req, 8)
        ack = self.read_bits(3)          # write->read trn absorbed
        self._trn()                      # read->write turnaround
        if ack != SWD_OK:
            return ack
        self.write_bits(value, 32)
        self.write_bits(_parity32(value), 1)
        self.idle(8)
        return SWD_OK

    def targetsel(self, target_id):
        # multidrop DP.TARGETSEL write (addr 0xC). ACK is driven by all targets
        # and must be ignored; data is sent regardless. Untested (nRF52840 is
        # single-drop); revisit on RP2040/RP2350 silicon.
        req = self._request(0, 0, 0xC)
        self.write_bits(req, 8)
        self.read_bits(3)                # ignore ack
        self._trn()
        self.write_bits(target_id, 32)
        self.write_bits(_parity32(target_id), 1)
        self.idle(8)


# Known DPIDR / TARGETSEL values for sanity-checking the spike.
KNOWN = {
    0x0BC12477: "RP2040 (DPv2 multidrop)",
    0x4C013477: "RP2350 (DPv2 multidrop)",   # verify against silicon
    0x2BA01477: "Cortex-M3/M4 (e.g. STM32F4) DPv1",
    0x6BA02477: "Cortex-M33 (e.g. STM32H5) DPv2",
    0x0BB11477: "Cortex-M0+ DPv1",
}

# RP multidrop TARGETSEL instance ids (core 0). Confirm RP2350 value on silicon.
RP2040_CORE0 = 0x01002927
RP2350_CORE0 = 0x00040927


def try_single_drop(swclk=2, swdio=3, delay_us=1):
    s = SWD(swclk, swdio, delay_us)
    s.jtag_to_swd()
    ack, val = s.read(0, 0x0)   # DPIDR
    return s, ack, val


def try_multidrop(target_id, swclk=2, swdio=3, delay_us=1):
    s = SWD(swclk, swdio, delay_us)
    s.dormant_to_swd()
    s.targetsel(target_id)
    ack, val = s.read(0, 0x0)   # DPIDR after target select
    return s, ack, val


def main(swclk=2, swdio=3, delay_us=1):
    print("SWD spike: SWCLK=GP%d SWDIO=GP%d delay=%dus" % (swclk, swdio, delay_us))

    print("\n[1] single-drop (line-reset + JTAG-to-SWD)")
    try:
        s, ack, val = try_single_drop(swclk, swdio, delay_us)
        if ack == SWD_OK and isinstance(val, int):
            print("    ACK=OK DPIDR=0x%08X  %s" % (val, KNOWN.get(val, "unknown")))
            return val
        print("    ACK=%d val=%r" % (ack, val))
    except Exception as e:
        print("    err", e)

    for name, tid in (("RP2350", RP2350_CORE0), ("RP2040", RP2040_CORE0)):
        print("\n[2] multidrop targetsel %s (0x%08X)" % (name, tid))
        try:
            s, ack, val = try_multidrop(tid, swclk, swdio, delay_us)
            if ack == SWD_OK and isinstance(val, int):
                print("    ACK=OK DPIDR=0x%08X  %s" % (val, KNOWN.get(val, "unknown")))
                return val
            print("    ACK=%d val=%r" % (ack, val))
        except Exception as e:
            print("    err", e)

    print("\nNo IDCODE. Check wiring (SWCLK/SWDIO/GND), target power, and pull-up.")
    return None


if __name__ == "__main__":
    main()
