# Measurement: per-EP lane tasks vs queue-in-completion

## Question

mpy-pod's USB/IP server serialises per-EP via dedicated FreeRTOS lane tasks (`src/c_modules/usbip/usbip_server.c:849` `lane_task_arg_t`, dispatched from `intake_submit` line ~1484). The TinyUSB usbipd example (upstream PR #3637) solves the same problem with an inflight pool flagged `queued`, drained inline from the completion callback (`examples/host/usbipd/src/usbip_submit.c`).

Which is faster in practice on ESP32-S3, and by how much? Worth keeping the lane-task model, or is the simpler queue-in-completion good enough?

The TinyUSB host backend currently forces `USBIPD_PIPELINE_DEPTH = 1` (R27 gotcha #2 in `src/c_modules/usbhost/usbhost.c` per `docs/spec.md:152`), so the lane model's nominal parallelism advantage is already neutralised at the per-EP level. Cross-EP parallelism still applies.

## Hypothesis

* On a single cdc-acm bulk-IN workload, both models cap at the same throughput because both serialise per-EP and the TinyUSB host stack itself is 1-URB-per-EP.
* The lane-task model wins on cross-EP workloads (e.g. cdc-acm bulk-IN + interrupt-IN concurrent) because each lane runs on its own task and the completion callbacks can interleave without contending for one drain context.
* The lane-task model pays ~3 KB stack per lane (32 lanes \* 3 KB ~= 96 KB) and FreeRTOS task overhead per URB.
* For the mpy-pod single-DUT case with at most one bulk-IN + one interrupt-IN active, the cross-EP advantage is small.

## Bench

### Setup

* Annealage Pod: ESP32-S3 dev board, mpy-pod firmware HEAD.
* DUT: Raspberry Pi Pico W with stock MicroPython on OTG (vid 2e8a:0005).
* Host: Linux box on same LAN; `usbip attach -r <ip> -b 1-1`.

### Workloads

1. **bulk-IN read-only.** `cdc_throughput.py` from `test/integration/phase3/` measures device->host DATA IN. Single endpoint, ideal for revealing the per-EP ceiling.

2. **bulk-IN + interrupt-IN concurrent.** Bring up cdc-acm and trigger the interrupt-IN line-state notifications by toggling DTR rapidly while a `cdc_throughput`-style read is running. Two endpoints, exercises cross-EP parallelism.

3. **bulk-OUT under bulk-IN read-ahead.** `mpremote fs cp` of a 10 MB file while a background reader holds the IN endpoint busy. Bidirectional, exercises both directions of the same composite interface.

### Variants

* **A: lane-task (current).** HEAD.
* **B: queue-in-completion.** Patch `intake_submit` to skip `lane_dispatch` for cdc-acm bulk endpoints; drain via a single completion-context worker. See "Variant B implementation sketch" below.

### Metrics per workload x variant

| Metric | How |
|---|---|
| Sustained throughput | `cdc_throughput.py`'s 16 KiB buf size row, MB/s. |
| Per-URB submit-to-complete latency | `inflight_urb_t.t_submit` (already captured at `usbip_server.c:425`); add log on completion to compute delta. |
| Lane task stack high-water marks | `uxTaskGetStackHighWaterMark()` per lane after a sustained run. |
| Lane task wakeups per second | Increment a counter in `lane_task` each loop iteration; sample over 10 s. |
| Heap free (min during run) | `esp_get_minimum_free_heap_size()`. |

### Procedure

```
# Capture variant A (HEAD).
git log -1 --oneline > /tmp/perf-variant.txt
test/integration/phase3/cdc_throughput.py /dev/ttyACM<N> | tee /tmp/perf-A-w1.txt
# (workload 2 / 3 scripts TBD - manual repro for now)

# Switch to variant B.
git checkout -b perf-variant-B
# Apply Variant B patch (sketch below)
make ... && flash ...
test/integration/phase3/cdc_throughput.py /dev/ttyACM<N> | tee /tmp/perf-B-w1.txt
```

### Variant B implementation sketch

In `intake_submit`, replace the lane dispatch path with a single
post-completion drain. Minimum viable change:

1. Add a per-conn `pending_q` (linked list of `inflight_urb_t*`,
   ordered by arrival) and a per-conn `active_eps` bitmap (uint64_t,
   one bit per (ep, direction) tuple).
2. In `intake_submit`: if the (ep, dir) bit is already set in
   `active_eps`, link the slot into `pending_q` and return. Else
   set the bit, call `usbhost_*_transfer` directly, return.
3. In the lane completion callback: clear the (ep, dir) bit, walk
   `pending_q` for the first slot matching this (ep, dir),
   submit it (sets the bit again), return.
4. Drop the 32 lane tasks; the completion runs in usbhost's
   callback context which is fine since `tx_ret_submit` is already
   reentrant-safe.

Estimated change: ~150 LOC added, ~250 LOC removed. Net simplification.

## Decision criteria

* If variant B is within 5% on workloads 1 and 2 and matches or beats memory budget, **adopt variant B** and remove lane tasks.
* If variant A wins by >10% on workload 2 (cross-EP), keep lane tasks and document the measurement here.
* If variant A wins on workload 2 but only marginally, defer the decision until a real multi-EP user workload exists; the simpler model is preferable in the absence of clear evidence.

## Output

Record numbers in `measurement-per-ep-serialisation-results.md` alongside this file, with the git commit refs for each variant. Include the raw `cdc_throughput.py` table output, not just the summary, so future readers can spot artefacts.
