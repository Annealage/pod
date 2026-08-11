# ADIv5 DP / AP / MEM-AP and Cortex-M debug control over the PIO SWD transport.
#
# Implemented from the ARM ADIv5 (IHI0031) and ARMv7-M (DDI0403) architecture
# reference manuals: the register offsets and bit fields are the architecture's;
# the transport, caching, block access and error recovery are the pod's own, over
# swd_pio.
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
AP_BASE = 0xF8
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

# DFSR (Debug Fault Status Register): records why the core last halted. Bits are
# write-1-to-clear; the GDB server clears it before every resume/step so the next
# halt reports a fresh cause, then decodes the raw value into a gdb signal.
DFSR = 0xE000ED30
DFSR_HALTED = 1 << 0
DFSR_BKPT = 1 << 1
DFSR_DWTTRAP = 1 << 2
DFSR_VCATCH = 1 << 3
DFSR_EXTERNAL = 1 << 4
DFSR_CLEAR_ALL = 0x1F

# --- Flash Patch and Breakpoint unit (FPBv1, Cortex-M4) -----------------------
# FPBv1 comparators match only the code/flash region (addr < 0x20000000); RAM
# breakpoints are realised by the host as software BKPT instructions. CTRL writes
# require the KEY bit set in the same write. The host owns the flash-vs-RAM
# realisation policy; this unit exposes only the hardware comparator mechanism.
FP_CTRL = 0xE0002000
FP_COMP0 = 0xE0002008
FP_CTRL_KEY = 1 << 1
FP_CTRL_ENABLE = 1 << 0

# --- Data Watchpoint and Trace unit (DWT, ARMv7-M / Cortex-M4) ----------------
# DWT comparators match data accesses by address, unlike the FPB which matches
# only the code/flash region. A data-address watchpoint compares COMP against
# the access address (low MASK bits ignored to cover the access width) and traps
# per FUNCTION (load / store / either). The DWT clock is gated by DEMCR.TRCENA
# (defined above); init() sets it so a watchpoint works outside a reset path.
DWT_CTRL = 0xE0001000          # NUMCOMP in bits[31:28]
DWT_COMP0 = 0xE0001020         # per-comparator stride 16: COMP+0, MASK+4, FUNCTION+8
DWT_MASK0 = 0xE0001024
DWT_FUNCTION0 = 0xE0001028
DWT_COMP_STRIDE = 16
# Data-address watch FUNCTION codes (ARMv7-M). These map to gdb watch types:
# Z2(write)=6, Z3(read)=5, Z4(access)=7.
DWT_FN_DISABLED = 0
DWT_FN_WATCH_WRITE = 6
DWT_FN_WATCH_READ = 5
DWT_FN_WATCH_ACCESS = 7
_DWT_FUNCS = (DWT_FN_WATCH_WRITE, DWT_FN_WATCH_READ, DWT_FN_WATCH_ACCESS)
# MASK = number of low address bits to ignore so the comparator matches the
# whole access width (1B -> 0, 2B -> 1, 4B -> 2).
_DWT_LEN_MASK = {1: 0, 2: 1, 4: 2}


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

    def resync(self):
        # Re-establish SWD framing after the bus has been driven by another
        # engine (the PIO DRW streamer hands the pins between PIO blocks, which
        # leaves the DP state machine out of phase). A line reset + JTAG-to-SWD
        # re-syncs the DP without disturbing the AP (CSW/TAR persist); SELECT is
        # reset by the line reset, so drop the cache and re-assert power.
        self.swd.jtag_to_swd()
        self.dpidr = self.read_dp(DP_DPIDR)
        self._select = None
        self.clear_sticky()
        stat = self.read_dp(DP_CTRL_STAT)
        if (stat & PWRUP_ACK) != PWRUP_ACK:
            self.power_up()
        return self.dpidr


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

    def read_debug_base(self):
        # ADIv5 MEM-AP BASE: the debug ROM table base pointer (bit 0 = present,
        # bit 1 = format). The host walks/decodes it; here it is just one more
        # generic identity word for discover().
        return self.dp.read_ap(AP_BASE, self.apsel)

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
        # The per-word posted-read + RDBUFF pair is run by the transport's
        # inlined read_drw_block (same method-call-overhead win as the write
        # streamer), so verify and dump are not bottlenecked by the DP/AP method
        # chain.
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
            out.extend(self.dp.swd.read_drw_block(n))
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

    def write16(self, addr, value):
        # Mirror read16's lane shift (8 * (addr & 2)) for halfword writes.
        self._set_csw(CSW_BASE | CSW_SIZE16 | CSW_NADDRINC)
        self.dp.write_ap(AP_TAR, addr, self.apsel)
        self.dp.write_ap(AP_DRW, (value & 0xFFFF) << (8 * (addr & 2)), self.apsel)


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

    def step(self, maskints=True, timeout=50):
        # maskints defaults True so the FLM call frame and existing callers keep
        # interrupts masked across the step; the GDB server steps with
        # maskints=False so the target's own interrupts can fire.
        bits = DBGKEY | C_DEBUGEN | C_STEP | (C_MASKINTS if maskints else 0)
        self.ap.write32(DHCSR, bits)
        for _ in range(timeout):
            if self.read_dhcsr() & S_HALT:
                return True
        raise TransferError("single-step did not complete")

    def read_dfsr(self):
        return self.ap.read32(DFSR)

    def clear_dfsr(self):
        # DFSR bits are write-1-to-clear. Cleared before every resume/step so the
        # next halt reports a fresh cause (mandatory; the host relies on this).
        self.ap.write32(DFSR, DFSR_CLEAR_ALL)

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

    # --- core register access (core must be halted) ---
    # regsel: 0..15 = R0..R12, SP(13), LR(14), PC(15); 16 = xPSR; 17 = MSP;
    # 18 = PSP. Uses DCRSR (REGSEL[6:0], REGWnR bit16) + DCRDR, gated on
    # DHCSR.S_REGRDY. Needed by the CMSIS-FLM call frame and the GDB server.
    def read_core_reg(self, regsel, timeout=50):
        self.ap.write32(DCRSR, regsel & 0x7F)
        for _ in range(timeout):
            if self.read_dhcsr() & S_REGRDY:
                return self.ap.read32(DCRDR)
        raise TransferError("core reg %d read timeout" % regsel)

    def write_core_reg(self, regsel, value, timeout=50):
        self.ap.write32(DCRDR, value)
        self.ap.write32(DCRSR, (regsel & 0x7F) | (1 << 16))
        for _ in range(timeout):
            if self.read_dhcsr() & S_REGRDY:
                return
        raise TransferError("core reg %d write timeout" % regsel)


