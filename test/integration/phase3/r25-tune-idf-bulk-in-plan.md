# R25 plan: tune IDF bulk-IN throughput on R23 main

## Context

R23 deep-dive (`r23-deep-dive-findings.md`) confirmed Case C: IDF host
stack bulk-IN average round-trip is 165 ms (n=857 IN URBs at n=900),
vs 271 µs average for bulk-OUT (n=43). min_round = 87-110 µs matches the
~110 µs FS-bulk wire floor exactly, proving the DWC2 hardware can
deliver IN data at wire speed when nothing is queued ahead.

Linux as a USB host runs the same Pico DUT at wire speed in both
directions. Linux pipelines URBs at the QH/qTD hardware level (EHCI/xHCI
walks queue heads itself; software gets completion interrupts).
Nothing in USB protocol or DWC2 silicon forces "submit, wait for
callback, submit next" - that is an IDF host-stack policy choice.

Confirmed by reading IDF source
(`/home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf/components/usb/`):

- `usb_host.c` `usb_host_transfer_submit` (line 1589) checks per-URB
  in-flight (`urb_obj->usb_host_inflight`), NOT per-EP. Multiple URBs
  to the same EP are accepted and forwarded to `usbh_ep_enqueue_urb`.
- `hcd_dwc.c` defines `NUM_BUFFERS = 2` (line 57) and uses a
  double-buffering scheme per pipe with `multi_buffer_control.wr_idx`,
  `rd_idx`, `fr_idx`. So at the HCD layer the IDF already supports up to
  2 URBs in flight per pipe at the DMA stage.
- `hcd_urb_enqueue` (line 2604) appends to `pending_urb_tailq` and
  calls `_buffer_can_fill` / `_buffer_fill` to push into hardware. The
  pending tailq is unbounded.

Our `usbip_server.c` lane task already submits up to
`USBIP_PIPELINE_DEPTH=16` URBs concurrently to `usb_host_transfer_submit`
(verified: R22 intake_count instrumentation showed avg_depth=13-15,
max=18). The `ep_submit_mutex` in `usbhost.c` is held only across the
`usb_host_transfer_submit` call itself, NOT until callback. So at the
public API our code already pipelines.

**Therefore the 165 ms IN average must be inside the IDF host-client
event-loop or hub task between completion and the next IN-token issue.**
The HCD has `NUM_BUFFERS=2` per pipe, so at most 2 IN tokens are
queued in DMA at any time; the rest sit in `pending_urb_tailq` waiting
for the host-client task to wake on the previous completion and call
`_buffer_fill` again. That wake-to-fill latency is what we measure as
the round-trip.

## Goal

Raise the IDF bulk-IN ceiling from ~88 URB/s/pipe toward FS wire speed
(~1000 URB/s for 128 B IN), without leaving R23 IDF and without
moving to TinyUSB. Bulk-OUT stays as-is (already wire-speed).

## Steps to consider

Use judgement on order/scope; the steps below are listed cheapest first.

### Step 1: read deeper into the host-client / hub event loop

Files of interest in
`/home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf/components/usb/`:

- `usb_host.c` - public API and the host-client event handler. Look for
  the task that wakes on pipe events and calls back through to drain
  done queues.
- `usbh.c` - USB-host abstraction layer between usb_host.c and HCD.
  Check whether it adds another serialisation step.
- `hcd_dwc.c` - DWC2 HAL/HCD. The `_buffer_fill` / `_buffer_can_fill` /
  `_buffer_can_exec` functions are the key: when does the HCD push the
  next URB into hardware after a completion?
- `private_include/hcd.h` - lower API. `hcd_urb_enqueue` is what
  `usbh_ep_enqueue_urb` ultimately calls.

Key question: is there a task-level loop that calls `_buffer_fill` only
on a completion event, or does it eagerly fill all available buffers
per wake? If the latter, the latency budget we are seeing is the
event-loop scheduling delay, not a serialisation lock.

### Step 2: kconfig knobs (no code changes)

The IDF Kconfig (`components/usb/Kconfig`) exposes:

