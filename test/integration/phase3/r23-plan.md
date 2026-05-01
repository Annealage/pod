# R23 plan: Wi-Fi PS off + RET_SUBMIT coalescing

R22 eliminated the firmware-side wakeup floor. Streaming throughput
remained at ~11 KiB/s; the new ceiling is on the TCP/Wi-Fi side. Live
`ss -tn -i` capture during streaming on `dbff363` (post-R22) showed:

```
rtt:12-18 ms  minrtt:3.03 ms
cwnd:7  ssthresh:4
bytes_retrans:2928-3888  retrans:0/8 -> 1/10  lost:1  reordering:1
rcvmss:176  ato:40-43 ms
```

Two diagnoses fall out:

1. The Wi-Fi link is dropping packets early in the connection (ssthresh
   pinned at 4) and never recovering. Idle ping RTT 57-94 ms with mdev
   ~12 ms is the Wi-Fi power-save signature: radio sleeps at DTIM
   intervals, first packets after wake see latency spikes that look
   like loss to TCP. Disabling Wi-Fi PS removes this.

2. Each completed URB is one small (~176 B) TCP segment. With cwnd
   pinned at 4-7 and `ato=40 ms` delayed-ACK timeout, the connection
   spends most of its time waiting for ACKs of small segments. With
   R22's pipeline=16, there are typically multiple completions queued
   in the responder simultaneously; coalescing them into one TCP write
   reduces segment count proportionally, lowers delayed-ACK exposure,
   and reduces loss surface.

R23 attacks both in one branch as two committed steps so each can be
measured separately.

## Step 1: disable Wi-Fi power save

The default `esp_wifi_set_ps(WIFI_PS_MIN_MODEM)` puts the radio to
sleep between DTIM beacons (typically 100 ms). Calling
`esp_wifi_set_ps(WIFI_PS_NONE)` keeps the radio active. Cost is mostly
idle current (the annealage_pod is mains-powered so this is irrelevant).

Where: `src/mpy/annealage_pod/boot.py` `wifi_connect` after a successful
connection. The call is exposed in MicroPython as
`network.WLAN(network.STA_IF).config(pm=0xa11140)` (the IDF flag
mapping in MicroPython esp32 port). Verify the right way to express
this against the running esp32 port; the simplest robust shape is:

```python
sta.config(pm=network.WLAN.PM_NONE)
```

if `PM_NONE` is exported, else fall back to the integer constant. The
MicroPython esp32 port already supports this knob.

Validation: `ping -c 20 -i 0.2 192.168.0.166` should drop from
~76 ms avg to <5 ms avg. Re-run cdc_throughput.py and capture
`ss -tn -i` during streaming. Expect `cwnd` to grow past 7 and
`bytes_retrans` to stay near 0.

Expected throughput at bufsize=256 after step 1: at least 30 KiB/s, up
to ~150 KiB/s. The exact number depends on whether the connection's
delayed-ACK behaviour is also a factor.

## Step 2: coalesce RET_SUBMITs in the responder

R22's responder receives one completed URB at a time from
`responder_queue`, calls `tx_ret_submit` (which does one `lwip_writev`
under `tx_mutex`), then loops. With pipeline=16, the queue typically
holds multiple completed URBs simultaneously when the responder
wakes, but they're drained one-at-a-time.

Change shape: in the responder loop, after `xQueueReceive(...)`
returns one URB, drain `xQueueReceive(...,0)` non-blocking until
empty, building a batch (max N=16 entries). Per-URB: take
`inflight_mutex`, claim tx_owner, check the URB still wants
RET_SUBMIT (UNLINK may have claimed it). For URBs that should send
RET_SUBMIT, append (header, payload) iovec entries to a batched
`lwip_writev` call. Emit one writev under `tx_mutex` for the whole
batch. Per-URB retire / slot-release as today.

Constraints to maintain:

- **Per-(ep,dir) RET_SUBMIT order**: the responder is per-connection,
  draining a single queue. The IDF preserves submit order on each
  pipe; completions arrive in submit order; the responder appends
  to the batch in arrival order, then writev emits in array order.
  Per-pipe RET_SUBMIT order is preserved by construction.
- **Cross-pipe order does not matter** to the kernel (vhci keys URBs
  by seqnum, not arrival).
- **tx_mutex still serialises the actual TCP write** so a parallel
  RET_UNLINK from the read loop (UNLINK handler calls `tx_ret_unlink`
  under tx_mutex) interleaves cleanly.