class FPB:
    # Flash Patch and Breakpoint unit, FPBv1 only (nRF52840 Cortex-M4). The unit
    # is lazily initialised on first use; comparators are flash-only. The host
    # treats a TransferError from set_breakpoint (no free slot / unsupported addr,
    # surfaced as wire status 1) as the signal to fall back to a software BKPT.
    def __init__(self, ap):
        self.ap = ap
        self.rev = None
        self.nb_code = 0
        self.enabled = False
        self._comp = []        # per-slot: breakpoint addr or None
        self._inited = False

    def init(self):
        fpcr = self.ap.read32(FP_CTRL)
        self.rev = (fpcr >> 28) & 0xF
        # NUM_CODE is split: [14:12] high bits and [7:4] low bits.
        self.nb_code = ((fpcr >> 8) & 0x70) | ((fpcr >> 4) & 0xF)
        self.ap.write32(FP_CTRL, FP_CTRL_KEY)            # disable; KEY on every CTRL write
        for n in range(self.nb_code):
            self.ap.write32(FP_COMP0 + 4 * n, 0)
        self._comp = [None] * self.nb_code
        self.enabled = False
        self._inited = True

    def enable(self):
        self.ap.write32(FP_CTRL, FP_CTRL_KEY | FP_CTRL_ENABLE)
        self.enabled = True

    def disable(self):
        # Clear FP_CTRL.ENABLE so no comparator can trap. KEY is required on
        # every CTRL write. Used on session teardown so the FPB unit is not left
        # armed in the DUT for the next operation (e.g. a flash that never inits
        # the FPB) to inherit. Safe to call before init().
        self.ap.write32(FP_CTRL, FP_CTRL_KEY)
        self.enabled = False

    def can_support(self, addr):
        # FPBv1 (rev != 2) comparators match only the flash/code region.
        return self.rev != 2 and addr < 0x20000000

    def set_breakpoint(self, addr):
        if not self._inited:
            self.init()
        if not self.can_support(addr):
            raise TransferError("FPB cannot break at 0x%08x (not flash/FPBv1)" % addr)
        if addr in self._comp:
            return self._comp.index(addr)
        try:
            slot = self._comp.index(None)
        except ValueError:
            raise TransferError("no free FPB comparator")
        if not self.enabled:
            self.enable()
        # FPBv1 COMP: bits[28:2]=addr, REPLACE[31:30] selects upper/lower halfword,
        # bit0=ENABLE.
        replace = 2 if (addr & 0x2) else 1
        comp = (addr & 0x1FFFFFFC) | (replace << 30) | 1
        self.ap.write32(FP_COMP0 + 4 * slot, comp)
        self._comp[slot] = addr
        return slot

    def clear_breakpoint(self, addr):
        if addr not in self._comp:
            return False
        slot = self._comp.index(addr)
        self.ap.write32(FP_COMP0 + 4 * slot, 0)
        self._comp[slot] = None
        return True

    def clear_all(self):
        if not self._inited:
            return
        for n in range(self.nb_code):
            self.ap.write32(FP_COMP0 + 4 * n, 0)
            self._comp[n] = None


