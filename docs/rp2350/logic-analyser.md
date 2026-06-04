# RP2350 pod: PIO logic analyser + PIO arbiter

**Status: implemented and hardware-validated, including the live Wi-Fi
round-trip.** The capture engine, the VCD decoder, the bounded (non-wedging)
socket teardown, and the full `Pod.logic_analyse` over Wi-Fi (capture -> host ->
VCD) are validated. The earlier "la_stream hangs over Wi-Fi" wedge was root-caused
and fixed: the LA had been placed on **PIO2**, which on the RP2350 Pico 2 W is the
live **CYW43 Wi-Fi** block (not PIO0, as an RP2040 carryover assumption had it).
Building the LA state machine on PIO2 corrupted the running CYW43 SM and
hard-wedged the chip whenever Wi-Fi was actively servicing a socket. The LA now
runs on **PIO0** (the free block); the real client streams reliably over Wi-Fi.
See the PIO map below and the bisection write-up in the checklist.

Two coupled pieces:

1. A PIO **logic analyser** (LA): sample a set of DUT pins at a configurable
   rate into a RAM ring buffer, with a trigger, then stream the capture to the
   host and decode it to a standard waveform format.
2. A runtime **PIO arbiter** so the LA, the SWD debug stack, and CYW43 Wi-Fi
   never collide on a PIO block. It reserves PIO2 (Wi-Fi) and records the LA
   (PIO0) and SWD (PIO1) claims (see the budget below). The LA and SWD are on
   independent blocks; `la_capture`/`la_stream` still swap SWD out first as a
   conservative default.

## The PIO budget (corrected for the RP2350 Pico 2 W)

The RP2350 has 3 PIO blocks (4 SMs each, 32 instruction words each). The
**authoritative block map lives in code at `annealage_pod.debug.pio_arbiter.PIO_MAP`**
(the single source of truth; this doc and others point there rather than
restating it). In summary: **PIO0 = logic analyser** (free block, `sm_id=0`),
**PIO1 = SWD** (owns GP14/GP15), **PIO2 = CYW43 Wi-Fi** (reserved, off-limits).

