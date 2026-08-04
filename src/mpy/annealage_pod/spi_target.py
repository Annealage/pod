# PIO SPI target for the Annealage Pod (RP2350), workstream F5.1. The pod is
# the SPI PERIPHERAL; the DUT is the controller.
#
# Built the same way as the logic analyser (debug/logic_analyser.py): an
# @rp2.asm_pio program assembled per config, an rp2.StateMachine on PIO0, and
# rp2.DMA channels moving the byte streams so no MicroPython code sits in the
# per-byte data path (the single-core cooperative runtime cannot afford a
# per-byte pump). SPI modes 0-3, 8-bit, MSB-first. Two personalities:
#   - stream (default): MISO is a ring-DMA replaying a 0..255 counter table
#     (unbounded transfer length, no growing buffer); MOSI is captured into a
#     bounded overwrite ring.
#   - regfile: a [reg_ptr][data...] register-file responder (see
#     docs/esp32-s3/design/slaveio.md section 4). A WRITE CS transaction
#     stores its payload into write_table[reg_ptr+] and repoints reg_ptr; a
#     FOLLOWING READ CS serves read_table[reg_ptr+]. The pointer is only ever
#     moved by the CS-deassert soft IRQ, never mid-transfer; a same-CS
#     write-then-read turnaround is out of scope (it would need a mid-CS
#     TX-DMA repoint the single-core runtime cannot afford). See
#     _regtable.py for the pure reg_ptr/wrap protocol logic.
#
# PIO budget: PIO0 is the free block (annealage_pod.debug.pio_arbiter.PIO_MAP);
# PIO1 is the SWD transport, PIO2 is CYW43 Wi-Fi and must never be touched.
# The SPI target and the logic analyser are mutually exclusive on PIO0 - both
# claim PIO0 through pio_arbiter, so a live one blocks the other (PioConflict)
# rather than silently sharing the block.
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
from ._regtable import parse_cmd, apply_write

SYS_HZ = 150_000_000

# DMA transfer-count seed: the RP2350 normal-mode max (2^28 - 1). This is a
# large FIXED count, not self-restarting: both DMAs count down from here and
# stop at zero (after which MISO stalls on an empty OSR and RX capture halts).
# 2^28-1 bytes is far past any bench transfer (a > 65535-byte stream uses a
# fraction of it), and it stays a MicroPython small int so the byte-count
# arithmetic on the IRQ/status path never allocates. Truly-unbounded streaming
# would need the channel chained to itself (chain_to + reload); that is Stage 2.
_COUNT0 = 0x0FFFFFFF

# SM0_SHIFTCTRL FJOIN_RX bit. Toggling it (any FJOIN change) is the only
# MP-reachable way to clear both PIO FIFOs (StateMachine exposes no
# FIFO-clear method); this is the mechanism pio_sm_clear_fifos uses.
_FJOIN = 0x80000000


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
    """Assemble the SPI-target program for `bits`-wide frames in SPI mode 0-3.

    `sck`/`cs` are the literal GPIO numbers (assembly-time constants, like the
    logic analyser's trigger pin) used by the absolute `wait ... gpio` checks;
    the CS abort check instead uses the SM's configured jmp_pin (also wired to
    `cs`), which is set on the StateMachine, not assembled here. CPOL/CPHA
    (derived from `mode`) select the sample/drive edges and the loop shape at
    assembly time, exactly like the logic analyser's trigger assembly: the
    Python `if` below runs once per build, so each mode compiles to just the
    instructions it needs.
    """
    cpol = mode >> 1
    cpha = mode & 1

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
        if cpha == 0:
            # CPHA0 (modes 0, 2): present MISO before the leading edge, sample
            # MOSI on the leading edge, MISO may change after the trailing
            # edge. SCK idles at CPOL between bytes.
            out(pins, 1)
            wait(1 - cpol, gpio, sck)  # leading edge: sample MOSI
            in_(pins, 1)
            wait(cpol, gpio, sck)      # trailing edge
        else:
            # CPHA1 (modes 1, 3): MISO changes on the leading edge, sample on
            # the trailing edge.
            wait(1 - cpol, gpio, sck)  # leading edge: drive next MISO bit
            out(pins, 1)
            wait(cpol, gpio, sck)      # trailing edge: sample MOSI
            in_(pins, 1)
        jmp(pin, "resync")         # jmp_pin=CS high (deasserted): close the frame
        jmp("bit")                 # else next bit, same CS assertion
        wrap()

    return prog


