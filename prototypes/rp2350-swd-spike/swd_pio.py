# SWD transport over RP2350 PIO (Annealage Pod, Phase D1.1).
#
# Ports the bit transport of swd_bitbang.py to a PIO state machine for speed.
# The SWD line protocol (request encoding, jtag_to_swd, DP/AP/MEM-AP sequencing,
# parity) is unchanged from swd_bitbang.py; ONLY write_bits/read_bits are
# replaced by PIO FIFO transactions.
#
# Wiring (do not change): pod GP14 = SWDIO, GP15 = SWCLK, common GND.
# Single-drop SWD target: nRF52840 dongle.
#
# PIO model (after essele/pico_debug swd.pio):
#   - One PIO program; the first word of every transaction is a jump-table
#     dispatch via `out pc, 5` so FIFO words act as function calls.
#   - SWCLK is driven by side-set. SWDIO is out/in/set pin; its direction is
#     flipped in-program with `set pindirs`.
#   - LSB-first shift on both OSR and ISR (matches SWD wire order).
#   - clkdiv sets the SWD clock: SWCLK toggles once per side-set, i.e. one full
#     SWCLK period takes 2 PIO clocks, so f_swclk = f_pio / (2 * clkdiv).
#
# Phase convention (must match the target's expectation, same as bit-bang):
#   - output: SWDIO set while SWCLK low (side 0), rising edge (side 1) latches.
#   - input:  SWDIO sampled by `in pins` on the SWCLK-high half.
#
# Turnaround (trn): RE-DERIVED empirically here, not assumed from the bit-bang.
# See TRN notes near read()/write().

import rp2
from machine import Pin
import time

SWD_OK = 1      # ACK = 0b001, LSB-first on the wire
SWD_WAIT = 2    # 0b010
SWD_FAULT = 4   # 0b100

# Jump-table offsets into the PIO program. These MUST match the label order in
# the assembled program (offsets are resolved at runtime via prog[...] below,
# but we dispatch by writing the offset as the low 5 bits of the control word).


def _parity32(v):
    v ^= v >> 16
    v ^= v >> 8
    v ^= v >> 4
    v ^= v >> 2
    v ^= v >> 1
    return v & 1


# ---------------------------------------------------------------------------
# PIO program.
#
# Dispatch: `out pc, 5` -> jumps to one of the public entry labels, which must
# sit at fixed offsets. We lay them out so their offsets are small constants we
# can hardcode into the control words.
#
# Entry points (offset = instruction index from program start):
#   start  : out pc,5            (offset 0)  -- dispatch
#   output : shift OUT `count+1` bits        -- control word low5 = OUTPUT off
#   inp    : shift IN  `count+1` bits        -- control word low5 = INPUT off
#
# Control word formats:
#   OUTPUT : (count-1) << 5 | OUTPUT_off        then data words (LSB first)
#   INPUT  : (count-1) << 5 | INPUT_off         then SM pushes one word
#
# We keep SWDIO idle-high via pull-up; pindirs flipped per routine.
# ---------------------------------------------------------------------------

