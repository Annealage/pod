# PIO SPI target (Stage 1: stream/counter mode) for the Annealage Pod (RP2350),
# workstream F5.1. The pod is the SPI PERIPHERAL; the DUT is the controller.
#
# Built the same way as the logic analyser (debug/logic_analyser.py): an
# @rp2.asm_pio program assembled per config, an rp2.StateMachine on PIO0, and
# rp2.DMA channels moving the byte streams so no MicroPython code sits in the
# per-byte data path (the single-core cooperative runtime cannot afford a
# per-byte pump). Stage 1 covers SPI mode 0, 8-bit, MSB-first, and a
# stream/counter personality: MISO is a ring-DMA replaying a 0..255 counter
# table (unbounded transfer length, no growing buffer); MOSI is captured into a
# bounded overwrite ring. Register-file mode (spi_target_regs, Stage 2) and
# SPI modes 1-3 (Stage 2) are not implemented here.
#
# PIO budget: PIO0 is the free block (annealage_pod.debug.pio_arbiter.PIO_MAP);
# PIO1 is the SWD transport, PIO2 is CYW43 Wi-Fi and must never be touched.
# The SPI target and the logic analyser are mutually exclusive on PIO0 for
# Stage 1 - both claim PIO0 through pio_arbiter, so a live one blocks the
# other (PioConflict) rather than silently sharing the block.
#
# DMA register facts, validated on this silicon (RP2350; same facts as the
# logic analyser, extended with the TX FIFO peers):
#   PIO block base = 0x50200000 + block*0x100000   (PIO0/1/2)
#   RX FIFO<sm>    = base + 0x20 + sm*4   RX DREQ<sm> = block*8 + 4 + sm
#   TX FIFO<sm>    = base + 0x10 + sm*4   TX DREQ<sm> = block*8 + sm
# (block = sm_id // 4, sm = sm_id % 4.)

import uctypes

import machine
import rp2
from machine import Pin

from .debug import pio_arbiter
from ._ring import ring_order

SYS_HZ = 150_000_000

# DMA transfer-count seed: the RP2350 normal-mode max (2^28 - 1). This is a
# large FIXED count, not self-restarting: both DMAs count down from here and
# stop at zero (after which MISO stalls on an empty OSR and RX capture halts).
# 2^28-1 bytes is far past any bench transfer (a > 65535-byte stream uses a
# fraction of it), and it stays a MicroPython small int so the byte-count
# arithmetic on the IRQ/status path never allocates. Truly-unbounded streaming
# would need the channel chained to itself (chain_to + reload); that is Stage 2.
_COUNT0 = 0x0FFFFFFF


def _pio_base(block):
    return 0x50200000 + block * 0x100000


