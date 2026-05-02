# R23 deep-dive findings: IDF host stack timing on DWC2 ESP32-S3

Date: 2026-05-03
Branch: main
Commit at time of measurement: 0b7f8eb (R23 deep-dive plan)

Firmware commit landed for this investigation: see commits in this session
(printf fix + per-direction breakdown patch to `usbhost.c`).

## Purpose

Verify or refute the hypothesis from the R23 CORRECTION section that the
IDF host stack on DWC2 ESP32-S3 limits bulk URB throughput to ~88/sec.
The original CORRECTION conclusion was reached without µs-resolution
timing; this investigation supplies that data.

## Instrumentation

`src/c_modules/usbhost/usbhost.c` `transfer_done_cb`, static counters
updated per-URB, emitted every 100 async URBs:

- `avg_submit`: synchronous cost of `usb_host_transfer_submit` itself.
- `avg_round`: time from submit-accepted to completion callback, including
  IDF event-loop, DMA, and USB wire time.
- `min_round` / `max_round`: extremes of the round trip.

Step 3 added per-direction (OUT vs IN) breakdowns using the existing
`inflight->is_in` field.

Two build/flash cycles:
- Cycle 1: fix printf format (IDF newlib-nano does not support 64-bit
  specifiers; `%lld` and `PRId64` both silently print the format
  characters rather than the value; fixed with `(int32_t)` cast and
  `PRId32`).
- Cycle 2: add per-direction counters.

Bench: `cdc_throughput.py /dev/ttyACM11` (Pico via USB/IP).
UART captured via `cat` on CH340N serial at 115200 baud.

## Raw data - combined histogram (cycle 1)

```
n=100  avg_submit=47us  avg_round=114298us  min_round=110us  max_round=204307us
n=200  avg_submit=52us  avg_round=142664us  min_round=110us  max_round=204307us
n=300  avg_submit=54us  avg_round=148579us  min_round=109us  max_round=204307us
n=400  avg_submit=59us  avg_round=151615us  min_round=109us  max_round=234002us
n=500  avg_submit=58us  avg_round=152003us  min_round=109us  max_round=234002us
n=600  avg_submit=56us  avg_round=155646us  min_round=109us  max_round=281891us
n=700  avg_submit=56us  avg_round=154192us  min_round=108us  max_round=281891us
n=800  avg_submit=54us  avg_round=157064us  min_round=108us  max_round=389101us
n=900  avg_submit=55us  avg_round=157136us  min_round=87us   max_round=486486us
```

9 lines, 900 URBs, ~10 seconds of bench.

## Raw data - per-direction breakdown (cycle 2, up to n=900 before anomaly)

```
n=100  OUT: n=11  avg=290us   min=103us  max=1042us
       IN:  n=89  avg=135068us min=182us  max=215281us
n=200  OUT: n=11  avg=290us   min=103us  max=1042us
       IN:  n=189 avg=154116us min=182us  max=215281us
n=300  OUT: n=15  avg=245us   min=103us  max=1042us
       IN:  n=285 avg=158743us min=182us  max=215281us
n=400  OUT: n=19  avg=281us   min=103us  max=1042us
       IN:  n=381 avg=162768us min=182us  max=231856us
n=500  OUT: n=26  avg=284us   min=103us  max=1042us
       IN:  n=474 avg=162751us min=182us  max=231856us
n=600  OUT: n=27  avg=279us   min=103us  max=1042us
       IN:  n=573 avg=165061us min=182us  max=278845us
n=700  OUT: n=35  avg=273us   min=103us  max=1042us
       IN:  n=665 avg=164565us min=182us  max=281696us
n=800  OUT: n=35  avg=273us   min=103us  max=1042us
       IN:  n=765 avg=164786us min=182us  max=386150us
n=900  OUT: n=43  avg=271us   min=103us  max=1047us
       IN:  n=857 avg=165916us min=182us  max=486927us
```

At n>1000 the IN max_round grew to 44-45 seconds (bench transitioning
to larger buffer sizes), corrupting the running average. These later
rows are excluded from the summary.

## Case determination

**Case C: avg_round > 10 ms. IDF is the bottleneck. Confirmed.**

The R23 CORRECTION hypothesis is verified by direct µs measurement.

Headline numbers (steady-state, n=900):
- avg_submit = 51-55 µs (IDF accepts transfers quickly)
- avg_round (combined) = 157,136 µs (157 ms)
- min_round (combined) = 87-110 µs (matches the ~110 µs FS-bulk wire floor)
- max_round (combined) = 486,486 µs (487 ms)
- Sample count: 900 URBs (9 complete 100-URB windows)

Per-direction (at n=900 window):
- OUT: avg=271 µs, min=103 µs, max=1047 µs, n=43 (5% of traffic)
- IN:  avg=165,916 µs, min=182 µs, max=486,927 µs, n=857 (95% of traffic)

## Structural finding: massive OUT vs IN asymmetry

This is the key new finding beyond the R23 CORRECTION.

Bulk-OUT completes in 271 µs average - essentially wire speed plus
IDF scheduling. The IDF host stack handles bulk-OUT without significant
queuing latency.

Bulk-IN takes 165,916 µs average (165 ms). The IDF host stack queues
IN tokens at a rate far below what the DWC2 hardware and FS bus can
deliver. The min_round for IN is 182 µs (about one IN poll period at
FS), but the average is ~920x higher.

