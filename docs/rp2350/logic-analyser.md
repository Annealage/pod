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

It is a module (not a class) of process-wide bookkeeping:

```python
PIO_MAP                  # authoritative block -> {owner, sm, reserved, note} (source of truth)
claim(owner, block)      # grant a block to `owner`; raise PioConflict if held/reserved; idempotent per owner
release(owner)           # drop every block claimed by `owner` (caller has torn its program/pins down)
holder(block)            # who owns a block (claimed or reserved), or None
status()                 # {block: owner, ...} snapshot (reserved + claimed)
```

There is no separate `to_logic_analyser`/`to_swd` entry point; `ops` drives the
swap directly. `ops.la_capture`/`la_stream` call `ops.close()` (which releases
the `swd` claim and frees PIO1) then `claim("la", 0)`; `ops._ensure` claims PIO1
for `swd` and `ops.close()` releases it.

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
- `Pod.logic_analyse(base_pin, width, rate, depth, trigger=..., out_path='cap.vcd',
  names=...)` invokes `ops.la_stream` over the REPL, connects the data socket,
  receives the header + packed words, and decodes to VCD. The pod side runs the
  SWD swap, capture, and stream; SWD rebuilds lazily on the next debug op.
- CLI `pod la <label> --pins 16-23 --rate 1e6 --depth 20000 [--trigger 16:rise]
  --out cap.vcd`; MCP tool `logic_analyse`.

## Using the logic analyser

One capture: it samples a contiguous block of pod GPIOs into RAM, streams the
packed words to the host, and writes a `.vcd` you open in GTKWave / PulseView
(sigrok). It runs on PIO0; starting a capture first closes any live SWD debug
session (the conservative swap), so do captures and single-stepping as separate
steps.

**Pin model.** A capture is defined by `base_pin` (the lowest GPIO) and `width`
(how many *contiguous* GPIOs). Channel k samples `GP(base_pin + k)`:

| `--pins` | base_pin | width | channels |
|---|---|---|---|
| `16` | 16 | 1 | ch0 = GP16 |
| `16-19` | 16 | 4 | ch0=GP16, ch1=GP17, ch2=GP18, ch3=GP19 |

So the pins you wire must be a contiguous run; pick `base_pin`/`width` to cover
them. `--names CLK,MOSI,MISO,CS` labels channels low-pin-first in the VCD.

**Which pod pins to use.** Capture pins must be free header GPIOs in the
`base_pin .. base_pin+width-1` range (PIO0 addresses GP0-GP31, so keep
`base_pin + width <= 32`). On the pod:

- Free for capture: **GP16-GP22** and **GP26-GP28** (also GP0-GP9 if unused).
- Avoid **GP14/GP15** (SWD SWDIO/SWCLK), and **GP10/GP11** if the I2C target
  peripheral is in use.
- **GP23/24/25/29 are not on the header** - they are the internal CYW43 Wi-Fi
  pins (PIO2), so you cannot and must not touch them.

`GP16-GP21` is the recommended default block (contiguous, clear of SWD and I2C).

**Capture parameters.**

- `rate` (Hz): actual rate is `150e6 / clkdiv` (clkdiv 1..65535), so ~2.3 kHz to
  150 MHz; the result reports the *actual* rate after clkdiv rounding. Choose at
  least ~4-10x the fastest edge you need to resolve.
- `depth` (samples): bounded by an 80 KB buffer. `words = ceil(depth*width/32)`
  must be `<= 20000`, i.e. `depth <= 640000/width` (640k @ width 1, 80k @ width
  8). Over that, depth is silently clamped and `depth` in the result is the
  actual.
- `trigger=(pin, cond)`, `cond` in `rise`/`fall`/`high`/`low`: capture waits for
  the condition then runs; omit for immediate. `complete=False` in the result
  means the trigger never fired within the time budget.

**Invoking.**

```bash
# CLI: capture an SPI bus on GP16-19, 2 MHz, start on CS falling
pod la mypod --pins 16-19 --rate 2e6 --depth 8000 --trigger 19:fall \
    --names CLK,MOSI,MISO,CS --out spi.vcd
```