def _rx_fifo_addr(sm_id):
    return _pio_base(sm_id // 4) + 0x20 + (sm_id % 4) * 4


def _rx_dreq(sm_id):
    return (sm_id // 4) * 8 + 4 + (sm_id % 4)


def _tx_fifo_addr(sm_id):
    return _pio_base(sm_id // 4) + 0x10 + (sm_id % 4) * 4


def _tx_dreq(sm_id):
    return (sm_id // 4) * 8 + (sm_id % 4)


def _next_pow2(n):
    p = 1
    while p < n:
        p <<= 1
    return p


def _log2_pow2(v):
    # log2 of a power-of-two `v`. int.bit_length() is not available on this
    # MicroPython build, so count shifts instead. `v` is always 2**k here (the
    # DMA ring size is a power-of-two byte count).
    n = 0
    while v > 1:
        v >>= 1
        n += 1
    return n


def _build_prog(bits, mode, sck, cs):
    """Assemble the SPI-target program for `bits`-wide mode-0 frames.

    `sck`/`cs` are the literal GPIO numbers (assembly-time constants, like the
    logic analyser's trigger pin) used by the absolute `wait ... gpio` checks;
    the CS abort check instead uses the SM's configured jmp_pin (also wired to
    `cs`), which is set on the StateMachine, not assembled here.
    """
    if mode != 0:
        raise ValueError("SPI mode %d is Stage 2" % mode)

    @rp2.asm_pio(out_init=rp2.PIO.OUT_LOW,
                in_shiftdir=rp2.PIO.SHIFT_LEFT, out_shiftdir=rp2.PIO.SHIFT_LEFT,
                autopush=True, push_thresh=bits, autopull=True, pull_thresh=bits)
    def prog():
        wrap_target()
        label("resync")
        # Clear the ISR (and its shift counter) so a short/aborted transaction
        # cannot leave the byte framing bit-slipped into the next one.
        mov(isr, null)
        wait(0, gpio, cs)          # block until CS asserted (active low)
        label("bit")
        out(pins, 1)               # present the next MISO bit (SCK is low here)
        wait(1, gpio, sck)         # mode 0: sample MOSI on the rising edge
        in_(pins, 1)
        wait(0, gpio, sck)         # mode 0: MISO changes on the falling edge
        jmp(pin, "resync")         # jmp_pin=CS high (deasserted): close the frame
        jmp("bit")                 # else next bit, same CS assertion
        wrap()

    return prog


class SpiTarget:
    """A PIO SPI target on one PIO0 state machine: stream/counter personality.

    MISO drives a ring-DMA over a 256-byte 0..255 counter table (RP2350 DMA
    read-address ring), so the controller reads a predictable 0,1,...,255,0,...
    counter for any transfer length. MOSI is captured by DMA into a bounded
    overwrite ring of `size` bytes (rounded up to a power of two, a ring
    requirement). `bytes_rx` (from the free DMA transfer-count register) is the
    primary verification metric; `captured` exposes the ring tail for spot
    checks. sm_id 0..3 are PIO0 (the only block this class will build on).

    No rolling checksum: the MOSI ring is a bounded overwrite ring filled by
    DMA writing RAM directly, with no MicroPython code in the data path: there
    is no way to fold every received byte into a running checksum without a
    per-byte drain, which would put a Python loop back in the data path and
    violate the single-core cooperative-loop constraint. `bytes_rx` (exact) plus
    the captured ring tail is the Stage-1 verification surface instead. A
    zero-cost checksum is possible later via the RP2350 DMA CRC sniffer
    (pack_ctrl sniff_en=True + the DMA_SNIFF_CTRL/DATA registers), deferred.
    """

    def __init__(self, miso, mosi, sck, cs, mode=0, bits=8, size=1024, sm_id=0,
                 name="spi_target"):
        if not (0 <= sm_id <= 3):
            raise ValueError(
                "sm_id must be 0..3 (PIO0 only; see "
                "annealage_pod.debug.pio_arbiter.PIO_MAP)")
        if mode != 0:
            raise ValueError("SPI mode %d is Stage 2" % mode)
        if bits != 8:
            raise ValueError("%d-bit frames are Stage 2 (Stage 1 is 8-bit only)" % bits)

        self.sm_id = sm_id
        self._name = name
        self.mode = mode
        self.bits = bits
        self.size = _next_pow2(size)
        # The MOSI capture ring is size*4 bytes and the DMA ring-wrap field is
        # 4 bits (32 KB max), so cap size: pack_ctrl gets a legal ring_size
        # instead of an opaque 'bad field value', and a request cannot exhaust
        # pod RAM (mirrors LogicAnalyser.MAX_WORDS).
        if self.size > 8192:
            raise ValueError(
                "spi_target size too large: max 8192 bytes (RX ring is size*4 "
                "bytes; the DMA ring-wrap field caps at 32 KB)")
        self.transfers_total = 0
        self.last_cs_len = 0
        self._last_rx = 0
        self._sm = None
        self._tx = None
        self._rx = None
        self._cs_pin = None

        # Claim the PIO block under THIS instance's name before touching any
        # hardware: a second SPI target (or a logic-analyser capture) on the
        # same block then raises PioConflict instead of silently building a
        # second state machine over this one. claim() is idempotent per owner,
        # so each live instance must claim under a distinct name.
        pio_arbiter.claim(self._name, sm_id // 4)
        try:
            self._build(miso, mosi, sck, cs)
        except Exception:
            pio_arbiter.release(self._name)
            raise

    def _build(self, miso, mosi, sck, cs):
        sm_id = self.sm_id

        # Grab the input pins before the StateMachine claims them (reconfiguring
        # a pad's function after the SM owns it disconnects PIO from the pad;
        # see debug/swd_pio.py's pin-ordering lesson).
        sck_pin = Pin(sck, Pin.IN)
        # CS is active-low: pull it up so an undriven line reads deasserted. A
        # floating CS otherwise reads low (asserted) and lets the SM shift on
        # SCK noise, which both advances the free-running MISO counter and
        # counts a spurious transaction on the eventual rising edge.
        self._cs_pin = Pin(cs, Pin.IN, Pin.PULL_UP)
        Pin(mosi, Pin.IN)
        miso_pin = Pin(miso, Pin.OUT)

        prog = _build_prog(self.bits, self.mode, sck, cs)
        self._sm = rp2.StateMachine(
            sm_id, prog, freq=SYS_HZ,
            out_base=miso_pin, in_base=Pin(mosi), jmp_pin=self._cs_pin)

        # MISO ring-DMA: a 256-word (1024-byte) counter table, MSB-justified
        # (i << 24) so the SHIFT_LEFT `out` emits bit7 first. Over-allocate and
        # align to 1024 bytes (the ring-wrap requirement); GC-heap buffers are
        # not aligned by default, so the table is carved out of a larger buffer
        # kept referenced (self._tx_buf) so the GC never moves or frees it.
        tx_bytes = 256 * 4
        self._tx_buf = bytearray(tx_bytes * 2)
        tx_addr = (uctypes.addressof(self._tx_buf) + tx_bytes - 1) & ~(tx_bytes - 1)
        for i in range(256):
            machine.mem32[tx_addr + i * 4] = i << 24

        self._tx = rp2.DMA()
        tx_ctrl = self._tx.pack_ctrl(
            size=2, inc_read=True, inc_write=False,
            ring_sel=0, ring_size=_log2_pow2(tx_bytes),
            treq_sel=_tx_dreq(sm_id))
        self._tx.config(read=tx_addr, write=_tx_fifo_addr(sm_id),
                        count=_COUNT0, ctrl=tx_ctrl, trigger=False)

        # MOSI capture ring: `size` bytes, one 32-bit word per received byte
        # (the low byte of each word is the data; see _captured). Same
        # over-allocate-and-align treatment as the TX table.
        rx_bytes = self.size * 4
        self._rx_buf = bytearray(rx_bytes * 2)
        self._rx_addr = (uctypes.addressof(self._rx_buf) + rx_bytes - 1) & ~(rx_bytes - 1)
        self._rx_view = uctypes.bytearray_at(self._rx_addr, rx_bytes)

        self._rx = rp2.DMA()
        rx_ctrl = self._rx.pack_ctrl(
            size=2, inc_read=False, inc_write=True,
            ring_sel=1, ring_size=_log2_pow2(rx_bytes),
            treq_sel=_rx_dreq(sm_id))
        self._rx.config(read=_rx_fifo_addr(sm_id), write=self._rx_addr,
                        count=_COUNT0, ctrl=rx_ctrl, trigger=False)

        # Pre-prime the TX FIFO before the SM runs, so the first `out`'s
        # autopull always finds data instead of stalling on an empty OSR.
        self._tx.active(1)
        self._rx.active(1)
        self._sm.active(1)

        # Belt-and-braces CS-boundary bookkeeping: soft IRQ (hard=False) so it
        # never runs in a hard-IRQ context, only integer arithmetic off the
        # data path. sm.restart() is a best-effort nudge for the mid-byte
        # clock-stop park (deasserting CS without further SCK edges leaves the
        # SM blocked in `wait 1 gpio SCK`, which a rising CS alone cannot
        # unblock); Stage 1 does not depend on its exact PC semantics; clean
        # re-sync is guaranteed only for whole-byte transactions and
        # byte-aligned aborts.
        self._cs_pin.irq(handler=self._on_cs_rising,
                         trigger=Pin.IRQ_RISING, hard=False)

    def _on_cs_rising(self, pin):
        now = _COUNT0 - self._rx.count
        self.last_cs_len = now - self._last_rx
        self._last_rx = now
        self.transfers_total += 1
        try:
            self._sm.restart()
        except Exception:
            pass

    def _captured(self, bx, wp):
        return [self._rx_view[e * 4] for e in ring_order(bx, self.size, wp)]

    def status(self):
        """Return {ok, mode, bits, bytes_rx, transfers_total, last_cs_len,
        size, captured}. `bytes_rx` is exact (the DMA transfer-count register).
        `captured` is the MOSI ring in chronological order, authoritative only
        after the transfer has ended; its length scales with `size` (up to
        `size` ints), so read it after a transaction rather than as a hot poll.
        The write-cursor and count are read adjacently so the derived byte count
        and ring position stay consistent."""
        wp = (self._rx.write - self._rx_addr) // 4
        bx = _COUNT0 - self._rx.count
        return {
            "ok": True, "mode": self.mode, "bits": self.bits,
            "bytes_rx": bx, "transfers_total": self.transfers_total,
            "last_cs_len": self.last_cs_len, "size": self.size,
            "captured": self._captured(bx, wp),
        }

    def deinit(self):
        """Stop the SM and DMA, free the PIO program, release the PIO0 claim.

        Best-effort at every step (mirrors LogicAnalyser._teardown): a failure
        partway through must not prevent the later steps, especially the
        pio_arbiter release, from running.
        """
        # Detach the CS IRQ first: once the RX DMA is closed and self._rx is
        # nulled below, a CS-rising edge would fault _on_cs_rising on a None
        # reference (soft IRQ, so only a print, but detaching first avoids it).
        if self._cs_pin is not None:
            try:
                self._cs_pin.irq(None)
            except Exception:
                pass
            self._cs_pin = None
        if self._sm is not None:
            try:
                self._sm.active(0)
            except Exception:
                pass
        if self._tx is not None:
            try:
                self._tx.active(0)
                self._tx.close()
            except Exception:
                pass
            self._tx = None
        if self._rx is not None:
            try:
                self._rx.active(0)
                self._rx.close()
            except Exception:
                pass
            self._rx = None
        try:
            rp2.PIO(self.sm_id // 4).remove_program()
        except Exception:
            pass
        self._sm = None
        pio_arbiter.release(self._name)