Important: on the RP2350 Pico 2 W the CYW43 driver claims a free SM that can
reach its high-numbered WL pins (`pio_claim_free_sm_and_add_program_for_gpio_range`
in the pico-sdk), which lands on **PIO2 SM0** - NOT PIO0 as on the RP2040. This
was confirmed by reading the PIO enable registers (PIO2 `CTRL=0x1`, SM0 running;
PIO0/PIO1 idle; the recipe is in `PIO_MAP`'s comment). Building a state machine on
PIO2 while Wi-Fi is live corrupts the running CYW43 SM and hard-wedges the whole
chip (REPL dead, Ctrl-C dead, recover only by power-cycle). So **the LA must use
PIO0** and PIO2 is reserved.

The arbiter reserves PIO2 for `cyw43` and records LA (PIO0) and SWD (PIO1)
claims so a consumer cannot silently stomp a live one. Because the LA (PIO0) and
SWD (PIO1) are now on independent blocks, they can in principle run at the same
time; `la_capture`/`la_stream` still close any SWD session first as a
conservative default (clean DUT state), but the mutual-exclusion is a policy
choice, not a hardware constraint. A clean SWD release (`SWDPio.release()`:
`active(0)` + `remove_program` + free pins) is still used so repeated
setup/teardown does not leak PIO instruction memory.

## PIO arbiter

`annealage_pod/debug/pio_arbiter.py` - the single owner of PIO block / SM / pin /
program-space claims, so no two consumers fight over the same silicon.

```python
# One process-wide arbiter instance.
class PioArbiter:
    # Static map of what is permanently reserved (PIO2 = CYW43 Wi-Fi).
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

## Hardware-validation checklist

1. [x] `rp2.DMA` present, and the PIO-RX DREQ-paced FIFO -> RAM ring works: a
   1 kHz PWM captured at **1 MHz via DMA** decoded to exact 500-sample
   half-runs, 48.8% duty, 1000.0 Hz. (This early capture-engine check ran on PIO2
   SM10, RXF `0x50400028`, DREQ 22 - before the PIO2/CYW43 collision was found;
   production is PIO0 SM0, RXF `0x50200020`, DREQ 4, validated in item 6.)
   Polled fallback also gap-free at 100 kHz. (2026-06-03)
2. [x] SWD full teardown (`SWDPio.release`: `remove_program` + SM free) frees
   PIO1: `ops.info` (DPIDR `0x2ba01477`) -> `la_capture` swap -> `ops.info`
   rebuild -> second swap -> `ops.info` again, all clean. Leak-free across
   repeated swaps. (2026-06-03)
3. [x] Capture correctness: validated polled (100 kHz) and DMA (1 MHz); the LA
   module's `capture()` recovers the signal with immediate and rising-edge
   triggers. VCD decoder unit-tested (`tests/test_vcd.py`).
4. [x] Socket teardown is non-wedging: `srv.settimeout`/`cl.settimeout` bound
   both `accept` and the data phase, so a missing/flaky host connection raises
   `ETIMEDOUT` and frees the REPL instead of hanging (confirmed: a minimal
   listener with no client raised ETIMEDOUT cleanly).
5. [x] DMA capture coexists with active Wi-Fi: `la_capture` (DMA) ran with the
   CYW43 link up and Wi-Fi survived (before/after both connected, `.133`). So
   the capture does not disturb the management link.
6. [x] **Live Wi-Fi streaming round-trip via the host client** (`Pod.logic_analyse`
   -> `la_stream` -> host receive -> VCD): working and reliable on PIO0. The real
   client completes in <1s per capture (3/3 back-to-back, words=250, complete=True,
   valid VCD) over an active Wi-Fi link. (2026-06-04)

   Root-cause write-up (the earlier "hangs over Wi-Fi"): an out-of-band USB-CDC
   observer + a tight-step bisection found the wedge was the `rp2.StateMachine(...)`
   construction inside `capture()` - and only when run tightly (no yields) while
   the Wi-Fi REPL connection was being serviced. A fully-instrumented run (a print
   between every step) always completed, because each print yields and forces a
   Wi-Fi-servicing window - a classic Heisenbug. The decisive evidence:
   `probe_trunc(2)` (no StateMachine) completed; `probe_trunc(3)` (+StateMachine)
   wedged; `probe_raw(0,9)` (full setup on PIO0) completed. Reading `PIO->CTRL`
   showed PIO2 SM0 enabled (CYW43) and PIO0 idle: the LA's `sm_id=10` was PIO2,
   the live Wi-Fi block. Moving the LA to PIO0 fixed it. The "DMA contention"
   hypothesis in the prior note was wrong; it was PIO-block aliasing with CYW43.
7. [ ] LA(PIO0)+SWD(PIO1) simultaneous coexistence: now possible (independent
   blocks) but not yet validated; `la_capture`/`la_stream` still swap SWD out
   first. A follow-up could drop the swap to capture DUT pins mid-debug-session.

## Decisions (settled 2026-06-03)

1. **Mutual-exclusion model: swap SWD out (conservative default).** Starting the
   LA closes any SWD session first (`SWDPio.release`: `remove_program` + free
   GP14/15); SWD rebuilds lazily on the next debug op. The LA (PIO0) and SWD
   (PIO1) are on independent blocks so they *could* run together; the swap is a
   policy choice for a clean DUT state, not a hardware requirement, and could be
   dropped to enable capturing DUT pins mid-debug-session (item 7).
2. **Output format: VCD**, decoded host-side. Opens in GTKWave / PulseView
   (sigrok) / most viewers, no extra runtime dependency.
3. **Capture transport: DMA primary, polled fallback.** `rp2.DMA` is confirmed
   present on the pod build; the DMA ring is the real-rate path, with the
   Python-polled FIFO read as a labelled degraded fallback.
