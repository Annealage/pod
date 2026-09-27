# SWD-puppet: drive the nRF52840 DUT's SPIM0 as an SPI controller to exercise
# the pod's PIO SPI target. Runs on the POD (pod is BOTH the SPI peripheral on
# PIO0 and the SWD controller on PIO1). The DUT core is halted; its SPIM0 +
# EasyDMA run autonomously against pod-DUT RAM buffers on the nRF side.
#
# Throwaway spike code, not built into firmware (prototypes/ per repo
# conventions). Written for the spi-la-concurrency.md Phase E hardware
# validation gate: paired with a pod-side script that also brings up
# annealage_pod.peripherals.spi_target and annealage_pod.debug.logic_analyser
# on distinct PIO0 state machines, this is the known-pattern source the SPI
# target and logic analyser are cross-checked against.
#
# Usage on the pod (over the REPL), DUT wired per below:
#   import spi_puppet as puppet
#   puppet.halt_idle(mode=0, clkdiv=32)      # halt DUT core, idle the shared pins
#   # ... build the pod SpiTarget here (peripherals.spi_target(...)) ...
#   puppet.enable_spim(mode=0, freq='k125')  # route + enable the DUT's SPIM0
#   puppet.xfer([0x00, 0xFF, ...], 8)        # clock nbytes, returns MISO rxd
#
# Wiring (pod GPnn <-> DUT P0.xx): 19 MOSI<->0.20, 18 SCK<->0.17,
# 17 CS<->0.15, 16 MISO<->0.13. Pod is peripheral: pod GP16 (MISO, pod OUT) ->
# DUT P0.13 (SPIM MISO in); pod GP19/18/17 (MOSI/SCK/CS, pod IN) <- DUT
# P0.20/0.17/0.15 (SPIM MOSI/SCK out, CS driven as plain GPIO).
#
# USAGE ORDER (matters): halt_idle() FIRST so the DUT core is halted and the
# shared CS/SCK/MOSI wires are driven to idle BEFORE the pod SpiTarget is built,
# otherwise the still-running DUT firmware toggles the wires and the pod SM
# captures phantom activity. Then build the SpiTarget, then enable_spim(), then
# xfer().

from annealage_pod.debug import ops

SPIM = 0x40003000
R_ENABLE, R_PSEL_SCK, R_PSEL_MOSI, R_PSEL_MISO = 0x500, 0x508, 0x50C, 0x510
R_FREQ = 0x524
R_RXD_PTR, R_RXD_MAX, R_RXD_AMT = 0x534, 0x538, 0x53C
R_TXD_PTR, R_TXD_MAX, R_TXD_AMT = 0x544, 0x548, 0x54C
R_CONFIG = 0x554
T_START, E_END = 0x010, 0x118

GPIO0 = 0x50000000
G_OUTSET, G_OUTCLR, G_DIRSET, G_DIRCLR, G_PINCNF = 0x508, 0x50C, 0x518, 0x51C, 0x700

CLOCK = 0x40000000
T_HFCLKSTART, E_HFCLKSTARTED = 0x000, 0x100

PIN_SCK, PIN_MOSI, PIN_MISO, PIN_CS = 17, 20, 13, 15
TXBUF, RXBUF = 0x20030000, 0x20030100

FREQ = {"k125": 0x02000000, "k250": 0x04000000, "k500": 0x08000000, "m1": 0x10000000,
        "m2": 0x20000000, "m4": 0x40000000, "m8": 0x80000000}

_ap = None


