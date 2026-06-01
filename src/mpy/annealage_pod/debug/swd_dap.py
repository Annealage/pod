# ADIv5 DP / AP / MEM-AP and Cortex-M debug control over the PIO SWD transport.
#
# Implemented from the ARM ADIv5 (IHI0031) and ARMv7-M (DDI0403) reference manuals, trimmed to what the
# pod's flasher needs and kept structurally close so behaviour is familiar:
#   DebugPort : line bring-up, DPIDR, power handshake, SELECT banking, sticky
#               error / ABORT recovery, posted AP reads via RDBUFF.
#   MEMAP     : 8/16/32-bit single access and 32-bit block access with TAR
#               auto-increment (re-armed at the 1 KB boundary), through AP 0.
#   CortexM   : halt / resume / reset-and-halt / step via DHCSR + AIRCR + DEMCR.
#
# All multi-byte values are little-endian on the wire (SWD) and host-order ints
# here. Errors raise TransferError with the failing ACK so callers can recover.

from . import swd_pio
from .swd_pio import SWD_OK, SWD_WAIT, SWD_FAULT

# --- DP registers (addr is the 4-bit A[3:2]<<2 offset; bank via SELECT) ------
DP_DPIDR = 0x0       # read
DP_ABORT = 0x0       # write
DP_CTRL_STAT = 0x4   # DPBANKSEL=0
DP_SELECT = 0x8      # write
DP_RDBUFF = 0xC      # read

# DP CTRL/STAT bits
CSYSPWRUPACK = 1 << 31
CSYSPWRUPREQ = 1 << 30
CDBGPWRUPACK = 1 << 29
CDBGPWRUPREQ = 1 << 28
STICKYERR = 1 << 5
STICKYCMP = 1 << 4
STICKYORUN = 1 << 1
# DP ABORT bits
ORUNERRCLR = 1 << 4
WDERRCLR = 1 << 3
STKERRCLR = 1 << 2
STKCMPCLR = 1 << 1
DAPABORT = 1 << 0
ABORT_CLEAR_ALL = ORUNERRCLR | WDERRCLR | STKERRCLR | STKCMPCLR  # 0x1E

PWRUP_REQ = CSYSPWRUPREQ | CDBGPWRUPREQ            # 0x50000000
PWRUP_ACK = CSYSPWRUPACK | CDBGPWRUPACK            # 0xA0000000

# --- MEM-AP registers (AP address offsets) -----------------------------------
AP_CSW = 0x00
AP_TAR = 0x04
AP_DRW = 0x0C
AP_IDR = 0xFC

# CSW: base debug flags | size[2:0] | (addrinc[1:0] << 4)
CSW_BASE = 0x23000000
CSW_SIZE8 = 0x0
CSW_SIZE16 = 0x1
CSW_SIZE32 = 0x2
CSW_NADDRINC = 0x0
CSW_SADDRINC = 0x1 << 4   # single auto-increment
CSW_WORD = CSW_BASE | CSW_SIZE32 | CSW_NADDRINC          # 0x23000002
CSW_WORD_INC = CSW_BASE | CSW_SIZE32 | CSW_SADDRINC      # 0x23000012

TAR_INC_BOUNDARY = 0x400  # auto-increment guaranteed only within 1 KB

# --- Cortex-M debug registers (memory-mapped, read via MEM-AP) ----------------
DHCSR = 0xE000EDF0
DCRSR = 0xE000EDF4
DCRDR = 0xE000EDF8
DEMCR = 0xE000EDFC
AIRCR = 0xE000ED0C
CPUID = 0xE000ED00

DBGKEY = 0xA05F << 16
C_DEBUGEN = 1 << 0
C_HALT = 1 << 1
C_STEP = 1 << 2
C_MASKINTS = 1 << 3
S_REGRDY = 1 << 16
S_HALT = 1 << 17
S_LOCKUP = 1 << 19

DEMCR_VC_CORERESET = 1 << 0
DEMCR_TRCENA = 1 << 24
AIRCR_VECTKEY = 0x05FA << 16
AIRCR_SYSRESETREQ = 1 << 2
AIRCR_VECTRESET = 1 << 0


class TransferError(Exception):
    def __init__(self, msg, ack=None):
        super().__init__(msg)
        self.ack = ack


def _ackname(ack):
    return {SWD_OK: "OK", SWD_WAIT: "WAIT", SWD_FAULT: "FAULT"}.get(ack, "ack=%d" % ack)


