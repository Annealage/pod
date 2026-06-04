# PIO AP-DRW write streamer for fast flash programming (workstream D, RP2350).
#
# EXPERIMENTAL, DISABLED BY DEFAULT. Pass an instance to NRF52Flash(streamer=...)
# to opt in; the default flash path uses the simpler inline native write. On the
# nRF52840 this streamer measured ~3131 words/s vs ~2934 for the inline write,
# only ~7% for a second PIO block, GP14/15 funcsel switching, and a DP resync per
# burst. Kept because that margin may matter once other parts of the write path
# are optimised; not worth enabling on its own today.
#
# The general SWD transport (swd_pio) drives one bit-group per FIFO word, so a
# full AP write (request + ACK + 32-bit data + parity + idle) costs ~8 FIFO
# operations plus a blocking ACK read per word, and at ~600 words/s that is
# almost entirely Python/per-transaction overhead, not wire time.
#
# This streamer is a PIO program that runs the ENTIRE AP-DRW write transaction
# per FIFO word: it emits the (constant, preloaded) request, clocks ACK +
# turnaround WITHOUT pushing (discarded on-chip, so there is no per-word read to
# block on), then emits the 32-bit data, parity, and idle. Python only feeds
# data and parity words; the TX FIFO buffers ahead, so the SM runs closer to
# wire rate. ACK is not checked here; the caller sets CSW auto-increment + TAR
# via the normal DP/AP path, then verifies the written region and checks the
# DP CTRL/STAT sticky bits, so a dropped write is caught.
#
# It needs its own PIO block (general SWD is PIO1) because it does not fit
# alongside the general program in one block's 32-instruction memory. The two
# blocks cannot both drive GP14/GP15 at once, so the GPIO function mux is
# switched between them around each burst (the discovered funcsel values are
# read back from the pads, not hard-coded).
#
# WARNING (RP2350 Pico 2 W): CYW43 Wi-Fi runs on PIO2, so the streamer must use
# PIO0 (the free block), NOT PIO2 - building a state machine on PIO2 while Wi-Fi
# is live hard-wedges the chip. This path is off by default (NRF52Flash is
# constructed with streamer=None) and is UNVALIDATED on the RP2350-with-Wi-Fi
# config; the default sm_id below is set to PIO0 accordingly but not yet
# hardware-checked. Authoritative block map: annealage_pod.debug.pio_arbiter.PIO_MAP.

import rp2
import time
import machine
import micropython
from .swd_pio import parity32

# IO_BANK0 GPIO control registers (RP2350): GPIOx_CTRL = base + 0x04 + 8*x,
# FUNCSEL in bits [4:0].
_IO_BANK0 = 0x40028000


def _ctrl_addr(gpio):
    return _IO_BANK0 + 0x04 + 8 * gpio


def _read_funcsel(gpio):
    return machine.mem32[_ctrl_addr(gpio)] & 0x1F


def _set_funcsel(gpio, func):
    a = _ctrl_addr(gpio)
    machine.mem32[a] = (machine.mem32[a] & ~0x1F) | (func & 0x1F)


@rp2.asm_pio(
    out_init=rp2.PIO.OUT_HIGH,        # SWDIO
    set_init=rp2.PIO.OUT_HIGH,        # SWDIO (set group)
    sideset_init=rp2.PIO.OUT_LOW,     # SWCLK
    out_shiftdir=rp2.PIO.SHIFT_RIGHT, # LSB first
    in_shiftdir=rp2.PIO.SHIFT_RIGHT,
    autopull=False,
    autopush=False,
)
def drw_stream_prog():
    # Preamble (runs once): load the constant AP-DRW write request into Y.
    pull()                                  .side(0)
    mov(y, osr)                             .side(0)
    wrap_target()
    label("start")
    # Pull the data word FIRST and stash it in ISR. This is the per-word stall
    # point: between bursts the SM blocks here having emitted nothing, so there
    # is no dangling half-transaction (emitting the request before pulling data
    # leaves a request+ACK awaiting data, which the next burst's first word then
    # wrongly completes).
    pull()                                  .side(0)   # OSR = data; stall here
    mov(isr, osr)                           .side(0)   # stash data in ISR
    # --- request: 8 output bits, LSB first (present on clk-low, latch on rise)
    set(pindirs, 1)                         .side(0)   # drive SWDIO
    mov(osr, y)                             .side(0)
    set(x, 7)                               .side(0)
    label("req")
    out(pins, 1)                            .side(0)
    jmp(x_dec, "req")                       .side(1)
    # --- write->read turnaround + ACK(3) + read->write turnaround(2): clock 5
    #     cycles with SWDIO released, do not sample/push (TRN_WR=0: first edge
    #     after the request clocks ACK[0], matching the bit-bang framing).
    set(pins, 0)                            .side(0)
    set(pindirs, 0)                         .side(0)   # release SWDIO
    set(x, 4)                               .side(0)
    label("ack")
    nop()                                   .side(1)
    jmp(x_dec, "ack")                       .side(0)
    # --- data: 32 output bits (restore from ISR)
    set(pindirs, 1)                         .side(0)   # drive
    mov(osr, isr)                           .side(0)
    set(x, 31)                              .side(0)
    label("dat")
    out(pins, 1)                            .side(0)
    jmp(x_dec, "dat")                       .side(1)
    # --- parity: 1 output bit
    pull()                                  .side(0)
    out(pins, 1)                            .side(0)
    nop()                                   .side(1)
    # --- idle: 8 clocks, SWDIO held low
    set(pins, 0)                            .side(0)
    set(x, 7)                               .side(0)
    label("idle")
    nop()                                   .side(1)
    jmp(x_dec, "idle")                      .side(0)
    wrap()


