# Prototype pod_exec recipes for the aio measurement asks (task #65).
#
# STATUS: all three validated on the pod by aio 2026-07-15 and corrected here.
# Recipe 1 (LA PIO0 + SWD PIO1 coexist) confirmed; RP2350 pad-ISO fix folded in
# (a bare Pin gives a silent all-zero capture). Recipe 2 (wake-latency): counting
# exact, 2-cycles/tick holds; timeout + polarity + raw_0-baseline guards folded in
# (a non-response otherwise underflows to raw=0 = "fastest" AND freezes the loop).
# Recipe 3 (edge counter): the wrap-top-jmp derail fixed (had returned edges=0) +
# the runtime-encode window-stretch fixed (pre-encode + measured elapsed). Next:
# promote the good ones to stable ops/MCP tools. Throwaway spike code, not firmware.
#
# PIO BUDGET (authoritative: annealage_pod.debug.pio_arbiter.PIO_MAP):
#   PIO0 = free block (logic analyser, and these #2/#3 primitives).
#   PIO1 = SWD debug stack (ops._ensure claims it).
#   PIO2 = CYW43 Wi-Fi - RESERVED, never build an SM there (hard-wedges the chip).
# CONSEQUENCE: recipe 1 uses PIO0(LA)+PIO1(SWD) - they coexist. Recipes 2 and 3
# ALSO live on PIO0, so they CANNOT run concurrently with an LA capture (one PIO0
# owner at a time via the arbiter); they CAN run alongside an SWD session (PIO1).
#
# PINS (pod free header GPIOs): GP16-GP22, GP26-GP28 (also GP2-GP9 if unused).
#   Avoid GP0/GP1 (backup UART REPL), GP14/GP15 (SWD), GP10/GP11 (I2C target),
#   GP23/24/25/29 (internal CYW43, not on header). 3.3V only, common GND to DUT.
#
# Run any of these over the socket REPL, e.g.:
#   mpremote connect socket://<pod>:8266 resume exec "$(cat this_file); print(demo_1())"
# or paste the function + a call into pod_exec.

import array
import time

import rp2
from machine import Pin

from annealage_pod.debug import ops, logic_analyser as la, pio_arbiter


