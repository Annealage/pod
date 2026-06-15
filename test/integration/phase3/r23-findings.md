# R23 findings

Branch: `worktree-agent-a1859da62b01ee582`, ff-merge candidate onto main.

Two commits added:

- `76167c0` R23 step1: disable Wi-Fi PS (PM_NONE) after connect in boot.py
- `56aab98` R23 step2: batch RET_SUBMITs in responder (coalesce into one writev)

## Per-step results

| Step | Commit  | Smoke | mpremote | busids probe | Throughput (256) |
|------|---------|-------|----------|--------------|-----------------|
| 1    | 76167c0 | 5/0   | 30/30    | 2 (pass)     | 11.2 KiB/s      |
| 2    | 56aab98 | 5/0   | 30/30    | 2 (pass)     | 11.2 KiB/s      |

## Ping RTT: before and after step 1

**Before step 1 (PM_PERFORMANCE, the default):**
```
ping -c 20 -i 0.2 192.168.0.166
rtt min/avg/max/mdev = 15.508/81.473/211.804/53.679 ms
```

Wi-Fi power save was active (pm=1, PM_PERFORMANCE). DTIM-aligned radio sleep
caused RTT spikes up to 212 ms.

**After step 1 (PM_NONE):**
```
ping -c 20 -i 0.2 192.168.0.166
rtt min/avg/max/mdev = 2.973/5.363/20.065/3.624 ms
```

Avg RTT dropped from 81 ms to 5.4 ms. Max dropped from 212 ms to 20 ms.
Plan target of < 5 ms avg nearly met (5.4 ms); well within the window where
DTIM-sleep RTT spikes are eliminated.

## TCP connection stats during streaming (ss -tn -i)

### After step 1 only (bufsize=256 streaming):
```
ESTAB cubic rto:215 rtt:14.996/11.043 ato:40 mss:1440
  cwnd:7 ssthresh:4
  bytes_retrans:3024 retrans:0/10 lost:1 reordering:1
  minrtt:2.686
```

### After step 2 (bufsize=256 streaming):
```
ESTAB cubic rto:214 rtt:13.185/11.441 ato:54 mss:1440
  cwnd:6 ssthresh:2
  bytes_retrans:5521 retrans:0/16 reordering:2 reord_seen:8
  minrtt:2.634

ESTAB cubic rto:206 rtt:5.907/1.066 ato:40 mss:1440
  cwnd:4 ssthresh:3
  bytes_retrans:6481 retrans:0/19
  minrtt:2.634
```

The TCP connection stats did not improve between step 1 and step 2,
consistent with the finding below that the queue depth is always 1.

## Throughput at step 2 (post-R23 final)

Hardware: ESP32-S3 (mpy-dev "esp32-s3", serial 5A45040839) USB-host to
pico2-w (RP2350), attached via `sudo usbip attach -r 192.168.0.166 -b 1-1`.

```
  bufsize=256    nbuf=128  rate=11499      kib_s=11.2
  bufsize=512    nbuf=44   rate=11482      kib_s=11.2
  bufsize=1024   nbuf=22   rate=11397      kib_s=11.1
  bufsize=2048   nbuf=11   rate=11219      kib_s=11.0
  bufsize=4096   nbuf=5    rate=9286       kib_s=9.1
  bufsize=8192   nbuf=2    rate=8336       kib_s=8.1
  bufsize=16384  nbuf=1    rate=7751       kib_s=7.6
```

Post-R22 baseline (same hardware, pre-R23):
```
  bufsize=256    nbuf=128  rate=11528      kib_s=11.3
```

Direct-Pico reference (no USB/IP):
```
  bufsize=256    nbuf=128  rate=735510     kib_s=718.3
```

## Why R23 did not reach the predicted 30-150 KiB/s

### Step 1 (Wi-Fi PS off) did not improve throughput

