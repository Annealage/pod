# R25 plan: fix the R24 fs-cp kernel D-state deadlock

## Context

R23 deep-dive confirmed Case C: IDF host stack bulk-IN avg_round = 157 ms
(~1400x the 110 µs wire floor). The R24 TinyUSB pivot was the correct
direction in principle. However, R24 is parked because `mpremote fs cp`
(a multi-step CDC session) hangs the Linux mpremote process in kernel
D-state on `usb_poison_urb`, requiring a host reboot to clear.

Before retrying R24 or making any further architectural change, the
D-state deadlock must be fixed. A USB/IP server that can permanently
lock the kernel is a hard blocker regardless of throughput.

See `r24-wip-history.md` for the full context, including the seven
TinyUSB-on-DWC2 gotchas already discovered and confirmed.

## New consideration before resuming TinyUSB

R23 deep-dive also found that bulk-OUT is fast (271 µs avg) but
bulk-IN is slow (165 ms avg) under IDF. TinyUSB gotcha #2 states that
`tuh_edpt_xfer` allows only one transfer in flight per (dev, ep).
This is the same constraint as IDF. If TinyUSB also serialises bulk-IN
tokens, the pivot will not improve throughput.

**Verify this before investing in the TinyUSB pivot:**

Option: on the r24-wip branch, measure the same idf_timing equivalent
using `tuh_edpt_xfer` with a timestamp before and after submit, and
compare avg_round for bulk-IN. If TinyUSB also shows ~165 ms avg-round
for bulk-IN, throughput improvement requires a different approach
(multiple concurrent EP opens, or option B class-driver forwarding).

This verification is a one build+run cycle and should be done before
investing further in the D-state fix, because if TinyUSB is equally
slow on bulk-IN, fixing the deadlock is still necessary (for safety)
but there is no throughput benefit to the pivot.

## Primary task: fix the D-state deadlock

### Symptom

During `mpremote fs cp` (multi-step raw-REPL paste-mode session), the
kernel mpremote process enters D-state (uninterruptible sleep) on
`usb_poison_urb`. Power-cycling the S3 does not release it. Host reboot
required.

Root cause per r24-wip-history.md "what's left to debug" section:
65 URBs flow through naturally, then one stops completing. The firmware
appears healthy (no panic, TCP still up). Three candidate hypotheses:

- **Hyp A**: A bulk-OUT URB hangs - kernel submits bulk-OUT, firmware
  accepts, but never sends RET_SUBMIT.
- **Hyp B**: Bulk-IN data corruption mid-stream. cdc-acm receives data
  but discards because it does not match expected raw-REPL framing.
- **Hyp C**: A control transfer (raw-REPL paste-mode includes Ctrl-D
  bytes inline) races with bulk transfers on the same connection.

### Step 1: confirm which URB stops completing

Instrument r24-wip `usbhost.c` to log every URB submission and every
callback, keyed by seqnum and EP direction. Run `mpremote fs cp` and
capture UART. Find URB #66 (the one that never returns). Note its EP,
direction, and seqnum.

Expected output to add:

```c
ESP_LOGI(TAG, "submit: seqnum=%u ep=0x%02x dir=%s len=%u",
         req->seqnum, ep_addr, is_in ? "IN" : "OUT", length);
```

```c
ESP_LOGI(TAG, "cb: seqnum=%u ep=0x%02x status=%d actual=%u",
         inflight->seqnum, ep_addr, status, actual_num_bytes);
```

If seqnum N is submitted but never appears in cb, N is the stuck URB.

### Step 2: based on stuck URB identity

If OUT URB is stuck (Hyp A):
- Add a watchdog timer in the lane task: any submit with no callback
  in 2 seconds calls `usb_host_endpoint_halt` + flush + clear on the
  EP, then synthesises a -ETIMEDOUT completion to unblock the kernel.
- Verify the watchdog fires, the kernel D-state resolves, and
  subsequent URBs flow after recovery.

If IN corruption (Hyp B):
- This is a kernel-side issue (cdc-acm discarding valid data). Check
  if the data toggle is misaligned. The ep_reset (close+open) sequence
  resets host toggle but not device toggle (gotcha #6). Run a
  CLEAR_FEATURE(ENDPOINT_HALT) after the stuck URB is identified to
  re-sync device toggle. If this unblocks flow, the fix is a proactive
  CLEAR_FEATURE at session start or after each URB cancel.

If control transfer race (Hyp C):
- EP0 control transfers share the IDF EP0 context with bulk EPs.
  Serialise EP0 and bulk submits with a device-level mutex (already
  partially done in r24 for cancel vs submit). Extend to cover control
  vs bulk overlap.

### Step 3: verify fix does not regress single-call mpremote

Run `mpremote 30/30` (single-call) after fix. The cancel-storm handling
fixed in r24 must still work. Then run `mpremote fs cp` to confirm D-state
is gone.

### Step 4: run cdc_throughput.py read_test

If the fix also resolves the throughput bench hang that r24-wip-history
documents, capture timing data comparable to r23-deep-dive-findings.md
to see if TinyUSB changed the bulk-IN latency.

## Suggested first instrumentation

Add these two ESP_LOGI calls to `usbhost.c` on r24-wip, around the
submit path and callback path, keyed by a unique seqnum from the
usbip request header:

```c
/* in usbhost_submit_async, after usb_host_transfer_submit: */
ESP_LOGI(TAG, "xmit ep=0x%02x dir=%s seq=%u len=%u",
         ep_addr, is_in?"IN":"OUT", seqnum, payload_len);

/* in transfer_done_cb, before user_cb call: */
ESP_LOGI(TAG, "done ep=0x%02x dir=%s seq=%u status=%d actual=%u",
         ep_addr, inflight->is_in?"IN":"OUT",
         inflight->seqnum, status, actual_num_bytes);
```

The seqnum field is already in the usbip_request_t (R20 step 2).

## Hardware

- ESP32-S3 on `mpy-dev esp32-s3`.
- Pico CDC on usbip from 192.168.0.166.
- Host must be rebooted before starting (vhci_hcd D-state from any
  prior failed fs cp may linger).
- Build on r24-wip branch: `git checkout r24-wip && bash src/tools/build.sh`.

## Exit criteria

1. `mpremote fs cp <some_file> :` succeeds without D-state.
2. `mpremote 30/30` still passes (no regression on cancel-storm path).
3. UART shows all submitted URBs receive a callback (no missing seqnum).
4. Host kernel shows no vhci_hcd processes in D-state after test.

## Out of scope

- TinyUSB bulk-IN throughput improvement (throughput is not the
  blocker; safety is).
- Option B class-driver forwarding. R25 targets the minimal fix to
  make the existing option A approach safe.
- Any architectural changes to the IDF-based main branch. R24-wip is
  the active target.
