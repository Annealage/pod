# RP2350 pod: SPI target and logic analyser concurrency on PIO0

The SPI target (`annealage_pod.spi_target`) and the PIO logic analyser
(`annealage_pod.debug.logic_analyser`) are both PIO0 consumers, and today they are
mutually exclusive: each `claim()`s the whole block through the arbiter, so a live
one blocks the other with `PioConflict`. This plan makes them coexist on PIO0 at
the same time, on separate state machines.

The two blocks that are off PIO0 stay put and are untouched by this work: SWD on
PIO1, CYW43 Wi-Fi on PIO2 (reserved, building a SM there wedges the chip). See
`annealage_pod.debug.pio_arbiter.PIO_MAP`.

## Why bother

The payoff is watching the bus the pod is driving. When the pod is an SPI
peripheral, an agent has no independent view of what actually went over the wire
past the SPI target's own DMA counters. Run the LA on the same four SPI pins at the
same time and it recovers the real MOSI/MISO/SCK/CS waveform, so a test can
cross-check the SPI target's reported bytes against an independent capture of the
line, catch bit-slip or a wrong CPOL/CPHA, and trace framing without a second
instrument. The LA reading the SPI pins is legal because sampling is input-only
(`in_(pins, w)`); only MISO is a pod output, and multiple state machines may read a
GPIO that one of them drives. The weaker case, LA on unrelated pins while the SPI
target runs, falls out of the same change for free.

## Validated premise: selective per-program PIO teardown (de-risk spike)

The whole design rests on one thing MicroPython had to support and that the current
code does not exercise: removing ONE program from a PIO block that holds two,
without disturbing the other program or its running state machine. The block's
32-word instruction memory is shared across all four SMs, and both consumers today
free it with a no-arg `rp2.PIO(block).remove_program()` that wipes the entire block
(`logic_analyser.py:116`, `spi_target.py:545`) - fine when you own the block alone,
fatal to a co-tenant.

Spiked on the rig (MP 1.29.0-preview, 2026-07-31), PIO0, three constant-pushing
programs sized so only two fit at once (A=16 words, B=3, C=15; A+B+C=34 > 32):

- Built A on sm0 and B on sm1; both ran (read back 13 and 21 from their FIFOs). Two
  programs coexist in one block on distinct SMs.
- A control build of C on top of A+B failed with `OSError(12)` (ENOMEM), proving the
  sizes actually contend for the 32 words rather than both fitting by luck.
- `rp2.PIO(0).remove_program(prog_a)` removed only A. B kept running through it
  (still read 21). So selective removal leaves the co-tenant's program and its live
  SM untouched.
- C then built into the slots A had vacated (B+C=18 fits only if A's 16 words were
  actually reclaimed) and ran alongside B (both read back 27 and 21). Selective
  removal genuinely frees the space, and three-way coexistence during a swap works.
- Teardown left `PIO0 CTRL=0x0`, nothing leaked.

So `remove_program(prog)` with a specific program object is the mechanism the whole
plan needs, and it works on this build. This is the piece that was worth proving
before writing any of the code below.

Caveat the spike does NOT cover: it used one `add_program` per program object. Our
consumers each build a fresh program object per session (the LA assembles per
width/trigger, the SPI target per mode/pins), so each is added once and removed
once; the "same object added at two offsets" case does not arise and was not
tested. If a future refactor caches and reuses a program object across sessions,
re-verify removal targets the right offset.

## Resource budget on PIO0

Ample, which is why this is worth doing rather than forcing a choice:

- Instruction words: SPI target program is the per-bit shift loop (~10 words), LA
  program is `in_(pins, w)` plus at most two trigger `wait`s (2-4 words). Together
  ~14 of 32. Room to spare, and the spike's 34-word overflow shows where the wall is.
- State machines: 4 in PIO0. SPI target takes one, LA one. Two free.
- DMA channels: SPI target uses 2 (MISO TX ring `_tx`, MOSI RX ring `_rx`), LA uses
  1 (RX FIFO to RAM). RP2350 has 16 channels; CYW43 holds 2 (ch0/ch1 on PIO2).
  `rp2.DMA()` auto-allocates a free channel, so no manual arbitration, but leaked
  channels masquerade as protocol bugs (see the `pod-pio-leaked-dma-masquerade`
  lesson), so both consumers must deinit their channels on teardown, which they do.
- PIO IRQ: the SPI target uses a CS-deassert soft IRQ to reparse and repoint its
  MISO DMA; the LA polls DMA completion and raises no PIO IRQ, so there is no IRQ
  contention on the block.

## Phase A: arbiter per-SM claims, not per-block

`pio_arbiter` is block-granular today: `_claims` is `block -> owner`, and `claim()`
rejects any second owner on a block (`pio_arbiter.py:46-59`). That is exactly what
forbids coexistence. Move it to per-SM granularity:

- Key claims by `(block, sm)` instead of `block`. `claim(owner, block, sm)` rejects
  only a clash on the same SM (or a reserved block); two owners on the same block,
  different SMs, both succeed.
- Keep the reserved-block check whole-block: PIO2 stays entirely unclaimable
  regardless of SM.
- `release(owner)` drops every `(block, sm)` that owner holds.
- Keep a block-level view for diagnostics (`holder(block)` returning the set of SM
  owners, `status()` snapshot) so the map stays legible.
- `PIO_MAP` stays the single source of truth for block assignment and reserved
  state; extend its per-block `sm` note to name the concurrent tenants on PIO0
  (SPI target and LA) rather than implying one owner. Callers keep pointing at
  `PIO_MAP`, they do not restate it.

Callers to update for the new signature: `swd_pio` / `ops` (SWD claims PIO1 sm0),
`logic_analyser` and `spi_target`. SWD is unaffected in behaviour, it just passes
its SM explicitly.

## Phase B: per-program teardown for both PIO0 consumers

Convert both consumers off the no-arg block wipe to removing only their own program,
using the mechanism the spike validated. Same shape as the SWD instruction-memory
fix (`swd_pio.py`, task #5): track the program object, remove exactly it, and do so
even on the partial-build failure path.

- Retain the built program object on the instance (`self._prog`) at build time,
  assigned before any post-`add_program` step that can still fail.
- Teardown calls `rp2.PIO(block).remove_program(self._prog)` when `self._prog` is
  set, never the no-arg form. `logic_analyser._teardown` and `spi_target.deinit`
  both change here.
- The failure/partial-build path (SPI target already routes through full `deinit`
  after the arbiter fix; the LA must gain the same) removes the own program too, so
  a half-built consumer never leaks a program onto a block a co-tenant is using.

Note the safety-net loss this accepts: the no-arg wipe also happened to clear any
program some other code leaked on the block. On a shared block you cannot wipe, so
that net is gone by necessity; leaks must be fixed at the source (as task #5 did),
not masked by a neighbour's teardown. A stray-program guard, if wanted later, has to
be selective, never a block wipe.

## Phase C: distinct state machines and pin roles

- Assign non-colliding SMs at the entry points: SPI target on PIO0 sm0 (its
  default), LA on PIO0 sm1. Both already take an `sm_id` argument; the ops /
  peripherals entry points must pick distinct ones and claim them per Phase A rather
  than both defaulting to sm0.
- Pins: for the headline use case the LA's `base_pin`/`width` are pointed at the SPI
  pin span (GP16 MISO, GP17 CS, GP18 SCK, GP19 MOSI) as inputs. MISO is a pod output
  driven by the SPI SM; the LA samples it as an input, which is allowed. Document
  that the LA must not be given output init on shared pins (it never drives, so this
  is a don't-regress note, not new code).

## Phase D: runtime coexistence

Wire the two through the host tooling so an agent can bring both up and have them
live together:

- `peripherals` keeps the SPI target instance in its registry (`_INST`) as now; the
  LA stays a per-call capture. Bringing up the LA while the SPI target is resident
  must claim sm1 and succeed, not `PioConflict`.
- Confirm no DMA-channel leak across an SPI-target-up, LA-capture, LA-teardown cycle
  (dump the DMA CTRL block per the leaked-DMA lesson if a capture comes back
  garbled). Auto-allocation should keep them disjoint; the check is a guard against
  a leaked channel racing the SPI FIFOs.
- The single-core cooperative runtime is unchanged: neither consumer runs
  MicroPython in the per-byte path (both are DMA-fed), so two live at once do not add
  per-byte load. The LA capture loop already `sleep_ms(1)`s so it does not starve
  Wi-Fi; that stays.

## Phase E: hardware validation gate

Run both at once on the rig and cross-check, using the SWD-puppet bench (pod drives
its own SWD to make the nRF52840 SPIM the controller, per the SPI target validation
setup):

- SPI target on sm0 as the peripheral, LA on sm1 tapping GP16-19, puppet clocks a
  known byte pattern.
- Assert the LA-recovered MOSI/MISO bytes match the pattern the puppet sent and the
  bytes the SPI target reports, at a few modes and clock rates within the validated
  125 kHz to 8 MHz band.
- Assert bring-up/teardown ordering is leak-free: SPI-up then LA-up then LA-down
  then SPI-down, and the reverse, each leaving `PIO0 CTRL=0x0` and no claimed SM in
  the arbiter.

Record the measured result at the gate and re-cut anything left, per the
dynamic-workflow rule in `overview.md`.

## Out of scope

- Same-CS write-then-read turnaround in the SPI regfile personality: unrelated to
  concurrency, tracked separately as a deferred SPI-target item (the pointer moves
  only in the CS-deassert IRQ, never mid-frame).
- LA and SWD coexistence: SWD is on PIO1, so it is already independent of PIO0; this
  plan does not touch that relationship beyond the arbiter signature change.