# DP/AP request encoding (mirror swd_pio.SWDPio._request) for AP write to DRW.
def _drw_write_request():
    apndp, rnw, addr = 1, 0, 0x0C
    a2 = (addr >> 2) & 1
    a3 = (addr >> 3) & 1
    bits = (apndp & 1) | ((rnw & 1) << 1) | (a2 << 2) | (a3 << 3)
    par = (apndp ^ rnw ^ a2 ^ a3) & 1
    return 1 | (bits << 1) | (par << 5) | (0 << 6) | (1 << 7)


class DRWStreamer:
    # Wraps the general transport's pins on a second PIO block. The general
    # SWDPio (PIO1) does setup and verify; this drives the DRW write burst.
    def __init__(self, swd, sm_id=1):
        # sm_id 0..3 = PIO0, the free block (PIO1 is the general SWD transport,
        # PIO2 is the live CYW43 Wi-Fi block and MUST be avoided - see the module
        # header warning). Default to PIO0 SM1. Shares SWDIO/SWCLK with `swd`
        # (PIO1) via funcsel switching. UNVALIDATED on RP2350-with-Wi-Fi.
        self.swd = swd
        self.swdio = swd.swdio
        self.swclk = swd.swclk
        self.n_io = swd.swdio_num
        self.n_ck = swd.swclk_num
        # funcsel for the general transport (PIO1), captured before we steal pins
        self.func_general = _read_funcsel(self.n_io)
        self.sm = rp2.StateMachine(
            sm_id, drw_stream_prog,
            freq=int(swd.SYS_HZ / swd.clkdiv),
            out_base=self.swdio,
            set_base=self.swdio,
            sideset_base=self.swclk,
        )
        # constructing the PIO2 SM repointed the pads to PIO2; capture that
        # funcsel, then hand the pins back to PIO1 so normal DP/AP still works.
        self.func_stream = _read_funcsel(self.n_io)
        self._configured = False
        # active, but the program's first instruction is the preamble pull, so
        # with no config word fed yet the SM stalls there (clk low, no edges) and
        # emits nothing until the first burst. Hand the pins back to PIO1 now.
        self.sm.active(1)
        self._to_general()

    def _to_general(self):
        _set_funcsel(self.n_io, self.func_general)
        _set_funcsel(self.n_ck, self.func_general)

    def _to_stream(self):
        _set_funcsel(self.n_io, self.func_stream)
        _set_funcsel(self.n_ck, self.func_stream)

    def deinit(self):
        self.sm.active(0)
        self._to_general()

    @micropython.native
    def write_words(self, words):
        # Stream a run of DRW writes (caller set CSW auto-inc + TAR via the DP).
        # Feeds (data, parity) per word; the SM does the rest. Switches the pin
        # mux to PIO2 for the burst and back to PIO1 after the FIFO drains.
        sm = self.sm
        self._to_stream()
        if not self._configured:
            # one-time preamble: load the constant DRW request into the SM's Y.
            sm.put(_drw_write_request())
            self._configured = True
        for w in words:
            w = w & 0xFFFFFFFF
            sm.put(w)
            v = w
            v ^= v >> 16
            v ^= v >> 8
            v ^= v >> 4
            v ^= v >> 2
            v ^= v >> 1
            sm.put(v & 1)
        # wait for the TX FIFO to drain, then for the last transaction to finish
        while sm.tx_fifo():
            pass
        time.sleep_us(20)
        self._to_general()