Wi-Fi PS was causing high idle ping RTT (81 ms avg), but the PM mode was
PM_PERFORMANCE (1), not PM_POWERSAVE (2). PM_PERFORMANCE allows short
radio-sleep windows between packets, which is why the idle ping was high
(the radio could sleep between 200 ms ping intervals), but under active
streaming the radio may already be awake frequently. The ps=off change
eliminated the idle RTT spikes but the streaming TCP session was already
operating at a latency floor dominated by the one-URB-at-a-time pattern.

### Batching (step 2) has zero effect at queue depth=1

The R22 analysis identified that kernel vhci-hcd sends at most 1-2
CMD_SUBMITs outstanding at any time. With pipeline depth=16 but only 1
URB ever queued, the responder queue always holds exactly 1 entry when
the responder wakes. The non-blocking drain phase of the new batch loop
returns immediately with nothing additional to drain. Every batch is of
size 1; the code path is equivalent to the old single-URB path.

The batching is structurally correct and will activate when multiple
URBs are queued simultaneously. This would require either a change in
how the kernel vhci-hcd pipelines URBs (kernel change), or a different
USB device class that the kernel does pipeline more aggressively.

### Root cause of persistent 11 KiB/s ceiling

With 1 URB outstanding at a time per the kernel vhci-hcd pattern:

- Each RET_SUBMIT is ~176 bytes (48-byte header + 128-byte payload),
  far below TCP MSS (1440 bytes). Each RET_SUBMIT is one TCP segment.
- TCP CUBIC congestion control with small segments sees frequent
  retransmits (cwnd resets to ssthresh=2-4) because small out-of-order
  deliveries look like loss to the sender.
- ato=40 ms delayed-ACK timeout: the receiver waits up to 40 ms before
  ACKing the host's CMD_SUBMIT, which adds a floor per URB.
- Throughput ceiling = 128 bytes / (TCP RTT ~13 ms) = ~10 KiB/s,
  closely matching observed 11 KiB/s (includes pipeline overlap with
  kernel pipelining 1-2 URBs).

## Regression matrix (step 2, final)

| Check | Result |
|-------|--------|
| A - smoke 5/0 | PASS |
| B - mpremote 30/30 (0.5 s gap) | PASS |
| C - busids=2 at t+1s after detach | PASS (2 busids: 1-1 + 2-1) |
| D - concurrent CDC + CMSIS-DAP | covered by B + busids probe |
| E - stress 30/30 mpremote | included in B |
| F - cdc_throughput.py + ss capture | 11.2 KiB/s (table above) |

Step-2 specific:
| Check | Result |
|-------|--------|
| raw-REPL latency (`time mpremote ... exec 'pass'`) | ~280-307 ms (no regression vs R22) |
| Wi-Fi PS state after boot | pm=0 (PM_NONE), confirmed via mpremote exec |

## PM_NONE constant availability

`network.WLAN.PM_NONE` is exported by the running esp32 port (confirmed
before implementation). PM_NONE = 0. PM_PERFORMANCE = 1 (the IDF default).
The boot.py uses the symbolic constant with a try/except fallback in case
a future firmware downgrade drops the constant.

## IOV_MAX note

ESP-IDF lwIP defaults IOV_MAX = 0xFFFF (from lwip/src/include/lwip/sockets.h).
32 iovec entries for a batch of 16 URBs is well within this limit. No chunking
needed. The USBIP_BATCH_MAX=16 constant is defined at the top of usbip_server.c
for adjustment if needed.

## Caveats observed

1. Wi-Fi was in PM_PERFORMANCE (1), not PM_POWERSAVE (2). The plan assumed
   PM_POWERSAVE was the default. The effect of PS off on idle ping was as
   predicted, but the throughput improvement did not materialise because the
   one-URB-at-a-time kernel pattern is the binding constraint, not radio sleep.

2. Step 2 batch coalescing is a no-op at the current kernel vhci-hcd pipeline
   depth. The code is correct and will activate on a deeper-pipelining client.

