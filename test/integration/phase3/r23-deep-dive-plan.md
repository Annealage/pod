# R23 deep-dive: plan to verify or refute the IDF-is-the-bottleneck hypothesis

## Context

R23 findings concluded that the IDF host stack on DWC2 ESP32-S3 caps
bulk URB throughput at ~88 URBs/sec/pipe (vs ~9000/sec the FS bulk wire
would support). That conclusion triggered the R24 TinyUSB pivot, which
hit a multi-step kernel-deadlock wall (see `r24-wip-history.md`).

The conclusion itself is unverified: R22 instrumentation used
`xTaskGetTickCount()` at 10 ms ticks (`CONFIG_FREERTOS_HZ=100`), too
coarse to localise per-URB latency to within the IDF stack vs other
pipeline stages.

Commit `d4f1e3b` (R23 deep dive) already landed the missing piece:
microsecond timing via `esp_timer_get_time()` around
`usb_host_transfer_submit` and the IDF callback. What's left is to
collect the data, interpret it, and decide the actual next move.

## What `d4f1e3b` measures

In `src/c_modules/usbhost/usbhost.c` `transfer_done_cb` (around line
615), per-URB:

- `t_submit_overhead` = `t_submit_post - t_submit_pre`, the synchronous
  cost of `usb_host_transfer_submit` itself.
- `t_idf_round` = `t_complete - t_submit_post`, time from the IDF
  accepting the submit to the callback firing on completion. Includes
  IDF event-loop scheduling, DMA handling, and wire transmission.

Aggregated over 100 URBs as `idf_timing: n=N avg_submit=X us
avg_round=Y us min_round=A us max_round=B us`.

For 128 B FS bulk, wire time is approximately:

- 128 B at 12 Mbps = ~85 us payload
- + USB protocol overhead (TOKEN, DATA, HSHK), ~25 us
- ~ **110 us total wire time** (per Beyond Logic USB FS analysis)