@rp2.asm_pio(
    out_init=rp2.PIO.OUT_HIGH,        # SWDIO
    set_init=rp2.PIO.OUT_HIGH,        # SWDIO (set group)
    sideset_init=rp2.PIO.OUT_LOW,     # SWCLK starts low
    out_shiftdir=rp2.PIO.SHIFT_RIGHT, # LSB first
    in_shiftdir=rp2.PIO.SHIFT_RIGHT,  # LSB first
    autopull=False,                   # explicit pull: full control over OSR
    autopush=False,                   # explicit push (variable bit counts)
)
def swd_prog():
    # No autopull/autopush. Every routine starts by explicitly `pull`-ing its
    # control word and (for output) its single data word. This keeps the OSR
    # shift state from leaking between transactions even when a transfer is not
    # a multiple of 32 bits (e.g. an 8-bit request, a 28-bit reset chunk).
    #
    # Dispatch uses RELOCATABLE label jumps, not `out pc` with literal offsets:
    # MicroPython loads the program at an arbitrary PIO offset and relocates
    # label-based jmp targets, but it does NOT relocate an `out pc` literal, so
    # a jump-table would land at the wrong absolute address. With only two
    # routines we select on a single control bit instead.
    #
    # Control word layout (low to high):
    #   bit 0     : routine select  (0 = output, 1 = input)
    #   bits 1..27: bit count - 1
    #
    # ---- dispatch ----
    wrap_target()
    label("start")
    pull()                                  # fetch control word into OSR
    out(x, 1)                               # x = routine select bit
    jmp(not_x, "output")                    # 0 -> output, else fall to input

    # ---- input routine ----
    # Two PIO cycles per bit, mirroring the bit-bang read phase: drive SWCLK
    # high, let SWDIO settle for one cycle, sample while still high, then drop
    # SWCLK low. The target presents each bit on its falling edge, so we sample
    # on the high plateau that follows.
    out(x, 27)                              # bit count - 1
    set(pindirs, 0)             .side(0)    # release SWDIO (input), clk low
    label("in_loop")
    nop()                       .side(1)    # clk high, let SWDIO settle
    in_(pins, 1)                .side(1)    # sample while clk high
    jmp(x_dec, "in_loop")       .side(0)    # clk low, next bit
    push()                                  # return collected bits
    jmp("start")

    # ---- output routine ----
    label("output")
    out(x, 27)                              # bit count - 1
    pull()                                  # fetch data word
    set(pindirs, 1)             .side(0)    # drive SWDIO
    label("out_loop")
    out(pins, 1)                .side(0)    # present bit, clk low
    jmp(x_dec, "out_loop")      .side(1)    # rising edge latches
    set(pins, 0)                .side(0)    # leave SWDIO low, clk low
    jmp("start")


# Control-word routine selector (bit 0).
SEL_OUTPUT = 0
SEL_INPUT = 1


