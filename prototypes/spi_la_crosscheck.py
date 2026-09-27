# Phase E hardware validation gate for spi-la-concurrency.md: SPI target (PIO0
# sm0) and logic analyser (PIO0 sm1) live together while the spi_puppet
# SWD-puppet clocks a known byte pattern, then the LA-recovered MOSI/MISO
# bytes are cross-checked against the pattern sent and the bytes the SPI
# target itself reports.
#
# Throwaway spike code, not built into firmware (prototypes/ per repo
# conventions).
#
# Usage on the pod (over the REPL), DUT wired per spi_puppet.py's header:
#   import spi_la_crosscheck as t
#   t.run_all()             # sweeps mode 0-3 across 125 kHz .. 8 MHz, prints
#                            # PASS/FAIL per config
#   t.teardown_order_check()  # bring-up/teardown ordering + PIO0 leak check,
#                              # both directions
#
# Triggering note: the LA trigger is armed on the first SCK edge (GP18, the
# edge away from each mode's CPOL idle level), not CS-fall (GP17). CS asserts
# several SWD register writes before SPIM actually starts clocking (each
# register poke is a real SWD round trip), and that gap alone can consume a
# short high-rate capture window before any real bit ever appears - triggering
# on SCK's first edge removes the gap from the window entirely.
#
# Decode note: the trigger's own wait-instructions consume the very first
# edge as their precondition, so the LA's first captured sample already
# reflects the post-edge level with no prior sample to diff against. decode()
# seeds `prev_sck` at the idle level (CPOL) rather than None so that first
# transition is still counted - omitting this seed silently drops bit 0 of
# byte 0 and cascades a one-bit shift through the rest of the stream.

import array
import gc
import time

import rp2
from machine import Pin

import spi_puppet as puppet
from annealage_pod import peripherals
from annealage_pod.debug import logic_analyser as la_mod

SYS_HZ = la_mod.SYS_HZ

# (mode, nRF SPIM freq key, LA sample rate) - LA rate is ~16-19x the SPI clock,
# oversampling comfortably within the validated 125 kHz - 8 MHz band.
CONFIGS = [
    (0, "k125", 2_000_000),
    (1, "m1", 16_000_000),
    (2, "m4", 64_000_000),
    (3, "m8", 100_000_000),
]

PATTERN = [0x00, 0xFF, 0xA5, 0x5A, 0x81, 0x7E, 0x01, 0xFE]

# LA capture budget: depth in samples, sized to comfortably outlast len(PATTERN)
# SPI clock cycles at even the loosest oversample ratio above, while staying
# small enough (a few hundred bytes) to survive a fragmented pod heap - a wider
# capture (needed to also bracket the CS-to-SPIM-start gap, when triggering on
# CS instead of SCK) hit MemoryError on this rig well under the 20000-word
# LogicAnalyser cap.
DEPTH = 1500


def decode_words(words, sck_bit, data_bit, mode, width=4):
    """Packed 32-bit words -> bytes clocked on `data_bit`, sampled on `mode`'s
    data edge (bit k of each width-bit sample = channel base_pin+k).

    Streams sample-by-sample straight out of the packed words (no
    intermediate per-bit or per-sample list): on this rig's fragmented pod
    heap, materialising ~6000 individual sample ints as a growing Python list
    reliably hit MemoryError even with hundreds of KB nominally free.

    prev_sck seeds at the idle level (CPOL) - see the module docstring's
    decode note for why that seed (not None) is required.
    """
    cpol = (mode >> 1) & 1
    cpha = mode & 1
    leading = "rise" if cpol == 0 else "fall"
    trailing = "fall" if leading == "rise" else "rise"
    sample_edge = leading if cpha == 0 else trailing
    mask = (1 << width) - 1
    samples_per_word = 32 // width
    out = []
    cur = 0
    nbits = 0
    prev_sck = cpol
    for w in words:
        for chunk in range(samples_per_word):
            shift = 32 - width * (chunk + 1)
            v = (w >> shift) & mask
            sck = (v >> sck_bit) & 1
            bit = (v >> data_bit) & 1
            if sck != prev_sck:
                edge = "rise" if sck else "fall"
                if edge == sample_edge:
                    cur = (cur << 1) | bit
                    nbits += 1
                    if nbits == 8:
                        out.append(cur)
                        cur = 0
                        nbits = 0
            prev_sck = sck
    return out