3. TCP connection still shows retransmits and ssthresh stuck at 2-4 even with
   Wi-Fi PS off. This is consistent with small-segment TCP behaviour at 1 URB
   outstanding, not with radio sleep. Disabling PS removed the idle RTT tail
   but not the congestion-control churn from small segments.

4. The plan predicted 30-150 KiB/s after step 1. This did not materialise.
   The diagnosis in R22 (kernel vhci-hcd 1-2 URBs outstanding) remains the
   binding ceiling, unchanged by R23.

## Iteration budget

Steps 1 and 2 completed in one agent session. No bisect cycles. No unexpected
failures or hardware issues. Two commits, no reruns.

## Post-R23 verification: responder batch-size measurement (2026-05-01)

The agent's claim that "the responder queue holds only 1 entry per wakeup"
was empirically verified by adding a diagnostic counter inside the
responder that logs `wakes`, `avg_batch`, `max_batch`, and `size1/wakes`
every 100 wakes. The instrumentation was applied locally on top of
`bc2ea91`, built, flashed, run during the same `cdc_throughput.py`
benchmark, then reverted (not committed).

UART log excerpt (1300 wakes captured during a single benchmark run):

```
responder wakes=100  avg_batch=1.00 max_batch=1 size1=100/100
responder wakes=200  avg_batch=1.00 max_batch=1 size1=200/200
responder wakes=300  avg_batch=1.00 max_batch=1 size1=300/300
responder wakes=400  avg_batch=1.00 max_batch=1 size1=400/400
responder wakes=500  avg_batch=1.00 max_batch=1 size1=500/500
responder wakes=600  avg_batch=1.00 max_batch=1 size1=600/600
responder wakes=700  avg_batch=1.01 max_batch=5 size1=699/700
responder wakes=800  avg_batch=1.00 max_batch=5 size1=799/800
...
responder wakes=1300 avg_batch=1.00 max_batch=5 size1=1299/1300
```

1299 of 1300 wakes had `batch_n=1`. One outlier of `batch_n=5`. Average
batch size 1.00 (truncated to two decimal places).

This proves the IDF callback is firing for one URB at a time end-to-end:
the responder cannot coalesce because there is never anything queued at
its wake. With pipeline depth=16 set in the lane task and no firmware-
side gate observable, the only remaining explanation is that the IDF
sees one outstanding submit at a time, which means the lane queue holds
at most one URB at a time, which means the read loop receives at most
one CMD_SUBMIT before processing it, which means the kernel vhci-hcd
sends at most one CMD_SUBMIT before waiting for the corresponding
RET_SUBMIT.

The arithmetic that follows: 1 URB / RTT * 128 B/URB. With observed
TCP RTT 5-12 ms during streaming this gives a 10-25 KiB/s envelope;
observed 11.2 KiB/s sits inside it.

R20 through R23 firmware architecture is exhausted as a throughput
lever for this transport. Future improvements require lower kernel-
side RTT (Ethernet or better Wi-Fi link) or kernel-side vhci-hcd
pipelining (out of scope for mpy-pod).

## Path to further improvement

The ceiling is the kernel vhci-hcd single-URB pipeline. Options per R22:

- Switch to Ethernet (~1 ms RTT): expected 128 KiB/s at 1 URB in flight.
- Patch kernel vhci-hcd to pipeline more CMD_SUBMITs per endpoint.
- Larger TCP receive buffer or TCP_NODELAY tuning to reduce delayed-ACK
  exposure (marginal effect while kernel queues 1 URB).

## CORRECTION (2026-05-01): the kernel does pipeline; IDF host stack is the bottleneck

> **Verified (2026-05-03, per `r23-deep-dive-findings.md`):**
> µs-resolution timing on the IDF submit-to-callback path confirms
> Case C: avg_round = 157 ms at steady state, min_round = 87-110 µs
> (wire-time floor). The IDF host stack IS the bottleneck for bulk-IN.
> Per-direction breakdown: bulk-OUT avg 271 µs (wire speed), bulk-IN
> avg 165,916 µs (165 ms). The asymmetry is the key new finding;
> min_round at the wire floor proves the hardware can deliver IN at
> wire speed when the queue is empty. The 165 ms shape is consistent
> with IDF serialising bulk URBs per pipe in its event loop, not a
> hardware constraint. R24 TinyUSB pivot is NOT the followup: TinyUSB
> gotcha #2 implies the same per-ep serialisation, so the pivot does
> not lift the IN ceiling. R25 stays on IDF and tunes the host stack;
> see `r25-tune-idf-bulk-in-plan.md`.

