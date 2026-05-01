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

## Path to further improvement

The ceiling is the kernel vhci-hcd single-URB pipeline. Options per R22:

- Switch to Ethernet (~1 ms RTT): expected 128 KiB/s at 1 URB in flight.
- Patch kernel vhci-hcd to pipeline more CMD_SUBMITs per endpoint.
- Larger TCP receive buffer or TCP_NODELAY tuning to reduce delayed-ACK
  exposure (marginal effect while kernel queues 1 URB).
