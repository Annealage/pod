# R27 Phase 1 revised findings: Phase 1 fix was a workaround, not a root-cause fix

## Honest amendment to r27-phase1-findings.md

The Phase 1 findings doc (`r27-phase1-findings.md`, written 2026-05-05)
claimed the fs-cp deadlock was fixed by:

- TinyUSB submodule patch backporting hathach/tinyusb#3632 (txsts
  re-read inside per-packet FIFO write loop in `handle_txfifo_empty`).
- Caller-level MPS-sized OUT chunking in `usbhost.c` so each
  bulk-OUT URB > MPS is split into a sequence of single-packet
  sub-URBs each issued via its own `tuh_edpt_xfer` call.

The 5/5 fs-cp + 30/30 mpremote success that ratified the fix was
real, but **the fix did not address the root cause**; the bug is
intermittent and the Phase 1 acceptance run was lucky. Sonnet's
attempt to convert the chunk size from a hardcoded 64 to a
descriptor-based MPS lookup (which still produces 64 for FS
endpoints; structurally identical at the 64-byte chunking level)
reproduced the same wedge pattern that motivated Phase 1.

This doc records the true bug shape and the open status.

## Reproduction (2026-05-06)

Three captures of the wedge with `R27_DEADLOCK_TRACE=1`:

1. `/tmp/r27-mps-fscp-uart.log` (sonnet, no per-URB trace) — wedge
   at uptime 35.6s on `ep=0x02`, watchdog age 2.07s.
2. `/tmp/r27-opus-repro-uart.log` (opus, 1st repro) — wedge on
   seq=209 ep=0x02 len=128, **chunk 1 cb fired (alen=64) but chunk
   2 never followed**. Watchdog age 2.07s.
3. `/tmp/r27-opus-repro2-uart.log` (opus, 7 PASS / 1 FAIL run) —
   wedge on seq=2454 ep=0x02 len=128, **NO chunk cb at all**.
   Watchdog age 2.09s.
4. `/tmp/r27-phase1-retest-uart.log` (opus, Phase 1 commit
   `34583f1` retest with hardcoded 64) — wedge on seq=497 ep=0x02
   len=128, chunk 1 cb fired (alen=64), chunk 2 missing. Same
   pattern as MPS-aware repro.

All four wedges share a common shape:

| seq | OUT len | chunks | chunks cb'd | preceded by |
|---|---|---|---|---|
| 209  | 128 | 2 | 1 | IN sub seq=208 ~2 ms before |
| 2454 | 128 | 2 | 0 | IN sub seq=2453 ~3 ms before |
| 497  | 128 | 2 | 1 | IN sub seq=496 ~3 ms before |
| 54   | 167 | 3 | 0 | (Phase 0 baseline, no chunking; first multi-packet OUT) |

Length-1024 (16-chunk) and length-256 (4-chunk) OUTs succeed
mostly, mixed in the same logs. **No wedge ever observed on
single-packet (≤ MPS) OUT URBs.**

## What the bug isn't

- **Not a chunk-size mismatch.** Sonnet's MPS-lookup variant returns
  64 for the Pico CDC FS bulk OUT (same as the hardcoded 64). Both
  variants reproduce the wedge.
- **Not a TinyUSB upstream PR #3632 prerequisite.** The submodule
  patch is in place in both Phase 1 and the regression. The PR
  reduces the failure rate (per opus's earlier observation that
  without it, the FIRST multi-packet OUT wedges deterministically;
  with it, only some multi-chunk OUTs wedge intermittently). It
  does not eliminate the wedge.
- **Not specific to caller-level chunking.** The original Phase 0
  code without chunking (one `tuh_edpt_xfer` per multi-packet OUT)
  hits the SAME wedge, just earlier and more reliably.
- **Not introduced by sonnet's HS-aware MPS lookup.** Sonnet's diff
  is structurally identical at the chunk-size level for FS bulk
  EPs; the wedge it exposed already existed.

## What the bug looks like to be

