# Generic CMSIS flash-algorithm (FLM) runner (workstream D2.2).
#
# Runs a standard CMSIS flash algorithm on the target itself, the general path
# that works for any chip with a CMSIS pack, as opposed to the per-family native
# NVM path (flash_nrf52). The algorithm is a position-independent Thumb blob with
# fixed entry points (Init / UnInit / EraseSector / EraseChip / ProgramPage), the
# standard CMSIS-FLM contract (ARM's FlashOS.H); we drive it through the MEM-AP +
# core registers:
#
#   1. load the blob into target SRAM at load_address;
#   2. per call: set R0..R3 = args, R9 = static_base (PIC data), SP = begin_stack,
#      LR = load_address|1 (the blob's first halfword is BKPT, so returning there
#      halts the core), PC = entry, xPSR = Thumb;
#   3. resume, wait for the BKPT halt, read R0 = status (0 = ok).
#
# The algorithm dict is supplied by the host, which extracts it from the target's
# CMSIS Device Family Pack on demand; no algorithms are carried on the pod. Keys:
#
#   instructions    the blob, as bytes (or a sequence of 32-bit words)
#   load_address    target SRAM address the blob is loaded at
#   static_base     R9 for the algorithm's PIC data
#   begin_stack     SP for algorithm calls
#   begin_data      target SRAM buffer ProgramPage reads its payload from
#   pc_init, pc_unInit, pc_erase_sector, pc_program_page   entry addresses
#   pc_eraseAll     optional; absent means erase_all() sweeps sectors instead
#   flash_base, flash_size, page_size    device geometry (FlashDevice)
#   sectors         optional erase-sector map, ascending
#                   [(offset_from_flash_base, sector_size), ...]; each entry
#                   applies until the next one's offset. Absent means uniform
#                   sectors of page_size.
#   timeout_prog_ms, timeout_erase_ms   optional, from the pack's FlashDevice;
#                   None/absent falls back to _call's default.
#
# page_size is the *program* granularity (FlashDevice.szPage) and the sector map
# is the *erase* granularity; they differ on most parts outside the nRF52, so
# erase addresses come from the sector map, never from page_size.

import time

from .swd_dap import S_HALT, DHCSR, DBGKEY, C_DEBUGEN, C_HALT, C_MASKINTS


class FLMError(Exception):
    pass


def _to_words(instructions):
    # The blob arrives as bytes (the host ships it base64-encoded so it stays
    # compact over the REPL); a sequence of 32-bit words is also accepted.
    if isinstance(instructions, (bytes, bytearray)):
        pad = -len(instructions) % 4
        if pad:
            instructions = bytes(instructions) + b"\x00" * pad
        return [int.from_bytes(instructions[i:i + 4], "little")
                for i in range(0, len(instructions), 4)]
    return list(instructions)


