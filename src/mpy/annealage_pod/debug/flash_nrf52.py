# nRF52 native-NVM flash loader, driven through the MEM-AP (workstream D2.2/D2.4).
#
# The nRF52840 has no separate flash controller blob to load; its NVMC programs
# the on-chip flash directly. With the core halted we drive NVMC over the MEM-AP:
# enable erase, erase the covered pages, enable write, store 32-bit words to
# flash addresses (the NVMC commits each), then read back to verify. This is the
# simplest flash-control proof (D2.4 step 1) and the per-family fallback path for
# targets the generic CMSIS-FLM loader cannot drive.
#
# nRF52840 flash: 1 MB at 0x00000000, 4 KB pages, word (32-bit) writes only.

from .swd_dap import TransferError

# NVMC registers (nRF52840)
NVMC_BASE = 0x4001E000
NVMC_READY = NVMC_BASE + 0x400      # bit0: 1 = idle/ready
NVMC_READYNEXT = NVMC_BASE + 0x408  # bit0: 1 = ready for next write
NVMC_CONFIG = NVMC_BASE + 0x504     # WEN[1:0]
NVMC_ERASEPAGE = NVMC_BASE + 0x508  # write page base addr to erase
NVMC_ERASEALL = NVMC_BASE + 0x50C   # write 1 to erase all of code + UICR
NVMC_ERASEUICR = NVMC_BASE + 0x514

CONFIG_REN = 0   # read-only
CONFIG_WEN = 1   # write enable
CONFIG_EEN = 2   # erase enable

PAGE_SIZE = 0x1000        # 4 KB
FLASH_BASE = 0x00000000
FLASH_SIZE = 0x100000     # 1 MB
ERASED = 0xFFFFFFFF


class NRF52Flash:
    def __init__(self, memap, cortexm, page_size=PAGE_SIZE):
        self.ap = memap
        self.cm = cortexm
        self.page_size = page_size

    def _wait(self, reg=NVMC_READY, timeout=100000):
        for _ in range(timeout):
            if self.ap.read32(reg) & 1:
                return
        raise TransferError("NVMC not ready (reg 0x%08x)" % reg)

    def _config(self, mode):
        self.ap.write32(NVMC_CONFIG, mode)
        self._wait()

    def prepare(self):
        # Halt the core so it is not executing from flash during the operation.
        if not self.cm.is_halted():
            self.cm.halt()
        self._wait()

    def erase_page(self, addr):
        if addr % self.page_size:
            raise ValueError("page addr 0x%08x not page-aligned" % addr)
        self._config(CONFIG_EEN)
        self.ap.write32(NVMC_ERASEPAGE, addr)
        self._wait()
        self._config(CONFIG_REN)

    def erase_range(self, addr, length):
        # Erase every page touched by [addr, addr+length).
        if addr % self.page_size:
            raise ValueError("erase addr not page-aligned")
        self._config(CONFIG_EEN)
        end = addr + length
        p = addr
        while p < end:
            self.ap.write32(NVMC_ERASEPAGE, p)
            self._wait()
            p += self.page_size
        self._config(CONFIG_REN)

    def mass_erase(self):
        self._config(CONFIG_EEN)
        self.ap.write32(NVMC_ERASEALL, 1)
        self._wait()
        self._config(CONFIG_REN)

    def write_words(self, addr, words):
        # Caller must have erased the target region first (NVMC only clears
        # bits). Words stream through the MEM-AP with TAR auto-increment (one
        # TAR per 1 KB window, re-armed by write_block32), not one TAR+DRW pair
        # per word. We do NOT poll READYNEXT per word: each MEM-AP write over
        # SWD already takes far longer than the ~41 us flash write, so the NVMC
        # buffer is always drained by the next write; a single READY wait at the
        # end confirms the last write committed.
        if addr % 4:
            raise ValueError("write addr not word-aligned")
        self._config(CONFIG_WEN)
        try:
            self.ap.write_block32(addr, words)
            self._wait(NVMC_READY)
        finally:
            self._config(CONFIG_REN)

    # --- byte-oriented, bounded-memory image programming ---
    @staticmethod
    def _chunk_words(data, off, n):
        # Pack up to n little-endian words from data starting at byte offset off,
        # padding a trailing partial word with 0xFF (the erased value).
        words = []
        end = min(off + n * 4, len(data))
        i = off
        while i < end:
            chunk = data[i:i + 4]
            if len(chunk) < 4:
                chunk = chunk + b"\xff" * (4 - len(chunk))
            words.append(int.from_bytes(chunk, "little"))
            i += 4
        return words

    def program(self, addr, data, erase=True, verify=True, chunk_words=256):
        # Stream the image in bounded page-sized word chunks so neither the pod
        # heap nor a single SWD burst holds the whole image (large block lists
        # otherwise exhaust the pod's RAM under socket+mount).
        if addr % 4:
            raise ValueError("program addr not word-aligned")
        self.prepare()
        nwords = (len(data) + 3) // 4
        if erase:
            start = addr & ~(self.page_size - 1)
            span = ((addr & (self.page_size - 1)) + len(data)
                    + self.page_size - 1) & ~(self.page_size - 1)
            self.erase_range(start, span)
        w = 0
        while w < nwords:
            n = min(chunk_words, nwords - w)
            words = self._chunk_words(data, w * 4, n)
            self.write_words(addr + w * 4, words)
            if verify:
                self._verify_chunk(addr + w * 4, words)
            w += n
        return True

    def _verify_chunk(self, addr, words):
        read = self.ap.read_block32(addr, len(words))
        for i in range(len(words)):
            if read[i] != words[i]:
                raise TransferError(
                    "verify mismatch at 0x%08x: got 0x%08x want 0x%08x"
                    % (addr + 4 * i, read[i], words[i]))
        return True

    def verify_words(self, addr, words, chunk_words=256):
        # Chunked compare; never materialises more than chunk_words at a time.
        w = 0
        n = len(words)
        while w < n:
            c = min(chunk_words, n - w)
            self._verify_chunk(addr + w * 4, words[w:w + c])
            w += c
        return True