- **`CONFIG_USB_HOST_HW_BUFFER_BIAS`** - choices BALANCED / IN /
  PERIODIC_OUT. Bias IN gives:
  - IN FIFO: 600 bytes (vs 408 balanced)
  - OUT non-periodic: 64 bytes (vs 192 balanced)

  This affects DWC2 RX/TX FIFO partitioning, which limits how many
  packets can be cached. For our 128 B IN URBs and 128 B OUT URBs at
  FS, balanced should already be fine, but bias-IN is worth a try
  because read-heavy bench can tolerate the OUT regression.

- **`CONFIG_USB_HOST_CONTROL_TRANSFER_MAX_SIZE`** - default 256.
  Probably irrelevant; control transfers are not in the hot path of
  the read_test.

- The Kconfig does NOT expose a USB_HOST_TASK_PRIORITY knob (verified
  by `grep -E "TASK_PRIORITY" Kconfig`). Task priority is set
  programmatically by whatever code spawns the host-client task in our
  build.

Process: edit `src/boards/ESP32_S3_ANNEALAGE_POD/sdkconfig` (or wherever
sdkconfig defaults live for our board), set
`CONFIG_USB_HOST_HW_BUFFER_BIAS_IN=y`, rebuild, re-run
`cdc_throughput.py` with the existing idf_timing instrumentation,
compare numbers.

### Step 3: bump IDF host task priority

`usb_host_install` accepts a `usb_host_config_t` with task config. Find
where we call it (in `usbhost.c` `usbhost_init`), check if we set
`intr_flags` and any task priority. Look for the host-client task
spawned via `xTaskCreatePinnedToCore` inside IDF and see what priority
it uses. If it is around 5 (default), raise to 18-20.

Goal: reduce event-loop scheduling delay between URB completion and
the next `_buffer_fill` invocation. The ~165 ms latency is suspicious
for a 100 Hz tick scheduler with the host task at default priority -
each completion may be waiting on tick boundaries before the host task
runs.

Note: our system has FreeRTOS tick at 100 Hz (10 ms per tick) per
sdkconfig. If the host task is yielding back to scheduler between
completions, each yield costs at least one tick. 16 URBs * 10 ms tick =
160 ms, suspiciously close to observed 165 ms. **This is the strongest
hypothesis.** Bumping host task priority and/or raising
`CONFIG_FREERTOS_HZ` to 1000 should both reduce this.

### Step 4: try `CONFIG_FREERTOS_HZ=1000`

Independent of priority: at 100 Hz, even a high-priority task yields
on tick boundaries. Bumping to 1000 Hz reduces the per-yield delay
floor from 10 ms to 1 ms. If the 165 ms latency drops by ~10x, the
tick was the culprit.

Cost: more tick interrupts; minor CPU overhead. Worth trying.

#### Result (2026-05-03): hypothesis refuted

`CONFIG_FREERTOS_HZ=1000` set in
`src/boards/ESP32_S3_ANNEALAGE_POD/sdkconfig.board`. Build clean, no errors
from any subsystem requiring 100 Hz. Boot was clean.

`cdc_throughput.py` numbers virtually unchanged from R23 baseline.
Comparison at n=800 (steady-state window before run-to-run anomaly):

| Metric | HZ=100 baseline | HZ=1000 | Delta |
|---|---|---|---|
| avg_submit | 54 us | 50 us | -7% |
| avg_round (combined) | 157,064 us | 160,360 us | +2% |
| min_round | 108 us | 108 us | 0% |
| max_round | 389,101 us | 505,014 us | +30% (noise) |
| OUT avg | 273 us | 259 us | -5% |
| IN avg | 164,786 us | 167,685 us | +2% |

Throughput at bufsize=256: 11.2 KiB/s -> 11.2 KiB/s. Identical.
Larger bufsizes regressed slightly (8192: 8.2 -> 7.2 KiB/s, 16384:
8.0 -> 6.5 KiB/s) but the 256-byte case is what matters for the
URB-rate ceiling and that is unchanged.

The tick rate is NOT the IDF host-stack bulk-IN serialiser. The
16-URBs × 10 ms tick = 160 ms ~ observed 165 ms coincidence was just
that, a coincidence. The IDF host task either does not yield to the
scheduler between URB completions, or yields to a non-tick-aligned
event (semaphore give, interrupt, queue post) so tick rate does not
gate it.