Consistent with HiFiPhile's diagnosis on hathach/tinyusb#3623,
comment 2026-05-03: the DWC2 host slave-mode FIFO write path
races against device NAKs on bulk-OUT. Specifically, our
reproduction shows that **a bulk-OUT URB is most likely to wedge
when a bulk-IN URB on the same device is submitted within ~3 ms
before the OUT (or in the OUT's chunked re-submit window).** When
both EPs wedge simultaneously (as in seq=2454/seq=2453), it
points at the **shared non-periodic request queue + FIFO** as the
contended resource:

- `channel_send_in_token` (called from IN xfer start AND from
  IN NAK retry in the ISR) busy-waits on `req_queue_avail` in
  ISR context.
- `handle_txfifo_empty` (called from NPTX_FIFO_EMPTY ISR) checks
  `req_queue_available` and skips writing the OUT packet to FIFO
  if the queue is full.
- If the IN URB is NAK-looping (Pico cdc-acm has nothing to
  send), the request queue is repeatedly consumed and freed.
  An OUT submitted in this window may struggle to land its
  packet in the FIFO, and HW state can wedge.
- The exact wire-level sequence that causes XFER_COMPLETE to
  never fire on the OUT channel is not established here. We
  observe the symptom (no XFER_COMPLETE event for 2 s) but
  cannot read DWC2 channel registers post-mortem because the
  watchdog already cleared them by the time the firmware
  responds to a new control request.

Upstream's recommended workaround on tinyusb#3623 is **enable DMA
mode** (`CFG_TUH_DWC2_DMA_ENABLE=1`). This is the path
P-R-O-C-H-Y reported as working. **DMA mode is not currently
available on ESP32-S3** because `dwc2_esp32.h` only defines L1
cache handling for ESP32-P4 (where `SOC_CACHE_INTERNAL_MEM_VIA_L1CACHE`
is set). Enabling DMA on S3 would silently corrupt buffers due
to missing cache flush/invalidate around DMA descriptors.

## What the Phase 1 fix actually achieved

Compared to Phase 0 (no chunking, no PR #3632):

| Pattern | Behaviour |
|---|---|
| Phase 0 baseline | First multi-packet OUT wedges deterministically (seq=54, 167 bytes). 0/N fs-cp PASS. |
| PR #3632 only | Wedge moves from "first multi-packet OUT" to "intermittent multi-packet OUT". 1/5 fs-cp PASS. |
| PR #3632 + chunking (Phase 1 + sonnet's MPS) | Wedge restricted to multi-chunk OUTs (>=128 bytes); single-packet OUTs always succeed. Per-iteration failure rate appears ~5-15%. 7-19 iters before first failure observed. |

The chunking is a NET POSITIVE — it reduces the failure rate
significantly — but it is NOT the root-cause fix.

## Iteration cost since Phase 1

Phase 1 declared 5/5 PASS based on a single capture. Looking at
the actual data more carefully:

- Phase 1's first 5/5 attempt (trace-on, `r27-fix3-5x-uart.log`):
  passed. Watchdog fires=0.
- Phase 1's next 5/5 attempt (trace-off, `r27-final-5x-uart.log`):
  iter 2 wedged. Watchdog fired on `ep=0x02` at 2.05s.
  Documented but ascribed to "flake".
- Phase 1's third 5/5 attempt (trace-off, retry,
  `r27-final2-uart.log`): 5/5 PASS. Watchdog fires=0.

The Phase 1 conclusion took the third (passing) run as the
verdict. With hindsight, the second run was the **real** result
and the first/third were lucky.

The opus 2026-05-06 retest reliably reproduces the wedge in 1-8
iterations. Phase 1's 5/5 success rate is consistent with a
random failure rate of ~10-20% per fs-cp.

## Decision: keep the chunking, document the residual race

Reverting the chunking commit `34583f1` would make things worse
(failure rate goes from ~10-20% per fs-cp to ~100% on the first
multi-packet OUT). The chunking is doing useful work even though
it's not a root-cause fix.

Sonnet's MPS-lookup change (committed at `6bac095`) is also kept.
It removes a hardcoded 64 fallback that would mis-chunk on HS
endpoints.

The TinyUSB submodule patch (PR #3632 backport) is also kept.
It reduces the failure rate.

## Open follow-ups, in priority order

1. **TinyUSB-side fix.** The race is at TinyUSB DWC2 slave-mode
   level. Possible angles:
   - File a follow-up issue on tinyusb#3623 with our specific
     reproduction (multi-chunk OUT wedge in the chunked re-submit
     pattern). HiFiPhile's note suggests they may have ideas.
   - Add deeper instrumentation to TinyUSB's `handle_channel_irq`
     and `handle_txfifo_empty` (gated on a debug flag) to capture
     channel-state at the moment of the wedge. Submit findings
     upstream.

2. **DMA-mode enablement on ESP32-S3.** The cleanest fix is to
   enable DMA mode and bypass the slave-mode race entirely. This
   requires:
   - Adding ESP32-S3 cache-handling support to TinyUSB's
     `dwc2_esp32.h` (or working around the L1 cache requirement
     via DRAM-only DMA buffers).
   - Confirming with Espressif that DMA-mode DWC2 host on S3 is
     actually viable on this silicon revision.
   - Out of R27 scope but high-value for production.

3. **Defer the chunk re-submit out of cb context.** A speculative
   workaround (untested): the chunk N+1 re-submit happens inline
   in the cb (USBH task) context. It might be that TinyUSB doesn't
   handle re-entrant `tuh_edpt_xfer` from a cb on the same EP as
   well as it should. Test by posting the inflight to a queue and
   doing the re-submit from a separate task. Cheap to try.

4. **Per-device IN/OUT serialisation at the lane level.** Force
   bulk-IN and bulk-OUT to not be in flight simultaneously per
   device. Eliminates the race window but kills duplex throughput;
   measure cost in Phase 2.

5. **Faster watchdog with kernel-friendly recovery.** Currently
   the watchdog fires at 2 s and runs `tuh_edpt_close + tuh_edpt_open
   + CLEAR_FEATURE`. The recovery sometimes leaves the kernel-side
   cdc-acm in a confused state (`clear_feat=0` was observed on
   some recoveries). A 100 ms watchdog with cleaner kernel-side
   re-sync would mask the race well enough for production while
   the upstream fix is pending.

## Action this dispatch

- Sonnet's MPS-aware lookup committed (clean improvement).
- This findings doc replaces the optimistic claim in
  `r27-phase1-findings.md`.
- Phase 1 acceptance is REVISED to "intermittent fs-cp PASS,
  reliable repro in 1-8 iterations". Phase 2 throughput
  measurement should NOT proceed until the wedge is addressed:
  the wedge would corrupt long-running benches.
- Recommendation: pursue follow-up #2 (DMA-mode S3) or #3
  (deferred chunk re-submit) before attempting Phase 2.