class SWDPio:
    def __init__(self, swdio=14, swclk=15, sm_id=4, clkdiv=12):
        # sm_id 4..7 = PIO1 (PIO0 SMs 0..3 are used by CYW43 Wi-Fi).
        # Enable the pad pull-up on SWDIO BEFORE the StateMachine grabs the pin:
        # StateMachine() sets the GPIO function mux to PIO but leaves the pad
        # pull bits intact. Reconfiguring the Pin to SIO afterwards would
        # disconnect PIO from the pad (this was a real bug), so we must not.
        Pin(swdio, Pin.IN, Pin.PULL_UP)
        self.swdio = Pin(swdio)
        self.swclk = Pin(swclk)
        self.sm_id = sm_id
        self.clkdiv = clkdiv
        freq = int(150_000_000 / clkdiv)
        self.sm = rp2.StateMachine(
            sm_id, swd_prog,
            freq=freq,
            out_base=self.swdio,
            set_base=self.swdio,
            in_base=self.swdio,
            sideset_base=self.swclk,
        )
        self.sm.active(1)

    @property
    def f_swclk(self):
        # Output routine: 2 PIO clocks per SWCLK period (out.side0 + jmp.side1).
        # Input routine is asymmetric (nop.side1 + in.side1 + jmp.side0 = 3 PIO
        # clocks/bit, i.e. ~2/3 of this rate during reads). This reports the
        # write-phase SWCLK, which bounds the fastest edges on the wire.
        return int(150_000_000 / self.clkdiv / 2)

    def deinit(self):
        self.sm.active(0)

    # --- bit primitives (LSB-first) -------------------------------------
    def write_bits(self, value, n):
        # OUTPUT control word [count-1 << 1 | SEL_OUTPUT], then one data word.
        self.sm.put((n - 1) << 1 | SEL_OUTPUT)
        self.sm.put(value & 0xFFFFFFFF)

    def read_bits(self, n):
        self.sm.put((n - 1) << 1 | SEL_INPUT)
        word = self.sm.get()
        # ISR shifted right, so the n collected bits land in the high end;
        # right-justify them.
        return word >> (32 - n)

    # --- reset / switch sequences ---------------------------------------
    def line_reset(self, clocks=60):
        # >=50 cycles SWDIO high. Send in chunks <=32.
        while clocks > 0:
            c = 32 if clocks > 32 else clocks
            self.write_bits((1 << c) - 1, c)
            clocks -= c

    def idle(self, n=8):
        self.write_bits(0, n)

    def jtag_to_swd(self):
        self.line_reset()
        self.write_bits(0xE79E, 16)
        self.line_reset()
        self.idle(2)

    # --- DP/AP transfers ------------------------------------------------
    def _request(self, apndp, rnw, addr):
        a2 = (addr >> 2) & 1
        a3 = (addr >> 3) & 1
        bits = (apndp & 1) | ((rnw & 1) << 1) | (a2 << 2) | (a3 << 3)
        par = (apndp ^ rnw ^ a2 ^ a3) & 1
        return 1 | (bits << 1) | (par << 5) | (0 << 6) | (1 << 7)

    # Turnaround, RE-DERIVED empirically on this PIO (not copied from bit-bang):
    #
    # write->read (TRN_WR): the output routine ends with `set pins,0 .side(0)`
    #   and the input routine opens with `set pindirs,0 .side(0)`; those two
    #   SWCLK-low cycles absorb the turnaround WITHOUT emitting a clock edge the
    #   target counts, so the first sampled bit is already ACK[0]. TRN_WR = 0
    #   (confirmed: raw bits after a DPIDR request were [1,0,0,...] = OK at
    #   offset 0; reading with any nonzero offset mis-frames the ACK).
    #
    # read->write (TRN_RW): after a read we must give the line back to the host
    #   over the turnaround period. Clocked as `read_bits` (SWDIO released).
    #   2 cycles, matching the bit-bang result on this nRF52840.
    TRN_WR = 0
    TRN_RW = 2

    def _trn_read_to_write(self):
        # Clock TRN_RW cycles with SWDIO released; the next write_bits drives it.
        if self.TRN_RW:
            self.read_bits(self.TRN_RW)

    def read(self, apndp, addr, _retries=4):
        # Retry on WAIT (ack=2), matching pico_debug's do/while(WAIT) loop.
        for _ in range(_retries):
            ack, val = self._read_once(apndp, addr)
            if ack != SWD_WAIT:
                return ack, val
        return ack, val

    def _read_once(self, apndp, addr):
        req = self._request(apndp, 1, addr)
        self.write_bits(req, 8)
        raw = self.read_bits(self.TRN_WR + 3)
        ack = (raw >> self.TRN_WR) & 0x7
        if ack != SWD_OK:
            self._trn_read_to_write()
            return ack, None
        value = self.read_bits(32)
        par = self.read_bits(1)
        self._trn_read_to_write()
        if _parity32(value) != par:
            return SWD_OK, ("PARITY", value)
        return SWD_OK, value

    def write(self, apndp, addr, value, _retries=4):
        for _ in range(_retries):
            ack = self._write_once(apndp, addr, value)
            if ack != SWD_WAIT:
                return ack
        return ack

    def _write_once(self, apndp, addr, value):
        req = self._request(apndp, 0, addr)
        self.write_bits(req, 8)
        raw = self.read_bits(self.TRN_WR + 3)
        ack = (raw >> self.TRN_WR) & 0x7
        self._trn_read_to_write()
        if ack != SWD_OK:
            return ack
        self.write_bits(value, 32)
        self.write_bits(_parity32(value), 1)
        self.idle(8)
        return SWD_OK