# ---------------------------------------------------------------------------
# Recipe 1: interleaved LA (PIO0) + SWD RAM sampling (PIO1).
# Arm the DMA capture, then do SWD MEM-AP reads WHILE it fills, then read the
# buffer back. Built from real primitives (logic_analyser rx_fifo_addr/rx_dreq/
# _build_prog + ops._ensure's ap.read_block32). Validated coexistence.
# ---------------------------------------------------------------------------
def interleaved_la_swd(base_pin, width, rate, depth,
                       swd_addr, swd_words, clkdiv=8):
    """Capture `depth` samples of GP[base_pin..+width) at ~rate Hz on PIO0 while
    repeatedly reading `swd_words` words from the DUT at `swd_addr` over SWD
    (PIO1). Returns {complete, rate, la_buf (array 'I'), swd_samples: [ [words], ...]}.
    """
    dp, ap, cm, fl = ops._ensure(clkdiv)            # SWD up on PIO1 (claims it)
    pio_arbiter.claim("la", 0)                      # PIO0 for the analyser
    sm_id = 0
    clk = max(1, min(65535, round(la.SYS_HZ / rate)))
    actual = la.SYS_HZ / clk
    words = min((depth * width + 31) // 32, la.LogicAnalyser.MAX_WORDS)
    buf = array.array("I", bytes(4 * words))
    sm = None
    dma = None
    try:
        # RP2350: a bare Pin(n) leaves pad isolation set -> silent all-zero capture
        # on an unconfigured DUT GPIO (only "works" when the pin is externally
        # driven). Configure the captured range as inputs first. aio val 2026-07-15.
        for _p in range(base_pin, base_pin + width):
            Pin(_p, Pin.IN)
        prog = la._build_prog(width, None)          # immediate (no trigger)
        sm = rp2.StateMachine(sm_id, prog, freq=int(actual), in_base=Pin(base_pin, Pin.IN))
        dma = rp2.DMA()
        ctrl = dma.pack_ctrl(size=2, inc_read=False, inc_write=True,
                             treq_sel=la.rx_dreq(sm_id))
        dma.config(read=la.rx_fifo_addr(sm_id), write=buf, count=words,
                   ctrl=ctrl, trigger=False)
        dma.active(1)
        sm.active(1)                                # capture running on PIO0
        swd_samples = []
        budget = int(words * 32 / width / actual * 1000) + 3000
        t0 = time.ticks_ms()
        # Interleave SWD reads (PIO1) as the PIO0 DMA fills. sleep_ms(1), NOT a
        # tight spin: CYW43 Wi-Fi is cooperatively scheduled and a spin starves it.
        while dma.active() and time.ticks_diff(time.ticks_ms(), t0) < budget:
            swd_samples.append(list(ap.read_block32(swd_addr, swd_words)))
            time.sleep_ms(1)
        complete = not dma.active()
    finally:
        if dma is not None:
            try:
                dma.active(0)
                dma.close()
            except Exception:
                pass
        if sm is not None:
            try:
                sm.active(0)
            except Exception:
                pass
        try:
            rp2.PIO(0).remove_program()             # free PIO0 instr memory
        except Exception:
            pass
        pio_arbiter.release("la")                   # SWD (PIO1) left up; ops.close() to drop it
    return {"complete": complete, "rate": actual, "words": words,
            "la_buf": buf, "swd_samples": swd_samples}


def demo_1():
    # GP16 capture at 1 MHz, 2000 samples, sampling DUT SRAM top word each round.
    r = interleaved_la_swd(base_pin=16, width=1, rate=1_000_000, depth=2000,
                           swd_addr=0x20000000, swd_words=1)
    return {"complete": r["complete"], "rate": r["rate"],
            "n_swd_rounds": len(r["swd_samples"]),
            "first_swd": r["swd_samples"][0] if r["swd_samples"] else None}


# ---------------------------------------------------------------------------
# Recipe 2 (PROTOTYPE, aio #2): GPIO round-trip / wake-latency timer on the PIO
# timebase. Drive a stimulus edge, count PIO cycles until the DUT responds on a
# second pin, push the count. Repeat N times -> delta array. PIO0.
#
# Loop body is 2 instructions (jmp pin + jmp x--) = 2 PIO cycles per tick, so
# raw_ticks = 0xFFFFFFFF - x, and latency_s ~= raw_ticks * 2 / freq (plus a small
# fixed entry overhead - CALIBRATE the constant with a known loopback delay).
# ---------------------------------------------------------------------------
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def _latency_probe():
    # set(pins,0) is the FIRST instruction so sm.restart() (jumps to initial PC but
    # does NOT reset GPIO outputs) re-lows the stimulus on timeout recovery. Without
    # this a stuck-high stim gives no rising edge and every following shot also times
    # out (aio val 2026-07-16). The normal path auto-wraps here after push and
    # re-lows harmlessly before the next go-signal.
    set(pins, 0)                # idle-low on (re)entry; restart lands here
    pull(block)                 # go signal from Python (value ignored)
    set(pins, 1)                # drive stimulus HIGH (edge to the DUT)
    mov(x, invert(null))        # x = 0xFFFFFFFF
    label("wait_resp")
    jmp(pin, "done")            # response pin (jmp_pin) high? -> done
    jmp(x_dec, "wait_resp")     # else x-- and keep waiting
    label("done")
    mov(isr, x)
    push(block)                 # Python reads (0xFFFFFFFF - this) = ticks waited


def wake_latency(stim_pin, resp_pin, n=64, freq=150_000_000, settle_ms=2,
                 timeout_ms=50, raw_0=1):
    """Fire `n` stimulus edges, time each to resp_pin going HIGH on the PIO
    timebase. Returns {freq, raw:[ticks|None], latency_us:[...|None], timeouts}.
    resp_pin MUST idle LOW and go HIGH on the DUT response. raw_0 = the self-
    loopback baseline (stim==resp) for pure internal overhead; latency reported as
    (raw - raw_0)*2/freq. PROTOTYPE - two silent-failure guards (aio val 2026-07-15):
    - per-shot timeout: without it a non-response underflows x after ~57 s, and the
      pushed 0xFFFFFFFF reads as raw=0 (masquerades as the FASTEST sample) while the
      blocking sm.get() freezes the single event loop; poll rx_fifo() with a deadline.
    - polarity: a shot fired while resp is already HIGH reads ~0; skip+flag it.
    NB latency scales by the nominal `freq`; exact only at the true SM clock (150 MHz).
    """
    pio_arbiter.claim("aio_lat", 0)                 # PIO0 (not while an LA capture holds it)
    sm = None
    rp = Pin(resp_pin, Pin.IN)
    try:
        sm = rp2.StateMachine(0, _latency_probe, freq=freq,
                              set_base=Pin(stim_pin, Pin.OUT), jmp_pin=rp)
        sm.active(1)
        raw = []
        timeouts = 0
        for _ in range(n):
            if rp.value():                          # resp not idle-low: don't fire into a high line
                raw.append(None)
                timeouts += 1
                time.sleep_ms(settle_ms)
                continue
            sm.put(0)                               # release one shot
            t0 = time.ticks_ms()
            # bounded wait on the result; sleep_ms(1) not a spin (the PIO already
            # captured the precise count, so this poll adds no measurement jitter
            # and keeps CYW43 Wi-Fi serviced).
            while not sm.rx_fifo() and time.ticks_diff(time.ticks_ms(), t0) < timeout_ms:
                time.sleep_ms(1)
            if sm.rx_fifo():
                v = sm.get()
                raw.append((0xFFFFFFFF - v) & 0xFFFFFFFF)
            else:
                raw.append(None)
                timeouts += 1
                sm.restart()                        # PC -> set(pins,0): re-lows stim for next shot
                while sm.rx_fifo():                 # drop a word that raced the restart
                    sm.get()
            time.sleep_ms(settle_ms)                # let the DUT re-arm
        latency_us = [((t - raw_0) * 2 / freq * 1e6) if t is not None else None
                      for t in raw]
        return {"freq": freq, "raw": raw, "latency_us": latency_us, "timeouts": timeouts}
    finally:
        if sm is not None:
            try:
                sm.active(0)
            except Exception:
                pass
        try:
            rp2.PIO(0).remove_program()
        except Exception:
            pass
        pio_arbiter.release("aio_lat")


# ---------------------------------------------------------------------------
# Recipe 3 (PROTOTYPE, aio #3): PIO edge counter / frequency over a window.
# Count rising edges on a pin for T seconds, compute Hz. PIO0.
#
# x counts DOWN from 0xFFFFFFFF, one decrement per rising edge. The driver arms
# it, sleeps T, snapshots x mid-run via sm.exec, and computes edges/T. The
# mid-run snapshot (exec mov(isr,x)+push) steals a PIO cycle - fine at these
# rates but VALIDATE against a known reference frequency.
# ---------------------------------------------------------------------------
@rp2.asm_pio()
def _edge_counter():
    # x counts DOWN one per rising edge. Use an EXPLICIT label, not a decoy jmp at
    # the wrap-top: on RP2 a TAKEN jmp at wrap-top overrides the wrap, so a
    # jmp(x_dec) sitting there escapes the loop after the first edge (aio proved the
    # wrap-top idiom returned edges=0; this form returns 10038@10k, 100432@100k).
    mov(x, invert(null))        # x = 0xFFFFFFFF
    label("loop")
    wait(0, pin, 0)             # low (pin 0 = in_base)
    wait(1, pin, 0)             # rising edge
    jmp(x_dec, "loop")          # x-- and repeat


# Pre-encoded snapshot: encoding "mov(isr,x)" from a string at runtime costs ~4 ms
# and stretches the measured window (~+0.4% at 1 s). aio val 2026-07-15.
_ENC_MOV_ISR_X = rp2.asm_pio_encode("mov(isr, x)", 0)
_ENC_PUSH_NOBLOCK = rp2.asm_pio_encode("push(noblock)", 0)


def freq_over_window(sig_pin, window_s=1.0, freq=150_000_000, external=True):
    """Count rising edges on sig_pin for ~window_s -> {edges, hz, elapsed_s}.
    Divides by the ACTUAL elapsed (ticks_us), not the nominal window. Also an
    encoder counter (read edges directly). external=True configures the pin as an
    input (a real DUT signal); external=False for a jumperless self-test where the
    SAME pin is PWM-driven (Pin.IN gates a same-pin reference to 0 edges). PROTOTYPE."""
    pio_arbiter.claim("aio_cnt", 0)                 # PIO0
    sm = None
    try:
        pin = Pin(sig_pin, Pin.IN) if external else Pin(sig_pin)
        sm = rp2.StateMachine(0, _edge_counter, freq=freq, in_base=pin)
        sm.active(1)
        t0 = time.ticks_us()
        time.sleep(window_s)
        sm.exec(_ENC_MOV_ISR_X)                     # snapshot x mid-run (pre-encoded)
        sm.exec(_ENC_PUSH_NOBLOCK)
        elapsed_s = time.ticks_diff(time.ticks_us(), t0) / 1e6
        raw = sm.get()
        edges = (0xFFFFFFFF - raw) & 0xFFFFFFFF
        return {"edges": edges, "hz": edges / elapsed_s, "elapsed_s": elapsed_s}
    finally:
        if sm is not None:
            try:
                sm.active(0)
            except Exception:
                pass
        try:
            rp2.PIO(0).remove_program()
        except Exception:
            pass
        pio_arbiter.release("aio_cnt")
