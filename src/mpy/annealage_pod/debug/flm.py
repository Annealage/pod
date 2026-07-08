# Generic CMSIS flash-algorithm (FLM) runner (workstream D2.2).
#
# Runs a standard CMSIS flash algorithm on the target itself, the general path
# that works for any chip with a CMSIS pack, as opposed to the per-family native
# NVM path (flash_nrf52). The algorithm is a position-independent Thumb blob with
# fixed entry points (Init / EraseSector / ProgramPage / EraseChip), the same
# standard CMSIS-FLM contract, as Keil and CMSIS-DAP tooling use; we drive it through the MEM-AP + core registers:
#
#   1. load the blob into target SRAM at load_address;
#   2. per call: set R0..R3 = args, R9 = static_base (PIC data), SP = begin_stack,
#      LR = load_address|1 (the blob's first halfword is BKPT, so returning there
#      halts the core), PC = entry, xPSR = Thumb;
#   3. resume, wait for the BKPT halt, read R0 = status (0 = ok).
#
# The algorithm dict (see flm_nrf52840.py) carries the blob, entry points,
# begin_data / begin_stack / static_base, flash_base/size and page_size. Target
# data is produced host-side from a CMSIS Device Family Pack; on-device pack handling is
# out of scope.

import time

from .swd_dap import S_HALT, DHCSR, DBGKEY, C_DEBUGEN, C_HALT, C_MASKINTS


class FLMError(Exception):
    pass


class FLMFlasher:
    def __init__(self, memap, cortexm, algo):
        self.ap = memap
        self.cm = cortexm
        self.algo = algo
        self.page_size = algo["page_size"]
        self._loaded = False

    def load(self):
        a = self.algo
        if not self.cm.is_halted():
            self.cm.halt()
        self.ap.write_block32(a["load_address"], list(a["instructions"]))
        self._loaded = True

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
        if not self._loaded:
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
        r = self._call(self.algo["pc_erase_sector"], addr)
        if r:
            raise FLMError("EraseSector(0x%08x) returned %d" % (addr, r))

    def erase_all(self):
        # Erase the entire flash: load the blob, bracket with Init/UnInit(1),
        # call EraseChip if the algo supplies it, otherwise fall back to a full
        # sector-by-sector sweep. Mirrors program()'s load/init/uninit structure.
        a = self.algo
        self.load()
        self.init(1)                                  # operation 1 = erase
        try:
            if "pc_eraseAll" in a:
                r = self._call(a["pc_eraseAll"])
                if r:
                    raise FLMError("EraseChip returned %d" % r)
            else:
                # Generic fallback: sector sweep over the full flash region.
                page = a["page_size"]
                p = a["flash_base"] & ~(page - 1)   # page-align (cf _flm_erase_range)
                end = a["flash_base"] + a["flash_size"]
                while p < end:
                    self.erase_sector(p)
                    p += page
        finally:
            self.uninit(1)

    def program_page(self, addr, data):
        a = self.algo
        if len(data) % 4:
            data = data + b"\xff" * (4 - (len(data) % 4))
        words = [int.from_bytes(data[i:i + 4], "little")
                 for i in range(0, len(data), 4)]
        self.ap.write_block32(a["begin_data"], words)
        r = self._call(a["pc_program_page"], addr, len(data), a["begin_data"])
        if r:
            raise FLMError("ProgramPage(0x%08x) returned %d" % (addr, r))

    def program(self, addr, data, erase=True, verify=True):
        page = self.page_size
        self.load()
        if erase:
            self.init(1)                       # operation 1 = erase
            p = addr & ~(page - 1)
            end = addr + len(data)
            while p < end:
                self.erase_sector(p)
                p += page
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