- **Allocation budget**: an iovec for header + payload per URB. With
  N=16 URBs, that's 32 iovec entries. lwIP's `lwip_writev` is sized
  for IOV_MAX which is generally fine. Stack-allocate the iovec
  array (`struct iovec iov[32]`) and the header copies (16 ×
  `usbip_header_t` = 16 × 48 = 768 B); keep the payload pointers
  pointing at the existing `u->in_buf` buffers. No heap.

Expected throughput at bufsize=256 after step 2: at least 50 KiB/s,
ideally 100-300 KiB/s. The improvement vs step 1 alone depends on
whether step 1 already let cwnd grow past where small-segment count
mattered.

## Files touched

- `src/mpy/annealage_pod/boot.py` (~5 lines: PM_NONE call after connect)
- `src/c_modules/usbip/usbip_server.c` (~80 lines: responder loop +
  new `tx_ret_submit_batch` helper that takes an array)

No board files, no manifest, no sdkconfig.

## Implementation order

1. **Step 1 commit**: Wi-Fi PS off in `boot.py`. Build, flash, verify
   with ping + ss, run cdc_throughput.py, record numbers.
2. **Step 2 commit**: RET_SUBMIT coalescing in responder. Build,
   flash, full validation matrix, run cdc_throughput.py again.

Each step is its own commit so the marginal contribution of each is
visible.

## Validation

R20 regression matrix at end of step 2:

| Check | What |
|-------|------|
| A | smoke 5/0 |
| B | mpremote 30/30 back-to-back, 0.5 s gap |
| C | usbip detach probe (busids=2 at t+1s) |
| D | concurrent CDC + CMSIS-DAP |
| E | stress 60/60 |
| F | cdc_throughput.py + ss capture during streaming |

Plus per-step-1 specific:

- `ping -c 20 -i 0.2 192.168.0.166` average < 5 ms (currently 76 ms)
- `ss -tn -i` during streaming: `bytes_retrans` near 0,
  `ssthresh` not pinned at 4

Plus per-step-2 specific:

- raw-REPL handshake responsiveness: `time mpremote ... resume exec
  'pass'` should not regress vs post-R22. Coalescing within one
  responder wake should not add visible latency since the responder
  drains immediately rather than waiting; if it takes more than 2-3
  ms longer than post-R22, the batching is too aggressive (e.g.
  artificial delay added).

## Caveats and risks

1. **Wi-Fi PS off increases current draw**, marginally. The annealage_pod
   is mains-powered (no battery), so this is only relevant for
   thermal headroom. Should be sub-100 mA increase.

2. **Step 1 alone may already saturate**, making step 2 invisible in
   measurement. That's a happy outcome; commit step 2 anyway because
   it removes per-URB TCP segment overhead structurally and avoids
   drift back to congestion-bound throughput on a worse Wi-Fi link.

3. **Step 2 batching changes RET_SUBMIT timing characteristics.**
   Today each completion fires its own TCP segment; tomorrow they
   batch. cdc-acm should be indifferent (it processes RET_SUBMITs
   in arrival order regardless of segmentation), but raw-REPL ACK
   pacing might shift slightly. Validate B and the latency probe
   carefully.

4. **The PM_NONE constant** may not be exported by the running
   MicroPython esp32 port; some versions only accept the raw int.
   The implementing agent should `mpremote ... resume exec
   'help(network.WLAN.config)'` or similar to confirm before relying
   on the symbol. If only raw int is accepted, use `0` (which is
   PM_NONE in the IDF).

5. **The iovec count of 32** (header + payload per URB times 16)
   assumes lwIP's IOV_MAX permits this. Default ESP-IDF lwIP has
   `LWIP_TCP_MAX_IOV` somewhere around 8 by default; if so, fall
   back to a chunked writev (write 8 iovec entries per syscall,
   loop). Implementation should handle both, with a build-time
   constant.

## Out of scope

- Larger TCP buffers / window scaling (effect dwarfed by step 1+2).
- Separate TX coalesce task (step 2's "drain queue, batch, write" is
  a single-task change, not a new task).
- Switching to Ethernet (hardware pivot).
- Tuning IDF lwIP `TCP_MSS`, `TCP_WND`, etc. (defaults are fine
  once cwnd can grow past 4).

## Findings file expectations

`test/integration/phase3/r23-findings.md` should match R20's shape:

- per-step throughput pre vs post (cdc_throughput.py table)
- per-step ping RTT and ss snapshot (step 1 specifically)
- regression matrix per step
- caveats observed
- iteration budget used (5 cycles available)