class SpiTarget:
    """A PIO SPI target on one PIO0 state machine: stream or regfile personality.

    stream (default): MISO drives a ring-DMA over a 256-byte 0..255 counter
    table (RP2350 DMA read-address ring), so the controller reads a
    predictable 0,1,...,255,0,... counter for any transfer length. MOSI is
    captured by DMA into a bounded overwrite ring of `size` bytes (rounded up
    to a power of two, a ring requirement). `bytes_rx` (from the free DMA
    transfer-count register) is the primary verification metric; `captured`
    exposes the ring tail for spot checks.

    No rolling checksum: the MOSI ring is a bounded overwrite ring filled by
    DMA writing RAM directly, with no MicroPython code in the data path: there
    is no way to fold every received byte into a running checksum without a
    per-byte drain, which would put a Python loop back in the data path and
    violate the single-core cooperative-loop constraint. `bytes_rx` (exact)
    plus the captured ring tail is the stream-mode verification surface
    instead. A zero-cost checksum is possible later via the RP2350 DMA CRC
    sniffer (pack_ctrl sniff_en=True + the DMA_SNIFF_CTRL/DATA registers),
    deferred.

    regfile: a [reg_ptr][data...] register-file responder over `table_size`
    bytes each way (see the module docstring and _regtable.py). MOSI capture
    is the same overwrite-ring DMA as stream mode; the CS-boundary soft IRQ
    additionally parses the just-captured transaction's reg_ptr, applies any
    write to write_table, and repoints the MISO ring-DMA to read_table[offset]
    for the next read CS. `regs()` gives host-side read/write access to
    either table.

    sm_id 0..3 are PIO0 (the only block this class will build on).
    """

    def __init__(self, miso, mosi, sck, cs, mode=0, bits=8, size=1024, sm_id=0,
                 personality="stream", table_size=256, name="spi_target"):
        if not (0 <= sm_id <= 3):
            raise ValueError(
                "sm_id must be 0..3 (PIO0 only; see "
                "annealage_pod.debug.pio_arbiter.PIO_MAP)")
        if not (0 <= mode <= 3):
            raise ValueError("SPI mode must be 0-3, got %d" % mode)
        if bits != 8:
            raise ValueError("%d-bit frames are not supported (8-bit only)" % bits)
        if personality not in ("stream", "regfile"):
            raise ValueError(
                "personality must be 'stream' or 'regfile', got %r" % (personality,))
        if not (1 <= table_size <= 4096):
            raise ValueError("table_size must be 1..4096, got %d" % table_size)

        self.sm_id = sm_id
        self._name = name
        self.mode = mode
        self.bits = bits
        self._personality = personality
        self.table_size = _next_pow2(table_size)
        self.size = _next_pow2(size)
        if personality == "regfile":
            # The RX ring must retain byte0 (reg_ptr) of a full transaction
            # (cmd + up to table_size payload bytes), so grow it to cover the
            # widest in-spec transaction rather than just the caller's `size`.
            self.size = _next_pow2(max(self.size, self.table_size + 1))
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
        self._overflow = 0
        self.reg_ptr = 0
        self.last_offset = 0
        self.last_length = 0
        self.last_was_write = False
        self._sm = None
        self._tx = None
        self._rx = None
        self._cs_pin = None
        self._read_addr = None
        self._read_buf = None
        self._write_table = None
        self._prog = None      # the loaded PIO program, for per-program teardown

        # Claim the PIO block under THIS instance's name before touching any
        # hardware: a second SPI target (or a logic-analyser capture) on the
        # same block then raises PioConflict instead of silently building a
        # second state machine over this one. claim() is idempotent per owner,
        # so each live instance must claim under a distinct name.
        pio_arbiter.claim(self._name, sm_id // 4, sm_id % 4)
        try:
            self._build(miso, mosi, sck, cs)
        except Exception:
            # Full teardown, not just the arbiter release: _build allocates DMA
            # channels (self._tx, then self._rx) partway before it can fail
            # (ENOMEM on a buffer, a late validation). rp2.DMA has no GC
            # finalizer, so a channel left allocated here leaks its claim
            # permanently (until reboot) and, if it reached active(1), keeps
            # driving the PIO FIFOs. deinit() is fully guarded and idempotent on
            # a half-built instance, and releases the claim itself.
            self.deinit()
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
        self._prog = prog      # track it so deinit removes only this program

        if self._personality == "regfile":
            # Register-file MISO table: a `table_size`-word MSB-justified
            # array (word[i] = read_table[i] << 24, zero-init), served by a
            # ring-read DMA. read_addr is aligned to table_size*4, so the
            # DMA's read-address ring wraps the low log2(table_size*4) bits:
            # tx.read = read_addr + reg_ptr*4 makes the DMA serve
            # read_table[reg_ptr], [reg_ptr+1], ..., wrapping modulo
            # table_size - the regtable read contract. reg_ptr only ever
            # changes while this DMA is idle, in the CS-boundary IRQ
            # (_arm_read), never mid-transfer. This word array is the one
            # storage for the read side (no shadow bytearray copy); MP
            # byte-lane access goes through machine.mem32 (see regs()).
            table_size = self.table_size
            tx_bytes = table_size * 4
            self._read_buf = bytearray(tx_bytes * 2)
            self._read_addr = (uctypes.addressof(self._read_buf) + tx_bytes - 1) & ~(tx_bytes - 1)
            for i in range(table_size):
                machine.mem32[self._read_addr + i * 4] = 0

            self._write_table = bytearray(table_size)

            self._tx = rp2.DMA()
            tx_ctrl = self._tx.pack_ctrl(
                size=2, inc_read=True, inc_write=False,
                ring_sel=0, ring_size=_log2_pow2(tx_bytes),
                treq_sel=_tx_dreq(sm_id))
            self._tx.config(read=self._read_addr, write=_tx_fifo_addr(sm_id),
                            count=_COUNT0, ctrl=tx_ctrl, trigger=False)
        else:
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
        # data path. In stream mode, sm.restart() is a best-effort nudge for
        # the mid-byte clock-stop park (deasserting CS without further SCK
        # edges leaves the SM blocked in `wait 1 gpio SCK`, which a rising CS
        # alone cannot unblock); it does not depend on the SM's exact PC
        # semantics, so clean re-sync is guaranteed only for whole-byte
        # transactions and byte-aligned aborts. regfile mode additionally
        # parses the transaction and repoints MISO (_regfile_boundary).
        self._cs_pin.irq(handler=self._on_cs_rising,
                         trigger=Pin.IRQ_RISING, hard=False)

    def _on_cs_rising(self, pin):
        now = _COUNT0 - self._rx.count
        self.last_cs_len = now - self._last_rx
        self._last_rx = now
        self.transfers_total += 1
        if self._personality == "regfile":
            self._regfile_boundary(now)
        else:
            try:
                self._sm.restart()
            except Exception:
                pass

    def _regfile_boundary(self, now):
        """Parse the just-completed CS transaction's reg_ptr and repoint MISO.

        Runs once per CS in the CS-deassert soft IRQ, never per byte:
        extracts this transaction's captured MOSI bytes from the RX ring,
        applies a write (if any) to write_table, and repoints the MISO
        ring-DMA to serve read_table[offset+] for the next read CS. See
        _regtable.py and the module docstring for the [reg_ptr][data...]
        protocol.
        """
        n = self.last_cs_len
        if n < 1:
            return
        size = self.size
        if n > size:
            # byte0 (the command) has already been overwritten by later bytes
            # of this over-long transaction; the parse below would read the
            # wrong byte. This cannot happen for in-spec transactions (the RX
            # ring is sized to cover table_size + 1 bytes), so count it and
            # clamp to the ring's actual capacity.
            self._overflow += 1
            n = size
        first = (now - n) % size
        mosi = [self._rx_view[((first + k) % size) * 4] & 0xFF for k in range(n)]
        offset, is_read = parse_cmd(mosi[0])
        # The command byte's offset field is 7 bits (0..127) independent of
        # table_size, so it must be reduced modulo table_size before use: a
        # table smaller than the offset field would otherwise point the TX
        # DMA outside its aligned ring window (_arm_read sets the DMA read
        # address directly, with no hardware wrap on that write).
        offset %= self.table_size
        if not is_read and n > 1:
            apply_write(self._write_table, offset, mosi[1:], self.table_size)
        self._arm_read(offset)
        self.reg_ptr = offset
        self.last_offset = offset
        self.last_length = n
        self.last_was_write = not is_read

    def _arm_read(self, offset):
        """Repoint the MISO TX DMA to serve read_table[offset+] next read CS.

        Ordered so the SM re-syncs cleanly: stop the TX DMA and the SM,
        flush the stale TX FIFO (the SM shifts MISO on every clocked byte,
        including write CSs, so the FIFO/OSR always hold stale data at the
        boundary), reconfigure and re-arm the TX DMA, then restart the SM
        from its program start (PC, OSR/ISR, and shift counters all reset).
        CS is deasserted (no SCK) for the whole sequence, so it completes
        before the next CS assert.
        """
        self._tx.active(0)
        self._sm.active(0)
        self._clear_tx_fifo()
        self._tx.read = self._read_addr + offset * 4
        self._tx.count = _COUNT0
        self._tx.active(1)
        self._sm.restart()
        self._sm.active(1)

    def _clear_tx_fifo(self):
        """Flush both PIO FIFOs via the FJOIN toggle.

        StateMachine exposes no FIFO-clear method; toggling FJOIN (any
        change) clears both TX and RX FIFOs, the mechanism
        pio_sm_clear_fifos uses. Only safe to call while the SM is stopped.
        """
        sc = _pio_base(self.sm_id // 4) + 0xD0 + (self.sm_id % 4) * 0x18
        machine.mem32[sc] ^= _FJOIN
        machine.mem32[sc] ^= _FJOIN

    def _captured(self, bx, wp):
        return [self._rx_view[e * 4] for e in ring_order(bx, self.size, wp)]

    def status(self):
        """Return {ok, mode, bits, bytes_rx, transfers_total, last_cs_len,
        size, captured, personality}, plus in regfile mode {table_size,
        reg_ptr, last_offset, last_length, last_was_write, overflow_count}.
        `bytes_rx` is exact (the DMA transfer-count register). `captured` is
        the MOSI ring in chronological order, authoritative only after the
        transfer has ended; its length scales with `size` (up to `size`
        ints), so read it after a transaction rather than as a hot poll.
        The write-cursor and count are read adjacently so the derived byte count
        and ring position stay consistent."""
        wp = (self._rx.write - self._rx_addr) // 4
        bx = _COUNT0 - self._rx.count
        d = {
            "ok": True, "mode": self.mode, "bits": self.bits,
            "bytes_rx": bx, "transfers_total": self.transfers_total,
            "last_cs_len": self.last_cs_len, "size": self.size,
            "captured": self._captured(bx, wp),
            "personality": self._personality,
        }
        if self._personality == "regfile":
            d["table_size"] = self.table_size
            d["reg_ptr"] = self.reg_ptr
            d["last_offset"] = self.last_offset
            d["last_length"] = self.last_length
            d["last_was_write"] = self.last_was_write
            d["overflow_count"] = self._overflow
        return d

    def regs(self, table, off=0, length=None, write=None):
        """Read or write a regfile backing table from the pod side.

        `table` is 'read' or 'write'. With `write` set (an iterable of
        bytes), write it at `off` first, wrapping is NOT applied here (an
        out-of-range write is dropped, mirroring i2c_target_regs); then
        return the window [off:off+length] (length defaults to the rest of
        the table). The read table is the DMA word array (byte-lane access
        via machine.mem32, the one storage for the read side); the write
        table is the plain bytearray the CS-boundary IRQ fills.

        `off`/`length` are clamped to [0, size) before the read-table
        machine.mem32 loop below: unlike the write-table bytearray slice
        (which clamps on its own), a raw mem32 address is not bounds-checked
        by the runtime, so an unclamped caller-supplied length or a
        negative/over-large off would read outside `_read_buf` - at best
        disclosing adjacent RAM, at worst faulting on an unmapped address.
        """
        if self._personality != "regfile":
            raise ValueError(
                "spi target %r is not in regfile personality" % self._name)
        if table not in ("read", "write"):
            raise ValueError("table must be 'read' or 'write', got %r" % (table,))
        size = self.table_size
        if off < 0:
            off = 0
        if length is None:
            length = size - off
        length = max(0, min(length, size - off))
        if table == "write":
            buf = self._write_table
            if write is not None:
                for i, b in enumerate(write):
                    if off + i < size:
                        buf[off + i] = b & 0xFF
            return list(buf[off:off + length])
        if write is not None:
            for i, b in enumerate(write):
                if off + i < size:
                    machine.mem32[self._read_addr + (off + i) * 4] = (b & 0xFF) << 24
        return [(machine.mem32[self._read_addr + (off + i) * 4] >> 24) & 0xFF
                for i in range(length)]

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
        # Remove ONLY this SPI target's program, so a co-tenant on another SM of
        # the same block (a logic-analyser capture on PIO0) keeps its program. The
        # no-arg remove_program() wipes the whole block and must never be used here.
        if self._prog is not None:
            try:
                rp2.PIO(self.sm_id // 4).remove_program(self._prog)
            except Exception:
                pass
            self._prog = None
        self._sm = None
        self._read_addr = None
        self._read_buf = None
        self._write_table = None
        pio_arbiter.release(self._name)