class DebugPort:
    def __init__(self, swd=None, **kw):
        self.swd = swd if swd is not None else swd_pio.SWDPio(**kw)
        self._select = None   # cached DP SELECT value
        self.dpidr = None

    # --- low-level DP/AP with error raising ---
    def _unwrap(self, ack, val, what):
        if ack != SWD_OK:
            raise TransferError("%s: %s" % (what, _ackname(ack)), ack)
        if isinstance(val, tuple):  # ("PARITY", value)
            raise TransferError("%s: parity error (val=0x%08x)" % (what, val[1]))
        return val

    def read_dp(self, addr):
        return self._unwrap(*self.swd.read(0, addr), what="read_dp 0x%x" % addr)

    def write_dp(self, addr, value):
        ack = self.swd.write(0, addr, value)
        if ack != SWD_OK:
            raise TransferError("write_dp 0x%x: %s" % (addr, _ackname(ack)), ack)

    def _select_bank(self, ap_addr, apsel=0):
        # SELECT: [31:24]=APSEL, [7:4]=APBANKSEL (high nibble of AP reg addr),
        # [0]=DPBANKSEL (0 here). Cache to avoid redundant writes.
        sel = (apsel << 24) | (ap_addr & 0xF0)
        if sel != self._select:
            self.write_dp(DP_SELECT, sel)
            self._select = sel

    def read_ap(self, ap_addr, apsel=0):
        # AP reads are posted: the AP read returns the previous result, the
        # current value is latched into RDBUFF.
        self._select_bank(ap_addr, apsel)
        self.swd.read(1, ap_addr & 0xC)  # posted; discard stale
        return self._unwrap(*self.swd.read(0, DP_RDBUFF),
                            what="read_ap 0x%x" % ap_addr)

    def write_ap(self, ap_addr, value, apsel=0):
        self._select_bank(ap_addr, apsel)
        ack = self.swd.write(1, ap_addr & 0xC, value)
        if ack != SWD_OK:
            raise TransferError("write_ap 0x%x: %s" % (ap_addr, _ackname(ack)), ack)

    # --- bring-up + power + recovery ---
    def connect(self):
        self.swd.jtag_to_swd()
        self.dpidr = self.read_dp(DP_DPIDR)
        self._select = None
        self.clear_sticky()
        self.power_up()
        return self.dpidr

    def clear_sticky(self):
        # Clear all sticky error bits via ABORT; resets cached SELECT.
        self.swd.write(0, DP_ABORT, ABORT_CLEAR_ALL)

    def power_up(self, timeout=50):
        self.write_dp(DP_SELECT, 0)
        self._select = 0
        self.write_dp(DP_CTRL_STAT, PWRUP_REQ)
        for _ in range(timeout):
            stat = self.read_dp(DP_CTRL_STAT)
            if (stat & PWRUP_ACK) == PWRUP_ACK:
                return stat
        raise TransferError("power-up not acked (CTRL/STAT=0x%08x)" % stat)

    def check_sticky(self):
        stat = self.read_dp(DP_CTRL_STAT)
        if stat & (STICKYERR | STICKYORUN | STICKYCMP):
            self.clear_sticky()
        return stat