```python
# Python client
from pod.client import Pod
pod = Pod(address="192.168.0.133")
r = pod.logic_analyse(base_pin=16, width=4, rate=2_000_000, depth=8000,
                      trigger=(19, "fall"), names=["CLK", "MOSI", "MISO", "CS"],
                      out_path="spi.vcd")
# r: {ok, out_path, width, rate, clkdiv, words, complete, samples}
```

The MCP tool `logic_analyse` takes the same fields (`base_pin`, `width`, `rate`,
`depth`, `trigger=[pin, cond]`, `out_path`, `names`). Open the `.vcd` with
`gtkwave spi.vcd` or in PulseView.

## Wiring the analyser to a DUT (electrical)

The capture pins are plain RP2350 GPIO inputs. The two rules that protect the
pod and give clean captures:

- **3.3V logic only.** RP2350 GPIOs are **not 5V tolerant**. Drive a pod capture
  pin only with 0-3.3V signals. For 5V (or other) logic, put a level shifter or
  a resistor divider in line - never connect a >3.3V signal directly.
- **Common ground is mandatory.** Tie a pod `GND` pin to the DUT ground, or the
  samples are meaningless (and you risk the pins). Keep capture leads short for
  fast edges.

Inputs are high-impedance with no pull configured, so a wired-but-undriven or
unconnected channel reads noise. Only capture channels you have actually wired
(set `width` to the number of connected signals), or expect junk on the rest.

## Guiding a user to wire it up (agent checklist)

When helping a user set up a capture, walk them through:

1. **Identify the signals** to observe on the DUT and their logic voltage.
   Confirm each is <= 3.3V; if higher, tell them to add a level shifter before
   touching a pod pin.
2. **Pick a contiguous pod GPIO block** for the channels from the free set
   (default `GP16-GP21`; avoid GP14/15 and GP10/11-if-I2C). The lowest pin is
   `base_pin`, the count is `width`; channel order is low-pin-first.
3. **Wire it:** each DUT signal to its pod `GPn`, and a **pod GND to DUT GND**
   (shared reference, not optional). Short leads for MHz signals.
4. **Choose a trigger** if the event is sparse (e.g. CS falling, or a clock
   edge), otherwise immediate.
5. **Pick rate and depth:** rate >= ~4-10x the fastest edge; keep
   `ceil(depth*width/32) <= 20000` words.
6. **Run** `pod la <label> --pins <base-last> --rate <Hz> --depth <n>
   [--trigger <pin>:<cond>] --names <a,b,...> --out cap.vcd`, then open
   `cap.vcd` in GTKWave/PulseView. Map the names to the wired signals.

If `complete=False`, the trigger never fired (check the trigger pin/edge or wire
it); if a channel is flat/noisy, check that pin's wire and the shared ground.

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
7. [x] Real multi-channel DUT capture: an nRF52840 dongle drove 50 kHz / 25 kHz
   PWM onto pod GP11 / GP10; a 2-channel capture (`base_pin=10, width=2`, 1 MHz,
   depth 8000) over Wi-Fi decoded to **50.1 kHz / 25.0 kHz** - exact, channels
   correctly mapped (low pin = ch0), `complete=True`. Confirms the DUT-wiring
   guide end-to-end (external signal source, real pins, decode fidelity).
   (2026-06-05)
8. [x] LA(PIO0) + SWD(PIO1) + CYW43(PIO2) simultaneous coexistence: validated.
   With a live SWD session on PIO1 (nRF52840 target, DPIDR `0x2ba01477`), running
   an LA capture on PIO0 left SWD fully intact - fresh FICR reads (part `0x52840`,
   flash/ram, `ficr0`) identical before and after, `dpidr` unchanged, capture
   `complete=True`. The arbiter held all three blocks at once
   (`{0:'la', 1:'swd', 2:'cyw43'}`) and Wi-Fi stayed up. So the swap in
   `la_capture`/`la_stream` is not required; dropping its `ops.close()` would let
   an agent capture DUT pins mid-debug-session without losing halt/breakpoint
   state (pending - it changes a validated default). (2026-06-05)

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