def halt_idle(mode=0, clkdiv=32):
    """Halt the DUT core, start HFCLK, and drive CS/SCK/MOSI to idle as GPIO.

    Call BEFORE building the pod SpiTarget so the shared wires are quiescent
    (CS high, SCK at CPOL) and the pod SM parks on CS-high instead of sampling
    firmware pin activity. SPIM stays disabled here.
    """
    global _ap
    _dp, ap, cm = ops._ensure(clkdiv=clkdiv)
    _ap = ap
    cm.halt()
    ap.write32(CLOCK + E_HFCLKSTARTED, 0)
    ap.write32(CLOCK + T_HFCLKSTART, 1)
    for _ in range(2000):
        if ap.read32(CLOCK + E_HFCLKSTARTED):
            break
    ap.write32(SPIM + R_ENABLE, 0)  # SPIM off while we hand-drive the pins
    cpol = (mode >> 1) & 1
    # CS high (deasserted) before enabling its output, to avoid a low glitch.
    ap.write32(GPIO0 + G_OUTSET, 1 << PIN_CS)
    if cpol:
        ap.write32(GPIO0 + G_OUTSET, 1 << PIN_SCK)
    else:
        ap.write32(GPIO0 + G_OUTCLR, 1 << PIN_SCK)
    ap.write32(GPIO0 + G_OUTCLR, 1 << PIN_MOSI)
    ap.write32(GPIO0 + G_DIRSET, (1 << PIN_CS) | (1 << PIN_SCK) | (1 << PIN_MOSI))
    return {"halted": True, "hfclk": ap.read32(CLOCK + E_HFCLKSTARTED)}


def enable_spim(mode=0, freq="k125"):
    """Route SPIM0 onto SCK/MOSI/MISO and enable it. CS stays a manual GPIO."""
    ap = _ap
    ap.write32(SPIM + R_ENABLE, 0)
    ap.write32(SPIM + R_PSEL_SCK, PIN_SCK)
    ap.write32(SPIM + R_PSEL_MOSI, PIN_MOSI)
    ap.write32(SPIM + R_PSEL_MISO, PIN_MISO)
    ap.write32(SPIM + R_FREQ, FREQ[freq])
    # nRF SPIM CONFIG: bit2=CPOL, bit1=CPHA, bit0=ORDER(0=MSB). Map SPI mode:
    # CPOL=mode>>1, CPHA=mode&1, MSB-first.
    ap.write32(SPIM + R_CONFIG, ((mode >> 1) << 2) | ((mode & 1) << 1))
    ap.write32(SPIM + R_ENABLE, 7)
    return {"enable": ap.read32(SPIM + R_ENABLE)}


def _wr_bytes(ap, addr, data):
    d = bytes(data)
    while len(d) % 4:
        d += b"\x00"
    for i in range(0, len(d), 4):
        ap.write32(addr + i, d[i] | (d[i + 1] << 8) | (d[i + 2] << 16) | (d[i + 3] << 24))


def _rd_bytes(ap, addr, n):
    out = []
    for wi in range((n + 3) // 4):
        w = ap.read32(addr + wi * 4)
        out += [w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF]
    return out[:n]


def xfer(tx, nbytes):
    """Clock `nbytes` on SPIM0: TX `tx` (0-padded), capture MISO into RXD.

    CS is asserted low around the transfer. Returns the RXD bytes = the MISO the
    pod's SPI target drove, plus done/rxd_amount for sanity.
    """
    ap = _ap
    tx = list(tx)
    _wr_bytes(ap, TXBUF, tx + [0] * (nbytes - len(tx)))
    ap.write32(SPIM + R_TXD_PTR, TXBUF)
    ap.write32(SPIM + R_TXD_MAX, nbytes)
    ap.write32(SPIM + R_RXD_PTR, RXBUF)
    ap.write32(SPIM + R_RXD_MAX, nbytes)
    ap.write32(SPIM + E_END, 0)
    ap.write32(GPIO0 + G_OUTCLR, 1 << PIN_CS)  # assert CS
    ap.write32(SPIM + T_START, 1)
    done = 0
    for _ in range(5000):
        if ap.read32(SPIM + E_END):
            done = 1
            break
    ap.write32(GPIO0 + G_OUTSET, 1 << PIN_CS)  # deassert CS
    return {"done": done, "rxd_amount": ap.read32(SPIM + R_RXD_AMT),
            "rxd": _rd_bytes(ap, RXBUF, nbytes)}