class DWT:
    # Data Watchpoint and Trace unit (ARMv7-M / Cortex-M4). Lazily initialised on
    # first use; comparators match any address region (RAM or flash). The host
    # treats a TransferError from set_watchpoint (no free comparator / bad args,
    # surfaced as wire status 1) as a failure to arm. FUNCTION is written last so
    # a comparator only traps once COMP/MASK are valid, and is cleared to 0 to
    # disarm.
    def __init__(self, ap):
        self.ap = ap
        self.numcomp = 0
        self._comp = []        # per-slot: {addr, length, func} or None
        self._inited = False

    def _func_addr(self, slot):
        return DWT_FUNCTION0 + DWT_COMP_STRIDE * slot

    def init(self):
        # Gate the DWT clock so comparators work even outside a reset_and_halt
        # path (reset_and_halt sets TRCENA too, but a watchpoint may be armed on
        # an already-running session).
        self.ap.write32(DEMCR, self.ap.read32(DEMCR) | DEMCR_TRCENA)
        self.numcomp = (self.ap.read32(DWT_CTRL) >> 28) & 0xF
        for n in range(self.numcomp):
            self.ap.write32(DWT_FUNCTION0 + DWT_COMP_STRIDE * n, DWT_FN_DISABLED)
        self._comp = [None] * self.numcomp
        self._inited = True

    def set_watchpoint(self, addr, length, func):
        if not self._inited:
            self.init()
        if length not in _DWT_LEN_MASK or func not in _DWT_FUNCS:
            raise TransferError("DWT bad watch args (len=%d func=%d)" % (length, func))
        want = {"addr": addr, "length": length, "func": func}
        if want in self._comp:
            return self._comp.index(want)
        try:
            slot = self._comp.index(None)
        except ValueError:
            raise TransferError("no free DWT comparator")
        # addr should be naturally aligned to length; MASK ignores the low bits
        # so the comparator covers the whole access width.
        mask = _DWT_LEN_MASK[length]
        base = DWT_COMP0 + DWT_COMP_STRIDE * slot
        self.ap.write32(base, addr)             # COMP
        self.ap.write32(base + 4, mask)         # MASK
        self.ap.write32(base + 8, func)         # FUNCTION last: arms the comparator
        self._comp[slot] = want
        return slot

    def clear_watchpoint(self, addr, length, func):
        want = {"addr": addr, "length": length, "func": func}
        if want not in self._comp:
            return False
        slot = self._comp.index(want)
        self.ap.write32(self._func_addr(slot), DWT_FN_DISABLED)
        self._comp[slot] = None
        return True

    def clear_all(self):
        if not self._inited:
            return
        for n in range(self.numcomp):
            self.ap.write32(self._func_addr(n), DWT_FN_DISABLED)
            self._comp[n] = None

    def disable(self):
        # Disarm every comparator without requiring a prior init(), a safe
        # teardown backstop so a watchpoint is not left armed for the next
        # operation (e.g. a flash that never inits the DWT) to inherit.
        if self._inited:
            self.clear_all()
            return
        numcomp = (self.ap.read32(DWT_CTRL) >> 28) & 0xF
        for n in range(numcomp):
            self.ap.write32(DWT_FUNCTION0 + DWT_COMP_STRIDE * n, DWT_FN_DISABLED)