class FLMFlasher:
    def __init__(self, memap, cortexm, algo):
        self.ap = memap
        self.cm = cortexm
        self.algo = algo
        self.page_size = algo["page_size"]
        # Normalise the erase-sector map once: ascending [(offset, size), ...].
        sectors = algo.get("sectors")
        if not sectors:
            sectors = [(0, self.page_size)]
        self.sectors = sorted((int(o), int(s)) for o, s in sectors)
        self._loaded = False

    # ── blob loading ─────────────────────────────────────────────────────

    def load(self):
        # Write the algorithm into target SRAM. Idempotent: the blob stays
        # resident for the life of this flasher, so a multi-page program() does
        # not re-upload it per page. reload() forces a fresh copy.
        if self._loaded:
            return
        a = self.algo
        if not self.cm.is_halted():
            self.cm.halt()
        self.ap.write_block32(a["load_address"], _to_words(a["instructions"]))
        self._loaded = True

    def reload(self):
        # Re-write the blob, restoring the algorithm's initialised RW data. Only
        # needed if the target's SRAM has been disturbed since load() (a reset,
        # or the DUT running its own firmware over the load region).
        self._loaded = False
        self.load()

    # ── sector geometry ──────────────────────────────────────────────────

    def sector_size(self, addr):
        # Erase-sector size covering addr, from the CMSIS sector map.
        off = addr - self.algo["flash_base"]
        if off < 0:
            raise FLMError("addr 0x%08x below flash_base" % addr)
        size = None
        for s_off, s_size in self.sectors:
            if off >= s_off:
                size = s_size
            else:
                break
        if not size:
            raise FLMError("no sector covers 0x%08x" % addr)
        return size

    def sector_base(self, addr):
        # Base address of the erase sector containing addr. Sector runs are
        # uniform within a map entry, so align relative to that entry's start.
        off = addr - self.algo["flash_base"]
        entry_off, size = 0, None
        for s_off, s_size in self.sectors:
            if off >= s_off:
                entry_off, size = s_off, s_size
            else:
                break
        if not size:
            raise FLMError("no sector covers 0x%08x" % addr)
        return self.algo["flash_base"] + entry_off + ((off - entry_off) // size) * size

    # ── algorithm calls ──────────────────────────────────────────────────

    def _call(self, pc, r0=0, r1=0, r2=0, r3=0, timeout_ms=8000):
        a = self.algo
        cm = self.cm
        cm.write_core_reg(0, r0)
        cm.write_core_reg(1, r1)
        cm.write_core_reg(2, r2)
        cm.write_core_reg(3, r3)
        cm.write_core_reg(9, a["static_base"])      # static base for PIC data
        cm.write_core_reg(13, a["begin_stack"])     # SP
        cm.write_core_reg(14, a["load_address"] | 1)  # LR -> BKPT trampoline
        cm.write_core_reg(15, pc)                    # PC
        cm.write_core_reg(16, 0x01000000)            # xPSR: Thumb bit
        # Resume with interrupts masked (C_MASKINTS): the algo runs with the
        # target's own vector table, so an interrupt would vector into the
        # target's firmware and never return to the BKPT trampoline. C_MASKINTS
        # must be set while halted and held across the resume; changing it in
        # the same write that clears C_HALT is UNPREDICTABLE (ARMv7-M), so do it
        # in two writes.
        self.ap.write32(DHCSR, DBGKEY | C_DEBUGEN | C_HALT | C_MASKINTS)
        self.ap.write32(DHCSR, DBGKEY | C_DEBUGEN | C_MASKINTS)
        t0 = time.ticks_ms()
        while not (cm.read_dhcsr() & S_HALT):
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                cm.halt()
                raise FLMError("algo call timed out at pc=0x%08x" % pc)
        return cm.read_core_reg(0)

    def init(self, fnc, addr=None, clk=0):
        a = self.algo
        self.load()
        if "pc_init" in a:
            base = a["flash_base"] if addr is None else addr
            r = self._call(a["pc_init"], base, clk, fnc)
            if r:
                raise FLMError("Init(fnc=%d) returned %d" % (fnc, r))

    def uninit(self, fnc):
        if "pc_unInit" in self.algo:
            self._call(self.algo["pc_unInit"], fnc)

    def erase_sector(self, addr):
        r = self._call(self.algo["pc_erase_sector"], addr,
                       timeout_ms=self.algo.get("timeout_erase_ms") or 8000)
        if r:
            raise FLMError("EraseSector(0x%08x) returned %d" % (addr, r))

    def erase_range(self, addr, length):
        # Erase every sector covering [addr, addr+length), walking the sector
        # map so non-uniform devices step by the right size. The caller supplies
        # the Init(1)/UnInit(1) bracket.
        p = self.sector_base(addr)
        end = addr + length
        while p < end:
            self.erase_sector(p)
            p += self.sector_size(p)

    def erase_all(self):
        # Erase the entire flash: load the blob, bracket with Init/UnInit(1),
        # call EraseChip if the algo supplies it, otherwise sweep every sector.
        a = self.algo
        self.load()
        self.init(1)                                  # operation 1 = erase
        try:
            if "pc_eraseAll" in a:
                r = self._call(a["pc_eraseAll"],
                               timeout_ms=a.get("timeout_erase_ms") or 8000)
                if r:
                    raise FLMError("EraseChip returned %d" % r)
            else:
                self.erase_range(a["flash_base"], a["flash_size"])
        finally:
            self.uninit(1)

    def program_page(self, addr, data):
        a = self.algo
        if len(data) % 4:
            data = data + b"\xff" * (4 - (len(data) % 4))
        words = [int.from_bytes(data[i:i + 4], "little")
                 for i in range(0, len(data), 4)]
        self.ap.write_block32(a["begin_data"], words)
        r = self._call(a["pc_program_page"], addr, len(data), a["begin_data"],
                       timeout_ms=a.get("timeout_prog_ms") or 8000)
        if r:
            raise FLMError("ProgramPage(0x%08x) returned %d" % (addr, r))

    def program(self, addr, data, erase=True, verify=True):
        page = self.page_size
        self.load()
        if erase:
            self.init(1)                       # operation 1 = erase
            try:
                self.erase_range(addr, len(data))
            finally:
                self.uninit(1)
        self.init(2)                           # operation 2 = program
        off = 0
        while off < len(data):
            chunk = data[off:off + page]
            self.program_page(addr + off, chunk)
            off += len(chunk)
        self.uninit(2)
        if verify:
            self.verify(addr, data)
        return True

    def verify(self, addr, data, chunk_words=256):
        n = (len(data) + 3) // 4
        w = 0
        while w < n:
            c = min(chunk_words, n - w)
            read = self.ap.read_block32(addr + w * 4, c)
            for i in range(c):
                chunk = data[(w + i) * 4:(w + i) * 4 + 4]
                if len(chunk) < 4:
                    chunk = chunk + b"\xff" * (4 - len(chunk))
                want = int.from_bytes(chunk, "little")
                if read[i] != want:
                    raise FLMError(
                        "verify mismatch at 0x%08x: got 0x%08x want 0x%08x"
                        % (addr + (w + i) * 4, read[i], want))
            w += c
        return True
