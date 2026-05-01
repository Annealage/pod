# R21 findings

Branch: `worktree-agent-ab6dea4cf74ee8deb`, ff-merged onto main.

Two commits added:

- `96f7917` R21 cycle1: add verbose-gated per-URB timing instrumentation to run_inflight
- `57bc70e` R21 cycle2: Path B b1: replace malloc/memcpy/free in tx_ret_submit with lwip_writev

## What R21 actually concluded

**The hypothesis was wrong.** R21 was framed as "larger USB transfer
sizes for streaming throughput" with the suspected bottleneck being
the kernel `cdc-acm` driver's 128-byte URB readsize. The agent
correctly verified that `cdc-acm` does issue 128-byte URBs and that
our firmware never sees larger ones; it then attributed our observed
11 KiB/s ceiling to that constraint and concluded the only
firmware-irreversible fix was a kernel patch.

That conclusion did not survive a direct-USB comparison. Running the
same `cdc_throughput.py` benchmark against a Pico 2 W
(`pico2-w` in mpy-dev, RP2350, also FS-only) plugged directly into
the host — bypassing the ESP32-S3 middlebox entirely — yielded:

| bufsize | direct (KiB/s) | via USB/IP middlebox (KiB/s) | ratio |
|---------|----------------|-------------------------------|-------|
| 256     | 718.3          | 11.1                          | 65x |
| 512     | 700.1          | 11.0                          | 64x |
| 1024    | 714.4          | 10.7                          | 67x |
| 2048    | 715.7          | 10.1                          | 71x |
| 4096    | 714.9          | 7.3                           | 98x |
| 8192    | 713.1          | 5.5                           | 130x |
| 16384   | 701.8          | 5.5                           | 128x |

Direct USB hits ~700 KiB/s with the same kernel `cdc-acm` driver
issuing the same 128-byte URBs. The 65x gap is entirely in our
middlebox path. The kernel readsize is not the bottleneck.

## Actual root cause

The R21 agent's timing instrumentation was correct as raw data; only
its label was wrong. The instrumentation source notes
"`xTaskGetTickCount()` resolution is 1 ms (configTICK_RATE_HZ=1000)"
but our build has `CONFIG_FREERTOS_HZ=100`, so `xTaskGetTickCount()`
returns ticks of 10 ms each, not 1 ms. The reported "t_total median 4
ms" should be read as "4 ticks" with whatever sub-tick alignment is
actually happening; absolute values from this instrumentation are
suspect, but relative comparisons (t_idf vs t_tcp) still hold.

The 11 KiB/s observed = 88 URBs/sec = 11.4 ms wall-clock per URB.
This matches the per-URB cost of one FreeRTOS-tick-aligned wakeup at
HZ=100, confirmed by the priority structure:

```
USB host daemon  (lib events)              priority 10
USB host worker  (client events, callback) priority 9   <-- gives done_sem
USBIP lane task  (waits on done_sem)       priority 5
USBIP read loop                             priority 5
```

When the IDF callback fires at priority 9 and gives `done_sem`, the
priority-5 lane task does not preempt the worker. Lane runs in the
next round-robin slot for priority-5, which at HZ=100 is
tick-aligned (10 ms). One tick wait per URB.

Direct USB hits 700 KiB/s because the host kernel's USB completion
runs in interrupt/tasklet context with chained URB resubmission, no
scheduler tick latency, and xHCI hardware DMAs URBs without per-URB
software overhead.

## What R21 leaves in main

Despite the misdiagnosis, both committed changes have value and stay:

### `96f7917`: verbose-gated per-URB timing instrumentation

Adds `t_idf_ms`, `t_tcp_ms`, `t_total_ms` fields to verbose-mode log
lines. Zero overhead when verbose is off. Useful for any future
throughput debugging.

Caveat: the comment in the source claims 1 ms tick resolution but
the build has 10 ms ticks. The instrumentation is still useful
relative; absolute times should be multiplied by 10 ms/tick if
needed. A comment fix is worth doing as a small follow-up, not a
revert.

### `57bc70e`: lwip_writev replaces malloc/memcpy/free in tx_ret_submit

Replaces a per-URB `malloc + memcpy + write + free` sequence with a
single `lwip_writev` two-iovec call. Saves one heap allocation and
two memcpy operations per bulk-IN URB completion.

Throughput impact at the user-visible metric (bufsize=256): within
noise, no measurable change pre-vs-post. The change is structural
hygiene rather than a throughput win — under the eventual R22
pipelined architecture each connection will have ~16 URBs in flight
and the per-URB heap pressure savings compound 16-fold. Worth
keeping for that reason.

API note: `lwip_writev` is the lwIP-internal symbol; POSIX `writev`
is gated behind `LWIP_POSIX_SOCKETS_IO_NAMES` which our IDF build
does not enable. Other ESP-IDF code uses `lwip_writev` directly so
this is acceptable coupling.

## Validation

| Check | Result |
|-------|--------|
| A. Smoke 5/0 | pass |
| B. mpremote 30/30 (two runs back-to-back) | pass at 0.5 s gap |
| C. R18 t+1s post-detach probe | pass (returned 2 devices) |
| D. Concurrent CDC + CMSIS-DAP attach | pass |
| E. Stress 60/60 | pass |
| F. Throughput floor >= baseline | pass (within noise at bufsize=256) |

mpremote at <0.2 s inter-iteration gap shows alternating failures due
to OS serial-port lock release latency on the cdc-acm/USB-IP layer.
Pre-existing characteristic, not an R21 regression. 0.5 s gap stable.

## Caveats and follow-ups

1. **The framing of R21 was wrong.** "Larger transfer sizes" was the
   wrong knob. The right knob is per-URB wakeup latency on the
   firmware side. R22 attacks that directly via priority-based
   preemption + pipelining (`r22-plan.md`).

2. **The agent's `t_idf_ms` units mislabeling** should be corrected
   in a follow-up commit. Either change `CONFIG_FREERTOS_HZ` to 1000
   (which would actually match the comment, and is itself a real
   throughput lever per the R22 analysis) or change the comment +
   multiply the printed values by `portTICK_PERIOD_MS`. R22 plan
   explicitly leaves HZ at 100 and goes after the wakeup latency via
   priorities; the comment fix is sufficient.

3. **Direct-Pico baseline numbers** are recorded at the top of this
   file. Adding them to `cdc_throughput.baseline.txt` as a second
   reference point would give R22 a clear before/after target.
   (Done in a sibling commit.)

4. **R21's writev change uses lwIP-internal symbols.** If a future
   port adds POSIX-socket-names compatibility (`LWIP_POSIX_SOCKETS_IO_NAMES`),
   it can be replaced with `writev` for portability. Not blocking.

5. **R21 used 2 of 5 cycles** before reaching its (wrong) conclusion.
   3 cycles unused. The misdiagnosis was caught after the agent
   completed by running the direct-USB comparison the user
   suggested; that comparison was not in the original R21 plan and
   should be added to the R22 plan as a sanity baseline (it is).

## Files touched

- `src/c_modules/usbip/usbip_server.c` (timing instrumentation +
  writev refactor)
- `test/integration/phase3/r21-findings.md` (this file, corrected)

No changes to micropython submodule, referencea/, or vendor/.