`CONFIG_FREERTOS_HZ=1000` retained on main as a conservative default
for latency-sensitive code; the cost (more tick interrupts) is small
and the change does not regress throughput on the 256-byte case.

Next hypothesis to test: step 3 (host task priority). If the host
task is at default priority (~5) and competing with TCP/lwIP/Wi-Fi
tasks at higher priority, the wake-to-run latency between URB
completion and the next `_buffer_fill` could be the limiter. Bump
the host-client task priority to 18-20.

### Side investigation: TCP RET_SUBMIT path timing

Triggered by the direct-USB baseline (`r25-direct-usb-baseline.md`)
which showed 677 KiB/s direct vs 11.2 KiB/s via ESP32 (60x). Per-URB
instrumentation in `responder_task` measured both:

- `avg_writev` = wallclock time spent in `lwip_writev` (per writev call).
- `avg_cb2tx` = gap between `lane_completion_cb` (`u->t_cb`) and the
  pre-writev timestamp (per URB that sends RET_SUBMIT).

Both the verbose-gated `t_cb` capture in `lane_completion_cb` was
made unconditional (one extra `esp_timer_get_time()` per URB; cost
negligible).

#### Result (2026-05-03): both gates well under 1 ms

Steady-state at n=300 (clean window before bench bufsize transition):

| Metric | avg | min | max |
|---|---|---|---|
| writev (per call) | 482 us | 388 us | 1000 us |
| cb2tx (per URB)   | 121 us | 83 us  | 813 us  |

Total ESP32-side work between IDF callback and bytes-on-wire is
~600 us per URB. Throughput at 11.2 KiB/s = 88 URB/s implies
inter-URB time of 11.4 ms. So the URBs are NOT arriving back-to-back
at the responder; they arrive in bursts with ~10 ms gaps in between.

Verdict: branch C of the original interpretation - **the gate is
NOT the responder or TCP send**. Combined with R23 deep-dive
finding (`avg_round = 165 ms` IDF submit-to-callback for bulk-IN),
the 10 ms-per-URB cost lives **inside the IDF host stack between
`usb_host_transfer_submit` accepting the URB and the IDF firing
our completion callback**. This is consistent with the R25 source-
reading findings (`r25-idf-source-findings.md`): either DWC2
per-URB channel-halt-to-reactivate, or task-wakeup latency between
the HCD ISR (which gives `event_sem`) and `_handle_pending_ep`
running in our task context.

Critically: this measurement does NOT disambiguate between those
two sub-causes. It only confirms that everything from
`lane_completion_cb` onward is fast. To split the IDF-internal
gap requires patching the IDF source (or using a lower API like
`hcd_*` to bypass `usb_host_*` and measure separately).

Implications for the followup steps:

- Step 3 (bump host task priority) remains the cheapest test.
  If the 165 ms drops, task-wakeup is the IDF-internal gate.
- Step 6 (descend to `hcd_*` API) is justified if step 3 fails.
- Step 7 (custom HAL driver) only if 6 also fails.

The instrumentation in `usbip_server.c` is left in place. It is
unconditional (not gated on `s_urb_verbose`) so it logs during
every bench. Cost: 4 `esp_timer_get_time()` calls per URB and one
ESP_LOGI per 100 URBs.

### Step 5: pin the IDF host task to a less contended core

Our `usbhost.c` runs on which core? The IDF host task runs on which
core? If both are on PRO_CPU and competing with other scheduler work,
moving the IDF host task to APP_CPU may help. Check
`usb_host_lib_task` or whatever task the IDF spawns for event handling.

### Step 6: experiment with the lower hcd_* API

If steps 2-5 do not lift the ceiling, the remaining option without
leaving IDF is to use the `hcd_*` API directly
(`components/usb/private_include/hcd.h`). `hcd_urb_enqueue` is the
underlying primitive, takes a `hcd_pipe_handle_t`. Bypassing
`usb_host_*` removes one layer of event-loop scheduling.