class MEMAP:
    def __init__(self, dp, apsel=0):
        self.dp = dp
        self.apsel = apsel
        self._csw = None

    def _set_csw(self, csw):
        if csw != self._csw:
            self.dp.write_ap(AP_CSW, csw, self.apsel)
            self._csw = csw

    def idr(self):
        return self.dp.read_ap(AP_IDR, self.apsel)

    def read32(self, addr):
        self._set_csw(CSW_WORD)
        self.dp.write_ap(AP_TAR, addr, self.apsel)
        return self.dp.read_ap(AP_DRW, self.apsel)

    def write32(self, addr, value):
        self._set_csw(CSW_WORD)
        self.dp.write_ap(AP_TAR, addr, self.apsel)
        self.dp.write_ap(AP_DRW, value, self.apsel)

    def read_block32(self, addr, count):
        # Auto-increment TAR within each 1 KB region; re-arm TAR at the boundary.
        out = []
        self._set_csw(CSW_WORD_INC)
        i = 0
        while i < count:
            self.dp.write_ap(AP_TAR, addr, self.apsel)
            # words remaining in this 1 KB window
            room = (TAR_INC_BOUNDARY - (addr & (TAR_INC_BOUNDARY - 1))) >> 2
            n = count - i
            if n > room:
                n = room
            for _ in range(n):
                out.append(self.dp.read_ap(AP_DRW, self.apsel))
            addr += n * 4
            i += n
        return out

    def write_block32(self, addr, words):
        count = len(words)
        self._set_csw(CSW_WORD_INC)
        i = 0
        while i < count:
            self.dp.write_ap(AP_TAR, addr, self.apsel)
            room = (TAR_INC_BOUNDARY - (addr & (TAR_INC_BOUNDARY - 1))) >> 2
            n = count - i
            if n > room:
                n = room
            for j in range(n):
                self.dp.write_ap(AP_DRW, words[i + j], self.apsel)
            addr += n * 4
            i += n

    def write_block32_fast(self, addr, words):
        # Same as write_block32 but uses the transport's inlined DRW streamer for
        # the hot path (CSW/TAR/SELECT are set here; SELECT stays in AP bank 0,
        # where CSW/TAR/DRW live, so the streamer's DRW writes are consistent).
        count = len(words)
        self._set_csw(CSW_WORD_INC)
        i = 0
        while i < count:
            self.dp.write_ap(AP_TAR, addr, self.apsel)
            room = (TAR_INC_BOUNDARY - (addr & (TAR_INC_BOUNDARY - 1))) >> 2
            n = count - i
            if n > room:
                n = room
            self.dp.swd.write_drw_block(words[i:i + n])
            addr += n * 4
            i += n

    # Byte/halfword single access: place data in the correct DRW lane per CSW
    # size and TAR low bits (ADIv5 byte-laning).
    def read8(self, addr):
        self._set_csw(CSW_BASE | CSW_SIZE8 | CSW_NADDRINC)
        self.dp.write_ap(AP_TAR, addr, self.apsel)
        drw = self.dp.read_ap(AP_DRW, self.apsel)
        return (drw >> (8 * (addr & 3))) & 0xFF

    def read16(self, addr):
        self._set_csw(CSW_BASE | CSW_SIZE16 | CSW_NADDRINC)
        self.dp.write_ap(AP_TAR, addr, self.apsel)
        drw = self.dp.read_ap(AP_DRW, self.apsel)
        return (drw >> (8 * (addr & 2))) & 0xFFFF

    def write8(self, addr, value):
        self._set_csw(CSW_BASE | CSW_SIZE8 | CSW_NADDRINC)
        self.dp.write_ap(AP_TAR, addr, self.apsel)
        self.dp.write_ap(AP_DRW, (value & 0xFF) << (8 * (addr & 3)), self.apsel)


class CortexM:
    def __init__(self, ap):
        self.ap = ap

    def read_dhcsr(self):
        return self.ap.read32(DHCSR)

    def is_halted(self):
        return bool(self.read_dhcsr() & S_HALT)

    def halt(self, timeout=50):
        self.ap.write32(DHCSR, DBGKEY | C_DEBUGEN | C_HALT)
        for _ in range(timeout):
            if self.read_dhcsr() & S_HALT:
                return True
        raise TransferError("core did not halt (DHCSR=0x%08x)" % self.read_dhcsr())

    def resume(self):
        self.ap.write32(DHCSR, DBGKEY | C_DEBUGEN)

    def step(self, timeout=50):
        self.ap.write32(DHCSR, DBGKEY | C_DEBUGEN | C_MASKINTS | C_STEP)
        for _ in range(timeout):
            if self.read_dhcsr() & S_HALT:
                return True
        raise TransferError("single-step did not complete")

    def reset_and_halt(self, timeout=100):
        # Catch the reset vector so the core halts at the first instruction.
        self.ap.write32(DEMCR, DEMCR_TRCENA | DEMCR_VC_CORERESET)
        self.ap.write32(DHCSR, DBGKEY | C_DEBUGEN | C_HALT)
        self.ap.write32(AIRCR, AIRCR_VECTKEY | AIRCR_SYSRESETREQ)
        for _ in range(timeout):
            dhcsr = self.read_dhcsr()
            if dhcsr & S_HALT:
                # leave vector catch armed off again
                self.ap.write32(DEMCR, DEMCR_TRCENA)
                return True
        raise TransferError("reset-and-halt failed (DHCSR=0x%08x)" % self.read_dhcsr())

    def sysreset(self):
        self.ap.write32(AIRCR, AIRCR_VECTKEY | AIRCR_SYSRESETREQ)

    def cpuid(self):
        return self.ap.read32(CPUID)