# Known DPIDR values (sanity-check).
KNOWN = {
    0x0BC12477: "RP2040 (DPv2 multidrop)",
    0x4C013477: "RP2350 (DPv2 multidrop)",
    0x2BA01477: "Cortex-M3/M4 DPv1",
    0x24770011: "AP IDR (AHB-AP)",
}


def power_up(s):
    # ABORT=0x1E, SELECT=0x0, CTRL/STAT=0x50000000, then read CTRL/STAT.
    s.write(0, 0x0, 0x1E)        # ABORT clear-all
    s.write(0, 0x8, 0x0)         # SELECT = 0
    s.write(0, 0x4, 0x50000000)  # CTRL/STAT power-up request
    return s.read(0, 0x4)        # read CTRL/STAT


def read_ap_idr(s):
    # SELECT APBANKSEL=0xF, read AP 0xC (posted), then read DP RDBUFF 0xC.
    s.write(0, 0x8, 0x000000F0)  # SELECT: APBANKSEL=0xF
    s.read(1, 0xC)               # posted AP read of IDR
    return s.read(0, 0xC)        # RDBUFF


def read_mem32(s, addr):
    # CSW=0x23000002 (32-bit, auto-inc off), TAR=addr, read DRW (posted),
    # read RDBUFF.
    s.write(0, 0x8, 0x00000000)  # SELECT: APBANKSEL=0, AP=0
    s.write(1, 0x0, 0x23000002)  # CSW
    s.write(1, 0x4, addr)        # TAR
    s.read(1, 0xC)               # DRW posted read
    return s.read(0, 0xC)        # RDBUFF


def validate(clkdiv=12, sm_id=4, verbose=True):
    s = SWDPio(swdio=14, swclk=15, sm_id=sm_id, clkdiv=clkdiv)
    out = {}
    try:
        s.jtag_to_swd()
        ack, dpidr = s.read(0, 0x0)
        out["dpidr"] = (ack, dpidr)
        if verbose:
            print("DPIDR ack=%d val=%r %s" % (
                ack, dpidr if not isinstance(dpidr, int) else hex(dpidr),
                KNOWN.get(dpidr, "")))
        if ack != SWD_OK or not isinstance(dpidr, int):
            return out, s

        ack, ctrl = power_up(s)
        out["ctrl"] = (ack, ctrl)
        if verbose:
            print("CTRL/STAT ack=%d val=%r" % (
                ack, ctrl if not isinstance(ctrl, int) else hex(ctrl)))

        ack, apidr = read_ap_idr(s)
        out["ap_idr"] = (ack, apidr)
        if verbose:
            print("AP IDR ack=%d val=%r" % (
                ack, apidr if not isinstance(apidr, int) else hex(apidr)))

        ack, cpuid = read_mem32(s, 0xE000ED00)
        out["cpuid"] = (ack, cpuid)
        if verbose:
            print("CPUID ack=%d val=%r" % (
                ack, cpuid if not isinstance(cpuid, int) else hex(cpuid)))
    finally:
        pass
    return out, s


def reliability(n=15, clkdiv=12, sm_id=4):
    s = SWDPio(swdio=14, swclk=15, sm_id=sm_id, clkdiv=clkdiv)
    s.jtag_to_swd()
    ok = 0
    vals = []
    for _ in range(n):
        ack, v = s.read(0, 0x0)
        if ack == SWD_OK and v == 0x2BA01477:
            ok += 1
        vals.append((ack, v))
    print("DPIDR %d/%d clean @ f_swclk=%d Hz" % (ok, n, s.f_swclk))
    return ok, n, vals, s


def timing(s, n=200):
    # Time n DPIDR reads (full frame: 8b req + trn+ack + 32b + par + trn).
    t0 = time.ticks_us()
    for _ in range(n):
        s.read(0, 0x0)
    dt = time.ticks_diff(time.ticks_us(), t0)
    print("%d DPIDR reads in %d us => %.1f us/read" % (n, dt, dt / n))
    return dt / n


if __name__ == "__main__":
    validate()