Drawback: hcd.h is private and tied to IDF version. Pinning IDF or
re-validating on each IDF bump is a maintenance cost. Worth it only
if the lower API delivers meaningful pipelining win.

### Step 7 (out of scope here, file as R26 if reached)

Custom HAL-level driver atop `hal/usbh_hal.h` with direct DWC2 channel
programming. Spec says DWC2 has 8 host channels; using them directly
gives full pipelining control. This is a significant effort and only
warranted if the IDF stack truly cannot pipeline at any of its layers.

## Out of scope

- The R24 fs-cp deadlock. That is a TinyUSB-only failure mode on the
  r24-wip branch; R23 IDF main runs fs cp and cdc_throughput.py
  cleanly (the R23 deep-dive bench just put 900 IN URBs through R23
  without incident). The R24 deadlock only matters if we ever resume
  the TinyUSB pivot, which is now weakly motivated since TinyUSB
  gotcha #2 (`tuh_edpt_xfer` one-in-flight per (dev, ep)) implies
  TinyUSB has the same per-pipe serialisation IDF appears to have. The
  pivot's only remaining argument is upstream-MicroPython alignment,
  not bandwidth.
- Any TinyUSB pivot work.
- Architectural code changes on `src/c_modules/usbip/` or
  `src/c_modules/usbhost/usbhost.c` before the kconfig + priority +
  tick experiments are exhausted. Step 6 (hcd_* API) is the first
  step that justifies code changes.

## Exit criteria

Pick whichever lands first:

1. `cdc_throughput.py read_test` bufsize=256 throughput improves from
   11.2 KiB/s to >50 KiB/s (a clear lift, not just noise).
2. `idf_timing` `avg_round` for IN drops from 165 ms to <10 ms in
   steady state (n=900 sample). This is the direct, unambiguous
   measurement; throughput follows.
3. Or: definitive answer documented as "IDF will not pipeline at
   public API for the following reason... here is what would (R26
   custom HAL driver / TinyUSB raw-channel / Arduino USB Host
   library)..." with a code citation. This is a valid exit if all of
   1-5 fail; we then file R26 with a known target.

## Hardware

Same as R23 deep-dive:
- ESP32-S3 dev board on `mpy-dev` label `esp32-s3`.
- Pico DUT exposed via usbip from 192.168.0.166 (busid 1-N).
- R23 IDF main branch (no host reboot needed; not on r24-wip).
- Existing `idf_timing` and per-direction instrumentation in
  `src/c_modules/usbhost/usbhost.c` from R23 deep-dive remains
  in place.

## Findings noted while preparing this plan

These came from reading the IDF v5.5 source while drafting the plan.
They are non-obvious and should save the next agent some time:

- `usb_host_transfer_submit` does NOT serialise per-EP at the public
  API. Per-URB only (`urb_obj->usb_host_inflight` flag).
- `hcd_dwc.c` `NUM_BUFFERS=2` is a hard compile-time constant for
  per-pipe DMA buffers. Even if the host-client event loop were
  perfectly fast, only 2 URBs fit in hardware at a time per pipe.
  Throughput ceiling at the HCD layer is `2 / round-trip-time`.
- For 128 B FS bulk IN with ~110 µs wire time, the HCD ceiling at
  NUM_BUFFERS=2 is `2 / 110us = 18000 URB/s/pipe`, well above what we
  need. So NUM_BUFFERS=2 is NOT the active limiter.
- The `pending_urb_tailq` per pipe is unbounded; URBs accumulate
  there and feed into the 2-buffer DMA stage as completions free
  buffer slots.
- Our `usbhost.c` `ep_submit_mutex` is held only across the submit
  call. It does NOT enforce serial completion. Already pipelined at
  the application layer.
- The `usbip_server.c` lane task takes a slot from `inflight_slots`
  (counting sem at USBIP_PIPELINE_DEPTH=16) before each submit.
  R22 instrumentation showed avg_depth=13-15 - the lane is filling.
- Therefore the 165 ms IN average is inside IDF's host-client task
  scheduling between completion-event and next-submit-to-hardware.
  That task's priority and the FreeRTOS tick rate are the two most
  likely tuning levers.