def run_one(mode, freqkey, samp_rate, pattern=PATTERN, depth=DEPTH):
    """Run one (mode, freq) config: SPI target + LA up, puppet clocks `pattern`.

    Returns {mode, freq, rate_hz, la_complete, xfer_done, mosi_ok, miso_ok,
    pattern, mosi_decoded, spi_target_captured, miso_decoded, puppet_rxd}.
    """
    gc.collect()
    puppet.halt_idle(mode=mode, clkdiv=32)
    peripherals.spi_target(mode=mode, personality="stream", name="spi_target", size=64)
    la = la_mod.LogicAnalyser(base_pin=16, width=4, sm_id=1)
    clkdiv = max(1, min(65535, round(SYS_HZ / samp_rate)))
    actual = SYS_HZ / clkdiv
    words = (depth * 4 + 31) // 32
    cpol = (mode >> 1) & 1
    trig_cond = "rise" if cpol == 0 else "fall"
    prog = la_mod._build_prog(4, (18, trig_cond))   # GP18 = SCK, first edge away from idle
    la.sm = rp2.StateMachine(1, prog, freq=int(actual), in_base=Pin(16))
    la._prog = prog
    la.buf = array.array("I", bytes(4 * words))
    la.dma = rp2.DMA()
    dctrl = la.dma.pack_ctrl(size=2, inc_read=False, inc_write=True,
                             treq_sel=la_mod.rx_dreq(1))
    la.dma.config(read=la_mod.rx_fifo_addr(1), write=la.buf, count=words,
                  ctrl=dctrl, trigger=False)
    la.dma.active(1)
    la.sm.active(1)
    puppet.enable_spim(mode=mode, freq=freqkey)
    r_xfer = puppet.xfer(pattern, len(pattern))
    t0 = time.ticks_ms()
    while la.dma.active() and time.ticks_diff(time.ticks_ms(), t0) < 3000:
        time.sleep_ms(1)
    la_complete = not la.dma.active()
    buf_list = list(la.buf)
    la.release()
    status = peripherals.spi_target_status(name="spi_target")
    peripherals.release("spi_target")

    n = len(pattern)
    mosi_decoded = decode_words(buf_list, 2, 3, mode)[:n]   # bit3 = GP19 = MOSI
    miso_decoded = decode_words(buf_list, 2, 0, mode)[:n]   # bit0 = GP16 = MISO
    spi_captured = list(status["captured"][:n])

    return {
        "mode": mode, "freq": freqkey, "rate_hz": actual, "la_complete": la_complete,
        "xfer_done": r_xfer["done"], "pattern": pattern,
        "mosi_decoded": mosi_decoded, "spi_target_captured": spi_captured,
        "miso_decoded": miso_decoded, "puppet_rxd": r_xfer["rxd"],
        "mosi_ok": mosi_decoded == pattern and mosi_decoded == spi_captured,
        "miso_ok": miso_decoded == r_xfer["rxd"],
    }


def run_all(configs=CONFIGS):
    """Run every (mode, freq) config in `configs`, print PASS/FAIL, return the results."""
    results = []
    for mode, freqkey, samp_rate in configs:
        r = run_one(mode, freqkey, samp_rate)
        results.append(r)
        ok = r["mosi_ok"] and r["miso_ok"] and r["la_complete"] and r["xfer_done"]
        print("mode=%d freq=%s rate=%.0fHz %s" % (mode, freqkey, r["rate_hz"],
                                                   "PASS" if ok else "FAIL"))
        if not ok:
            print("  pattern=%r mosi_decoded=%r spi_target_captured=%r"
                  % (r["pattern"], r["mosi_decoded"], r["spi_target_captured"]))
            print("  puppet_rxd=%r miso_decoded=%r" % (r["puppet_rxd"], r["miso_decoded"]))
    return results


def _ctrl(block):
    import machine
    return machine.mem32[0x50200000 + block * 0x100000]


def teardown_order_check():
    """Bring-up/teardown ordering, both directions: PIO0 CTRL and arbiter claims
    must be empty again after each full cycle - see spi-la-concurrency.md Phase E.
    """
    from annealage_pod.debug import pio_arbiter

    gc.collect()
    print("baseline", hex(_ctrl(0)), pio_arbiter.status())

    peripherals.spi_target(mode=0, personality="stream", name="spi_target", size=64)
    la = la_mod.LogicAnalyser(base_pin=16, width=4, sm_id=1)
    la.release()
    peripherals.release("spi_target")
    ok1 = _ctrl(0) == 0 and 0 not in pio_arbiter.status()
    print("SPI-up,LA-up,LA-down,SPI-down -> ctrl=%s arbiter=%s %s"
          % (hex(_ctrl(0)), pio_arbiter.status(), "PASS" if ok1 else "FAIL"))

    la = la_mod.LogicAnalyser(base_pin=16, width=4, sm_id=1)
    peripherals.spi_target(mode=0, personality="stream", name="spi_target", size=64)
    peripherals.release("spi_target")
    la.release()
    ok2 = _ctrl(0) == 0 and 0 not in pio_arbiter.status()
    print("LA-up,SPI-up,SPI-down,LA-down -> ctrl=%s arbiter=%s %s"
          % (hex(_ctrl(0)), pio_arbiter.status(), "PASS" if ok2 else "FAIL"))

    return ok1 and ok2
