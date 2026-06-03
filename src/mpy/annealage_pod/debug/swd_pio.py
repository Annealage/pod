# SWD bit transport over an RP2350 PIO state machine (Annealage Pod, workstream D).
#
# Productionised from prototypes/rp2350-swd-spike/swd_pio.py (validated on an
# nRF52840: DPIDR 0x2BA01477, MEM-AP CPUID read, 15/15 clean at 6.25 MHz). The
# SWD line protocol (request encoding, jtag_to_swd, parity, turnaround) is
# unchanged; this module is the transport only. The DP/AP/MEM-AP layer lives in
# swd_dap.py.
#
# Wiring: pod GP14 = SWDIO, GP15 = SWCLK, common GND. Single-drop target.
#
# PIO model (after essele/pico_debug swd.pio): one program, SWCLK on side-set,
# SWDIO out/in/set with pindirs flipped in-program, LSB-first shift on OSR/ISR.
# A single control bit selects the output vs input routine (MicroPython relocates
# label jmp targets but not an `out pc` literal, so we avoid a jump table).

import rp2
import micropython
from machine import Pin

SWD_OK = 1      # ACK = 0b001, LSB-first on the wire
SWD_WAIT = 2    # 0b010
SWD_FAULT = 4   # 0b100


class SWDError(Exception):
    def __init__(self, msg, ack=None):
        super().__init__(msg)
        self.ack = ack

# Control-word routine selector (bit 0); bits 1.. carry (bit count - 1).
SEL_OUTPUT = 0
SEL_INPUT = 1


def parity32(v):
    v ^= v >> 16
    v ^= v >> 8
    v ^= v >> 4
    v ^= v >> 2
    v ^= v >> 1
    return v & 1


@rp2.asm_pio(
    out_init=rp2.PIO.OUT_HIGH,        # SWDIO
    set_init=rp2.PIO.OUT_HIGH,        # SWDIO (set group)
    sideset_init=rp2.PIO.OUT_LOW,     # SWCLK starts low
    out_shiftdir=rp2.PIO.SHIFT_RIGHT, # LSB first
    in_shiftdir=rp2.PIO.SHIFT_RIGHT,  # LSB first
    autopull=False,
    autopush=False,
)
def swd_prog():
    # Control word: bit 0 = routine select (0 output, 1 input); bits 1..27 =
    # (bit count - 1). Output routine also pulls one data word after the count.
    wrap_target()
    label("start")
    pull()                                  # control word into OSR
    out(x, 1)                               # x = routine select bit
    jmp(not_x, "output")

    # ---- input routine: 2 PIO cycles/bit, sample on the SWCLK-high plateau ----
    out(x, 27)                              # bit count - 1
    set(pindirs, 0)             .side(0)    # release SWDIO (input), clk low
    label("in_loop")
    nop()                       .side(1)    # clk high, let SWDIO settle
    in_(pins, 1)                .side(1)    # sample while clk high
    jmp(x_dec, "in_loop")       .side(0)    # clk low, next bit
    push()
    jmp("start")

    # ---- output routine: present on clk low, rising edge latches ----
    label("output")
    out(x, 27)                              # bit count - 1
    pull()                                  # data word
    set(pindirs, 1)             .side(0)    # drive SWDIO
    label("out_loop")
    out(pins, 1)                .side(0)    # present bit, clk low
    jmp(x_dec, "out_loop")      .side(1)    # rising edge latches
    set(pins, 0)                .side(0)    # leave SWDIO low, clk low
    jmp("start")