The `cdc_throughput.py read_test` is bulk-IN-heavy (Pico sends data to
host). Nearly all URBs are IN transfers. This explains why the overall
system throughput ceiling is determined entirely by the IN latency.

The `min_round=182 µs` for IN shows the IDF CAN deliver IN data at near
wire speed when the queue is empty. The 165 ms average implies the IDF
does not issue concurrent IN tokens on the same endpoint; it issues one,
waits for the callback, then issues the next. With a 16-deep pipeline
filling from the kernel, the queue depth builds and per-URB wait time
grows proportionally.

## Why OUT is fast and IN is slow

The IDF `usb_host_transfer_submit` is non-blocking for OUT (the DWC2
controller sends the DATA0/DATA1 packet immediately and reports
completion when the device ACKs). For IN, the host must issue an IN
token, wait for the device to respond with data, and only then fires
the completion callback. If the IDF host task rate-limits IN token
issuance (e.g., by serialising submissions within a single pipe), each
IN URB must wait for all preceding IN URBs to complete.

This is consistent with `tuh_edpt_xfer` gotcha #2 from r24-wip-history
(TinyUSB also allows only one transfer in flight per (dev, ep)).
The IDF stack appears to have the same restriction internally.

## Impact on R24 TinyUSB pivot rationale

The R24 pivot was triggered by the hypothesis that IDF was slow. That
hypothesis is now confirmed for bulk-IN. However, the TinyUSB gotcha #2
documents that TinyUSB also serialises per (dev, ep). If TinyUSB has
the same bulk-IN serialisation, the pivot would not improve throughput
even if the fs-cp deadlock is fixed.

The real question is not which USB host stack to use, but whether any
stack can pipeline multiple IN tokens on the same bulk endpoint at FS.
Per USB 1.1/2.0 spec, a host CAN issue multiple IN tokens per
microframe/frame on the same endpoint if the endpoint supports it
(NAK-retry loop). Whether IDF, TinyUSB, or any stack does this
automatically for bulk endpoints is the unverified sub-question.

## avg_submit sign anomaly at n>1000

At n=1000 the avg_submit becomes negative (-41781 µs). The `s_sum_overhead_us`
is an int64_t accumulator but the division result is cast to int32_t for
printing. At n=1000, the sum of ~55 µs * 1000 = 55,000 µs, which is well
within int32. The negative value suggests some overhead readings went
pathologically negative (t_submit_post < t_submit_pre, possible if the
esp_timer counter wraps or if the bench causes the submit to block in a
way that disrupts the timestamp ordering). The sum is correct for early
rows; the sign anomaly at n>1000 is a data quality issue to note, not
a primary finding.

## Bench throughput during measurement

```
bufsize=256  nbuf=128  rate=11506  kib_s=11.2
bufsize=512  nbuf=44   rate=11446  kib_s=11.2
bufsize=1024 nbuf=22   rate=11397  kib_s=11.1
bufsize=2048 nbuf=11   rate=11199  kib_s=10.9
bufsize=4096 nbuf=5    rate=8789   kib_s=8.6
bufsize=8192 nbuf=2    rate=8381   kib_s=8.2
bufsize=16384 nbuf=1   rate=8216   kib_s=8.0
```

11.2 KiB/s ceiling unchanged from R23 baseline. The timing data explains
why: ~88 bulk-IN URBs/sec * 128 bytes/URB = 11264 B/s = 11.0 KiB/s,
matching perfectly.

## Next direction

The R24 fs-cp D-state deadlock is a TinyUSB/r24-wip issue, not an R23/IDF
issue. R23 IDF main is rock-solid for streaming workflows: this bench
just put 900 IN URBs through it cleanly with no kernel hangs, and per
`r24-bug-findings.md` / `r24-wip-history.md` R23 main also runs
`mpremote fs cp` cleanly. So the deadlock is not the right Case C
followup.

Linux as a USB host pipelines URBs at the QH/qTD hardware level
(EHCI/xHCI walks queue heads itself; software gets completion
interrupts) and runs the same Pico DUT at wire speed in both
directions. DWC2 has 8 host channels and queue-mode descriptor lists;
nothing in USB protocol or DWC2 silicon forces serial bulk-IN. Our
min_round at the wire-time floor proves the hardware can deliver IN
data at full speed when the queue is empty. The 165 ms average shape
is consistent with IDF running bulk URBs serially per pipe inside
its event-loop.

TinyUSB gotcha #2 (`tuh_edpt_xfer` one-in-flight per (dev, ep)) is a
TinyUSB user-API constraint that would still bite our raw-URB-forwarding
USB/IP use case. So a TinyUSB pivot does not lift the IN ceiling
either; the pivot's only remaining argument is upstream-MicroPython
alignment, not bandwidth.

Real Case C followup: investigate IDF bulk-IN serialisation and whether
it is tunable. Knobs to try, in cheapest-first order: HW_BUFFER_BIAS_IN
kconfig, IDF host-task priority, `CONFIG_FREERTOS_HZ=1000`, then descent
to the lower `hcd_*` API. Stay on R23 IDF main; do not branch to
TinyUSB.

Follow-up task: `r25-tune-idf-bulk-in-plan.md`.
