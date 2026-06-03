# RP2350 pod: PIO logic analyser + PIO arbiter

**Status: DESIGN (Track 2, pre-implementation).** Converts to the usage doc once
built and hardware-validated. This is the proposal to review before code lands.

Two coupled pieces:

1. A PIO **logic analyser** (LA): sample a set of DUT pins at a configurable
   rate into a RAM ring buffer, with a trigger, then stream the capture to the
   host and decode it to a standard waveform format.
2. A runtime **PIO arbiter** so the LA and the SWD debug stack can each claim
   PIO blocks / SMs / pins on demand without colliding. SWD and the LA do not
   run at the same time (see the budget below); the arbiter makes swapping
   between them safe and explicit.

## Why an arbiter: the PIO budget

The RP2350 has 3 PIO blocks (4 SMs each, 32 instruction words each):

| Block | Current owner | Notes |
|---|---|---|
| PIO0 | CYW43 Wi-Fi (SM0-3) | permanent, off-limits |
| PIO1 | SWD transport (SM0, `swd_prog` ~28 instr) | owns GP14/GP15 |
| PIO2 | optional DRW write-streamer (off by default), else free | |

Usable blocks for {SWD, DRW-streamer, LA, future SWO/I2C-SPI-bitbang} are PIO1
and PIO2. A useful LA wants a whole block: instruction space, one SM, a DMA
channel, and ideally room to grow a trigger/timestamp engine. With SWD on PIO1
and the streamer occasionally on PIO2, the LA has no guaranteed free block, so
running it means freeing one - and SWD is the one to swap out, since you capture
signals when you are *not* single-stepping over SWD. Hence SWD and the LA are
mutually exclusive.

The swap has to be **clean**, and today it is not: `SWDPio.deinit()` only stops
the SM (`active(0)`). It leaves `swd_prog` resident in PIO instruction memory
and GP14/GP15 bound to PIO. A real release must also remove the program
(`rp2.PIO(n).remove_program(...)`) and return the pins, or the LA cannot claim
the block (and re-`_ensure`-ing SWD later would re-add the program and leak
instruction memory, per the `dev-notes.md` resume caveat).

## PIO arbiter

`annealage_pod/debug/pio_arbiter.py` - the single owner of PIO block / SM / pin /
program-space claims, so no two consumers fight over the same silicon.

```python
# One process-wide arbiter instance.
class PioArbiter:
    # Static map of what is permanently reserved (PIO0 = CYW43).
    def claim(self, owner, block, n_sms=1, pins=(), instrs=0): ...
        # Grant (block, SMs, pins) to `owner`, or raise PioConflict naming the
        # current holder. Records the claim.
    def release(self, owner): ...
        # Drop `owner`'s claim (caller has already torn its program/pins down).
    def holder(self, block): ...        # who owns a block, or None
    def status(self): ...               # {block: owner, ...} for introspection

# Higher-level: the SWD<->LA swap the user asked for.
def to_logic_analyser(...):   # ensure SWD is fully torn down, claim its block + pins for the LA
def to_swd():                 # tear the LA down, let ops._ensure rebuild SWD
```

Integration points:
- `ops` (SWD) registers its PIO1 claim through the arbiter in `_ensure`, and its
  teardown path gains a *full* release: `sm.active(0)` + `PIO(1).remove_program`
  + drop the pins, exposed as `SWDPio.release()` (distinct from today's
  `deinit()` which only pauses). `ops.close()` calls it and releases the claim.
- The LA claims its block + pins through the arbiter before configuring its SM,
  and releases on stop.

The arbiter is deliberately small: it is bookkeeping + the clean-teardown
contract, not a generic scheduler. It does not try to *share* a block between
consumers; claims are whole-block for simplicity (matches the budget reality).

## Logic analyser

`annealage_pod/debug/logic_analyser.py`.

**Capture engine.** A minimal PIO program samples `width` contiguous pins
(`in_base .. in_base+width-1`) once per PIO clock and autopushes packed samples:

```python
@rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_LEFT, autopush=True, push_thresh=32)
def la_prog():
    wrap_target()
    in_(pins, WIDTH)      # one sample per loop iteration
    wrap()
```

Sample rate = `SYS_HZ / clkdiv` (one sample per iteration). Packing: 32/width
samples per 32-bit FIFO word (width 1 -> 32/word, width 8 -> 4/word, width 16 ->
2/word). The host unpacks.

**Trigger.** Optional pre-roll guard before the capture loop:
- `immediate`: start sampling at once.
- `edge` / `level` on a trigger pin: `wait(level, gpio, trig)` (and a paired
  `wait` for an edge) before entering the loop.
Post-trigger only to start; pre-trigger history (ring + freeze on trigger) is a
later refinement.

**Transport: DMA ring (primary).** A `rp2.DMA` channel, DREQ-paced to the SM's
RX FIFO, writes packed words into a RAM buffer sized to the requested depth;
capture stops at the word count (or on a stop request for free-run). This is the
path that reaches real sample rates. **Hardware-validation dependency:**
`rp2.DMA` is confirmed present in the pod build (checked 2026-06-03); the
PIO-RX DREQ pacing still has to be validated when the capture engine lands.

**Transport: polled (fallback).** If `rp2.DMA` is unavailable, read the FIFO in
Python bursts (`sm.get()` loop). Correct but rate-limited (tens of kHz), enough
for slow buses; flagged as degraded in the result.

Depth is RAM-bounded (~100-200 KB free -> ~25-50K words; at width 8 that's
~100-200K samples). `clkdiv` sets the rate; the result reports the actual rate so
the host timebase is exact.

**Streaming out.** The captured RAM buffer streams to the host over a dedicated
TCP port (the `dump_stream` pattern: bounded blocks, no pod filesystem). For
small captures the REPL return path also works.

## Host side

- `pod/vcd.py`: decode raw packed samples -> VCD (GTKWave / PulseView / sigrok
  read VCD). Inputs: width, sample rate, channel names; output: a `.vcd` file.
- `Pod.logic_analyse(pins, rate, depth, trigger=..., out='cap.vcd')` orchestrates
  the swap (to_logic_analyser), capture, stream, decode, restore (to_swd).
- CLI `pod la <label> --pins 16-23 --rate 1e6 --depth 20000 [--trigger 16:rise]
  --out cap.vcd`; MCP tool `logic_analyse`.

## Hardware-validation checklist (when the rig is free)

1. `rp2.DMA` present; PIO-RX DREQ-paced FIFO -> RAM ring works; measure the max
   reliable sample rate.
2. SWD full teardown (`remove_program` + pin release) frees PIO1, the LA claims
   it, captures, then `to_swd()` + `ops.info()` round-trips cleanly (no leaked
   instruction memory, DPIDR still reads).
3. Capture correctness against a known signal (drive a pod GPIO at a known rate,
   capture it, check the decoded VCD).
4. Coexistence sanity: confirm whether the LA on PIO2 (streamer off) *could*
   coexist with SWD on PIO1, to decide if the swap should be forced or optional.

## Open decisions (for review)

1. **Mutual-exclusion model.** Always swap SWD out for the LA (simple, matches
   the stated constraint), or let the arbiter allow LA-on-PIO2 + SWD-on-PIO1 to
   coexist when the streamer is off and pins don't overlap (more flexible, more
   states to test)? Leaning: arbiter enforces generically; default to swapping
   SWD out, allow coexistence only once validated.
2. **Output format.** VCD host-side (universal, recommended) vs sigrok native vs
   raw + a separate decoder.
3. **DMA dependency.** Commit to DMA (verify `rp2.DMA` first) with the polled
   path as a labelled fallback - or ship polled-only first for a simpler initial
   cut, add DMA after. Leaning: verify DMA, build it primary, keep the fallback.