class SWDPio:
    # Turnaround, empirically derived on this PIO (see prototype notes):
    #   write->read: the output tail and input head each clock one SWCLK-low
    #     cycle without an edge the target counts, so ACK[0] is the first sampled
    #     bit -> TRN_WR = 0.
    #   read->write: clock 2 cycles with SWDIO released before the next drive.
    TRN_WR = 0
    TRN_RW = 2

    # PIO clock base (RP2350 sys clock). f_swclk(write) = SYS / clkdiv / 2.
    SYS_HZ = 150_000_000

    def __init__(self, swdio=14, swclk=15, sm_id=4, clkdiv=8):
        # clkdiv=8 -> 9.375 MHz SWCLK, validated 100/100 clean on the nRF52840
        # (12.5 MHz fails: input-sampling phase limit). See spike-findings.md.
        # sm_id 4..7 = PIO1 (PIO0 SMs 0..3 are used by CYW43 Wi-Fi).
        # Enable the SWDIO pad pull-up BEFORE StateMachine() grabs the pin;
        # reconfiguring the Pin to SIO afterwards would disconnect PIO from the
        # pad (a real bug found in bring-up), so do not.
        Pin(swdio, Pin.IN, Pin.PULL_UP)
        self.swdio_num = swdio
        self.swclk_num = swclk
        self.swdio = Pin(swdio)
        self.swclk = Pin(swclk)
        self.sm_id = sm_id
        self.clkdiv = clkdiv
        self.sm = rp2.StateMachine(
            sm_id, swd_prog,
            freq=int(self.SYS_HZ / clkdiv),
            out_base=self.swdio,
            set_base=self.swdio,
            in_base=self.swdio,
            sideset_base=self.swclk,
        )
        self.sm.active(1)

    @property
    def f_swclk(self):
        return int(self.SYS_HZ / self.clkdiv / 2)

    def deinit(self):
        self.sm.active(0)

    def release(self):
        # Full teardown so this PIO block is reclaimable (e.g. by the logic
        # analyser swap): stop the SM and clear the block's instruction memory.
        # The SM is freed when the last reference is dropped (the caller drops
        # it), so a later SWDPio re-creates it cleanly; remove_program stops the
        # swd_prog leaking instruction memory across repeated swaps (see the
        # PIO-instruction-memory caveat in dev-notes.md).
        try:
            self.sm.active(0)
        except Exception:
            pass
        try:
            rp2.PIO(self.sm_id // 4).remove_program()
        except Exception:
            pass

    # --- bit primitives (LSB-first) -------------------------------------
    def write_bits(self, value, n):
        self.sm.put((n - 1) << 1 | SEL_OUTPUT)
        self.sm.put(value & 0xFFFFFFFF)

    def read_bits(self, n):
        self.sm.put((n - 1) << 1 | SEL_INPUT)
        word = self.sm.get()
        # ISR shifted right; the n collected bits land high, so right-justify.
        return word >> (32 - n)

    # --- reset / switch sequences ---------------------------------------
    def line_reset(self, clocks=60):
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

    def dormant_to_swd(self):
        # SWD multidrop wake from dormant (RP2040/RP2350): not yet validated on
        # an RP target (no RP DUT wired). Left for D1 multidrop work.
        raise NotImplementedError("dormant/multidrop wake not yet validated")

    # --- raw DP/AP transfers --------------------------------------------
    def _request(self, apndp, rnw, addr):
        a2 = (addr >> 2) & 1
        a3 = (addr >> 3) & 1
        bits = (apndp & 1) | ((rnw & 1) << 1) | (a2 << 2) | (a3 << 3)
        par = (apndp ^ rnw ^ a2 ^ a3) & 1
        return 1 | (bits << 1) | (par << 5) | (0 << 6) | (1 << 7)

    def _trn_read_to_write(self):
        if self.TRN_RW:
            self.read_bits(self.TRN_RW)

    def read(self, apndp, addr, _retries=4):
        # Retry on WAIT (ack=2), matching pico_debug's do/while(WAIT) loop.
        ack, val = SWD_FAULT, None
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
        if parity32(value) != par:
            return SWD_OK, ("PARITY", value)
        return SWD_OK, value

    def write(self, apndp, addr, value, _retries=4):
        ack = SWD_FAULT
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
        self.write_bits(parity32(value), 1)
        self.idle(8)
        return SWD_OK

    @micropython.native
    def write_drw_block(self, words):
        # Fast inner loop for flash programming: stream AP DRW writes with the
        # request precomputed and the whole SWD write sequence (request, ACK,
        # 32-bit data, parity, read->write turnaround, idle) inlined, avoiding
        # the per-word method-call chain that dominates the cost (~99% of the
        # per-word time is Python/transaction overhead, not wire time). The
        # caller must have set CSW to 32-bit auto-increment and written TAR; the
        # SELECT bank must be AP bank 0 (CSW/TAR/DRW all live there). ACK is
        # checked but not WAIT-retried; the caller verifies by read-back and
        # checks sticky errors, so a stray WAIT is caught, not silently dropped.
        sm = self.sm
        req = self._request(1, 0, 0x0C)   # AP write, DRW
        trn = self.TRN_RW
        trn_ctl = ((trn - 1) << 1 | 1) if trn else 0
        for w in words:
            w = w & 0xFFFFFFFF
            sm.put(14)                    # 8-bit output (request)
            sm.put(req)
            sm.put(5)                     # 3-bit input (ACK)
            ack = (sm.get() >> 29) & 7
            if trn:
                sm.put(trn_ctl)           # read->write turnaround
                sm.get()
            if ack != 1:                  # SWD_OK
                raise SWDError("DRW write ack=%d" % ack, ack)
            sm.put(62)                    # 32-bit output (data)
            sm.put(w)
            # inline parity32(w)
            v = w
            v ^= v >> 16
            v ^= v >> 8
            v ^= v >> 4
            v ^= v >> 2
            v ^= v >> 1
            sm.put(0)                     # 1-bit output (parity)
            sm.put(v & 1)
            sm.put(14)                    # 8-bit output (idle)
            sm.put(0)

    @micropython.native
    def read_drw_block(self, count):
        # Fast inner loop for block reads (verify / dump). Same inlining win as
        # write_drw_block. Each word is a posted AP DRW read (returns the
        # previous result, latches the current one and auto-increments TAR)
        # followed by a DP RDBUFF read that yields that word, mirroring
        # MEMAP.read_ap but without the per-word method chain. Caller sets CSW to
        # 32-bit auto-increment and writes TAR; SELECT must be AP bank 0 with
        # DPBANKSEL=0 (so RDBUFF reads the AP result). Parity is clocked but not
        # checked (a bad read shows up as a verify mismatch); ACK is checked.
        sm = self.sm
        req_drw = self._request(1, 1, 0x0C)   # AP read, DRW (posted)
        req_rb = self._request(0, 1, 0x0C)    # DP read, RDBUFF
        out = []
        push = out.append
        for _ in range(count):
            # posted DRW read: discard return, latches current word, TAR += 4
            sm.put(14)
            sm.put(req_drw)
            sm.put(5)
            a = (sm.get() >> 29) & 7
            if a != 1:
                raise SWDError("DRW read ack=%d" % a, a)
            sm.put(63)                    # 32-bit input (data, discard)
            sm.get()
            sm.put(1)                     # 1-bit input (parity, discard)
            sm.get()
            sm.put(3)                     # 2-bit input (read->write turnaround)
            sm.get()
            # RDBUFF read: yields the word the posted read latched
            sm.put(14)
            sm.put(req_rb)
            sm.put(5)
            a = (sm.get() >> 29) & 7
            if a != 1:
                raise SWDError("RDBUFF read ack=%d" % a, a)
            sm.put(63)                    # 32-bit input (data)
            push(sm.get() & 0xFFFFFFFF)
            sm.put(1)
            sm.get()
            sm.put(3)
            sm.get()
        return out


# Known DPIDR / IDR values for sanity-checks during bring-up.
KNOWN_DPIDR = {
    0x0BC12477: "RP2040 (DPv2 multidrop)",
    0x4C013477: "RP2350 (DPv2 multidrop)",
    0x2BA01477: "Cortex-M3/M4 DPv1 (e.g. nRF52840)",
}