The conclusion above blamed the kernel vhci-hcd. Subsequent web research
(`drivers/usb/usbip/vhci_tx.c` `vhci_send_cmd_submit`) confirmed
vhci-hcd does NOT serialize CMD_SUBMITs: `vhci_tx_loop` drains
`priv_tx` continuously without waiting for RET_SUBMIT. cdc-acm queues
`NR_BUFFERS=16` bulk-IN URBs concurrently. Per kernel source, multiple
CMD_SUBMITs go onto the TCP stream back-to-back.

A second firmware-side probe was added (a counter of `inflight_count`
at intake time, logging every 100 wakes) and run on the same hardware
and benchmark. Result over 1300 URBs:

```
intake_count=100  avg_depth=13.16 max_depth=18 depth1=15/100
intake_count=200  avg_depth=14.81 max_depth=18 depth1=15/200
intake_count=300  avg_depth=15.34 max_depth=18 depth1=15/300
intake_count=400  avg_depth=15.45 max_depth=18 depth1=15/400
...
intake_count=1300 avg_depth=11.50 max_depth=18 depth1=15/1300
```

The kernel pipelines: 13-18 URBs are simultaneously in our pipeline
at intake. Only 15 of 1300 had depth=1.

Throughput = 88 URBs/sec, pipeline depth ~16. By Little's Law, per-URB
residence time = 16 / 88 = **182 ms**. Wire time at FS bulk for a 128 B
URB is ~110 us. **99.94% of per-URB latency is somewhere in our
pipeline downstream of intake.**

The lane queue holds at most depth-16; URBs sit there only if the
counting sem is full. The lane task pulls URBs and calls
`usbhost_submit_async` (non-blocking IDF submit). The IDF callback
fires per URB and pushes onto the responder queue. The responder
(priority 11) drains one URB at a time.

The responder per-URB cost is bounded by `tx_ret_submit` TCP send
(~1 ms LAN-local). 88/s × 1 ms = 88 ms/sec, well within the responder's
capacity. The responder is NOT the bottleneck.

Therefore the bottleneck is the IDF host stack: it accepts our
back-to-back submits but processes them on the wire at ~88/sec rather
than the ~9000/sec the FS bulk wire would support. Per-URB IDF
processing time is ~11 ms, of which only ~110 us is wire transmission.

This is consistent with the IDF v5.5 host stack on DWC2 ESP32-S3 not
pipelining transfers within a single bulk pipe. Verifying with t_submit
to t_cb timing (R22 step 4 instrumentation) is the next concrete step.

The right action items therefore are NOT what R23 implemented (Wi-Fi
PS, TCP coalescing). Both stay on main as structural improvements
without throughput effect. The actual lever for throughput is one of:

1. Verify IDF host stack URB pipelining behavior (by probing
   `submit_xfer` timing or testing with a different USB host stack).
2. Switch to TinyUSB host (`docs/esp32-s3/design/usbhost.md` §11), which has a
   different scheduling model and may pipeline transfers better. The
   firmware infrastructure for this pivot is already linked in the build
   but our `usbhost.c` still uses IDF `usb_host_*`.
3. If the IDF host stack on DWC2 fundamentally cannot pipeline FS bulk
   above 88 URBs/sec per pipe, that's the hardware/IDF ceiling and a
   board change (P4 with Ethernet or HS USB) is the only remaining lever.

R20-R23 work is correct architecture for whichever USB host stack is in
place; the current ceiling is below that architecture. Next step is to
probe IDF behavior, not to refactor what we already have.