So if `avg_round` ≈ 110 us, IDF is doing its job at wire speed and
the bottleneck lives elsewhere. If `avg_round` >> 110 us (e.g., 11 ms
as Little's Law estimated from R23 findings), IDF really is the cap.

## Steps

### Step 1: capture timing data under sustained load

1. `bash src/tools/build.sh && bash src/tools/flash.sh`. Confirm the
   `idf_timing:` line shape compiles in.
2. `mpy-dev cycle esp32-s3 ; sleep 18` to start cleanly.
3. Attach the Pico CDC: `sudo usbip attach -r 192.168.0.166 -b $(usbip list -r 192.168.0.166 | grep -oE "1-[0-9]+" | head -1)`.
4. Open a UART monitor in one terminal:
   `bash src/tools/monitor.sh > /tmp/r23-deep-dive-uart.log`.
5. In another terminal, run the throughput bench:
   `python3 test/integration/phase3/cdc_throughput.py read_test 2>&1 | tee /tmp/r23-deep-dive-bench.log`.
   Run for at least 1000 URBs (typically ~10 seconds at 88 URB/s).
6. Stop the monitor, save the log. The `idf_timing` lines accumulate
   one per 100 URBs. Aim for at least 10 lines (1000 URBs).
7. Detach: `sudo usbip detach -p 0`.

Capture state in `r23-deep-dive-progress.log` per the existing
convention: timestamp + commit + headline numbers.

### Step 2: interpret the timing

Three cases for `avg_round`:

**Case A: avg_round ≈ 110 us** (wire-time bound, IDF is fine)

- IDF is NOT the bottleneck. The R23 findings CORRECTION conclusion is
  refuted. The R24 pivot was triggered by a measurement artifact.
- Re-investigate where the per-URB residence time of ~11 ms actually
  goes. Likely candidates downstream of IDF:
  - Responder task wake latency (may still be tick-bound somewhere)
  - TCP send latency in `tx_ret_submit` (the lwip_writev change in R21
    helped but may still be a chunk)
  - Lane queue + dispatcher hop overhead
- Action: instrument those stages with the same `esp_timer_get_time()`
  pattern, find where the 10+ ms is sitting.

**Case B: avg_round in the 1-5 ms range**

- IDF is part of the bottleneck but not all of it. There's wire time +
  some scheduling overhead in the IDF event loop or DMA handling.
- Actions in priority order:
  1. Bump the IDF host stack task priority. Default is around 5; try
     20+ to see if scheduling latency drops.
  2. Check `CONFIG_USB_HOST_HW_BUFFER_BIAS` and related IDF kconfig
     knobs that affect DMA handling.
  3. If neither helps, IDF really is rate-limited and a TinyUSB or
     P4-HS pivot is justified.

**Case C: avg_round > 10 ms** (matches R23's Little's-Law estimate)

- IDF is the bottleneck, conclusion confirmed. The R24 retro's
  recommendation to retry TinyUSB after fixing the fs-cp hang is the
  right path.
- But before retrying R24, dig into WHY IDF is slow. Sub-questions:
  - Is `avg_submit` (synchronous part) high? Then IDF rejects/queues.
  - Is `avg_round` high but `avg_submit` low? Then DMA / event loop is
    the cap.
- Action: open a focused investigation. The fs-cp deadlock from R24
  retro is the bigger ship blocker than throughput, so any TinyUSB
  retry needs that fixed first regardless.

### Step 3: emit per-EP and per-direction breakdown

The current instrumentation aggregates all URBs into one histogram. For
diagnosis, knowing whether bulk-IN, bulk-OUT, or interrupt-IN dominates
the latency is useful. Extend the static counters to per-EP-direction
(at least separate IN vs OUT, ideally per-ep_addr too). Single small
patch to `transfer_done_cb`.

Re-run step 1 with this in place. Compare the breakdowns. cdc-acm
multi-step pattern is bulk-OUT-heavy (writes script chunks) followed
by bulk-IN-heavy (read responses); if one direction is much slower
than the other, that's a structural finding.

### Step 4: write up findings, update cross-links

1. Create `test/integration/phase3/r23-deep-dive-findings.md` with the
   numbers, the case (A/B/C above), and the chosen next direction.
2. Replace the "Caveat from R24 retro" banner at the top of the
   `r23-findings.md` CORRECTION section with a "Verified" or "Refuted"
   line pointing at the new findings doc.
3. If the conclusion changes the R24 status (e.g., refuted means R24
   was pursuing a non-issue), update `r24-wip-history.md` with a
   forward-pointer.
4. Update the `R24` row in `plan/overview.md` risk register.

### Step 5: file the next concrete task

Based on the case identified:

- Case A: a "find the real bottleneck" investigation task with the
  responder/TCP-side instrumentation as the first step.
- Case B: a "tune IDF host scheduling" task with the kconfig knobs to
  try first.
- Case C: a "fix R24 fs-cp deadlock" task (the kernel-D-state issue
  from `r24-wip-history.md` "what's left to debug" section).

## Iteration budget

Step 1: 30-60 minutes (build/flash already work; bench script exists).
Step 2: 1-2 hours of analysis if the data is clean.
Step 3-4: 1-2 build/flash cycles for per-EP breakdown.
Step 5: <1 hour to file the follow-up.

Total: half a day if the timing data points cleanly at one of the three
cases. Multi-day if the data is ambiguous or reveals a fourth case.

## Out of scope

- Any architectural code changes (tx_order refactor, lane structure,
  responder priority) until the timing data is in. R23-R24 already
  did several rounds of this without a verified target; the goal of
  this iteration is data, not code.
- The R24 fs-cp kernel-deadlock work. That's a separate task and
  shouldn't be tackled until step 5 confirms TinyUSB-pivot direction.

## Hardware

Same as R23: ESP32-S3 dev board (mpy-dev label `esp32-s3`), Pico DUT
on busid 1-N. Wi-Fi PS already off (R23 step 1). RET_SUBMIT batching
already enabled (R23 step 2). cdc_throughput.py read_test already
captures 11.2 KiB/s baseline (`r23-progress.log`).
