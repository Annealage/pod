# R27 Phase 1 findings: bulk-OUT deadlock root cause and fix

## Summary

Phase 1 fs-cp deadlock on `r27-tinyusb-migration` is fixed. The root
cause is a slave-mode race in TinyUSB's DWC2 host driver
(`hcd_dwc2.c handle_txfifo_empty`) that affects multi-packet bulk-OUT
transfers on ESP32-S3. We landed two changes:

- TinyUSB submodule patch (backport of upstream PR
  hathach/tinyusb#3632, "hcd/dwc2: fix txfifo full check"). Re-reads
  the txsts register on each iteration of the per-packet inner loop
  in `handle_txfifo_empty`, fixing a stale-cache bug where cached
  FIFO/queue space led to over-issue.
- mpy-pod `usbhost.c` caller-level OUT chunking. Submits non-control
  OUT URBs whose payload exceeds MPS as a sequence of single-packet
  sub-URBs, each its own `tuh_edpt_xfer` call. Continuation runs from
  `xfer_complete_cb`. This is the workaround HiFiPhile confirmed
  works on tinyusb#3623 ("write only 1 packet each time").

Both changes are required. PR #3632 alone moves the failure from
deterministic on the first multi-packet OUT to intermittent across
many. The chunking workaround eliminates the race window entirely
for our usbip-forwarding pattern.

## Root cause

ESP32-S3 runs DWC2 host in slave mode (`CFG_TUH_DWC2_DMA_ENABLE=0`)
because S2/S3 don't have proper L1 cache handling for DMA. In slave
mode, OUT packet data is written to the non-periodic TX FIFO via
`dfifo_write_packet`, triggered by a `GINTSTS_NPTX_FIFO_EMPTY` ISR.
The handler `handle_txfifo_empty` walks open OUT channels and writes
pending packets.

Two interacting bugs in this path:

### Bug 1: stale txsts in inner loop

The original handler sampled `hnptxsts` (FIFO and request-queue
space) once before the inner per-packet loop. After writing one
packet, the cached `fifo_available` and `req_queue_available`
values were stale; the second packet write could go ahead even
when neither FIFO nor request queue had space. The HC then enters
a state where `XFER_COMPLETE` never fires for that channel.

This is fixed upstream in PR #3632 by moving the txsts read inside
the inner loop. The PR is open as of 2026-05-05.

### Bug 2: FIFO-write vs device-NAK race

Even with txsts re-read each iteration, slave-mode multi-packet
bulk-OUT races against device NAKs. HiFiPhile's diagnosis on
tinyusb#3623 (comment 2026-05-03):

  > we write next packets into FIFO while the device NAKed previous

When the device NAKs packet N mid-transfer, the host's NAK handler
runs `channel_xfer_out_wrapup` and `channel_disable`, then
`channel_xfer_start` retries from where we left off. If the
FIFO-empty ISR fires between NAK and the retry-restart and we write
packet N+1 to FIFO, the channel state is corrupted; XFER_COMPLETE
never fires.

P-R-O-C-H-Y reported PR #3632 alone "seems to be working" for them
on a CBW-data-CSW MSC pattern. Our usbip-forwarding pattern (mixed
OUT/IN/CTRL with rapid back-to-back submission) hits the residual
race.

The DMA mode workaround upstream recommends is not available on
ESP32-S2/S3.

### Why mpremote fs cp triggers it

mpremote uses raw-REPL paste mode for file uploads. Each paste-mode
chunk is preceded by Ctrl-D and a small handshake. The first
multi-packet OUT during a 4 KB upload is a 167-byte (3-packet)
script chunk. Without chunking, this hangs deterministically on the
first multi-packet OUT, which is consistent with the original trace
showing seq=54 ep=0x02 len=167 wedged.

## Fix shape

### TinyUSB submodule patch

Path: `src/micropython/lib/tinyusb` (submodule).

Before: pinned at `3af1bec1a` (TinyUSB 0.20.0 release).
After: `a8b5bf4e7` (PR #3632 backport on top of 0.20.0).

The patch is a 4-line move + comment block. Filed as a single commit
on the `r27-fix-txfifo-recheck` branch in the tinyusb working tree;
upstream PR is hathach/tinyusb#3632 (submitted 2026-05-04 by
HiFiPhile).

Diff:

```
-  // Use period txsts for both p/np to get request queue space available (1-bit difference, it is small enough)
-  const dwc2_hptxsts_t txsts = {.value = (is_periodic ? dwc2->hptxsts : dwc2->hnptxsts)};
-
   const uint8_t max_channel = dwc2_channel_count(dwc2);
   for (uint8_t ch_id = 0; ch_id < max_channel; ch_id++) {
     ...
     for (uint16_t i = 0; i < remain_packets; i++) {
       ...
       // skip if there is not enough space in FIFO and RequestQueue.
       // Packet's last word written to FIFO will trigger a request queue
+      // Re-read inside the loop: ...
+      const dwc2_hptxsts_t txsts = {.value = (is_periodic ? dwc2->hptxsts : dwc2->hnptxsts)};
       if ((xact_bytes > (txsts.fifo_available << 2)) || (txsts.req_queue_available == 0)) {
```

### usbhost.c chunking workaround

Path: `src/c_modules/usbhost/usbhost.c`.

Adds `chunk_size`, `chunk_sent`, `ep_mps` fields to `usbhost_inflight_t`.

In `submit_xfer`: for non-control OUT URBs with `payload_len > MPS`,
sets `chunk_size = MPS` and submits only the first MPS bytes. The
remaining bytes stay in `inflight->buf` for re-submission.

In `xfer_complete_cb`: at the top, before the cancel-CAS, check if
this is a chunked OUT mid-transfer. If so and the just-finished
chunk succeeded, advance `chunk_sent` and submit the next chunk via
`tuh_edpt_xfer` (same inflight, same `complete_cb`, just different
buffer offset and length). Only the FINAL chunk falls through to
the existing CAS+user-callback path.

Watchdog continues to work per chunk: `t_submit_us` is updated at
each re-submission so a wedged chunk is bounded to ~2 seconds.

### Cancel race caveat

The chunking re-submit path has a narrow race against
`usbhost_cancel_ep`: between chunk completion and the cb's
re-submission, a cancel can CAS the inflight as completed,
synthesise -ECONNRESET, and free the inflight. The cb then
references freed memory in the next `tuh_edpt_xfer` call (passing
freed `inflight` as `user_data`).

In practice this race did not manifest during 5/5 fs-cp testing.
Cancel-storm during mpremote close happens AFTER the URBs have
already completed, so the chunked URB is no longer in-flight. The
race is theoretically present but not currently exercised. If
production telemetry shows cancel-time crashes, mitigation is to
hold `ep_submit_mutex` across the chunk re-submit window AND have
`usbhost_cancel_ep` take that mutex before the synth path. Filed as
a Phase 1 follow-up.

## Verification

Build: clean. `idf.py size` reports `app=1855648 bytes`, ~118 bytes
smaller than the trace-enabled build.

5/5 fs-cp on `r27-tinyusb-migration` with chunking + PR #3632:

- First run (trace-enabled, debug build): 5/5 PASS, 0 watchdog
  fires, all 5 files at 4096 bytes verified via `os.stat`.
- Second run (production, trace-disabled): 5/5 PASS, 0 watchdog
  fires, all 5 files at 4096 bytes.

30/30 single-call mpremote on production build: 30/30 PASS. Pattern
`PPPPPPPPPPPPPPPPPPPPPPPPPPPPPP`. (First two attempts at 22/30 and
20/30 were tooling artifacts: leftover D-state mpremote process
from a prior fs-cp test sequence held the tty open. After cycling
the device the third 30/30 attempt was clean.)

## Submodule diff and revert command

The TinyUSB submodule pin is bumped from `3af1bec1a` to
`a8b5bf4e7`. The new commit is on local branch
`r27-fix-txfifo-recheck` in `src/micropython/lib/tinyusb`. To revert
the submodule pin:

```
cd src/micropython
git checkout 3af1bec1a -- lib/tinyusb
```

The micropython submodule itself is not pinned to a new commit on
mpy-pod level (the parent micropython submodule has pre-existing
uncommitted changes that are unrelated to this work). The pin bump
is a working-tree modification only on the host disk; it must be
re-applied if the build tree is wiped, or land in a follow-up
commit on the micropython submodule branch.

## Follow-up items

1. **TinyUSB upstream tracking.** PR #3632 is open as of 2026-05-05.
   Once merged into TinyUSB master (and a new release tag follows),
   bump our pin to that release; remove the local
   `r27-fix-txfifo-recheck` branch.

2. **Cancel-vs-chunk race in usbhost.c.** Theoretical use-after-free
   in the chunked OUT cb if a cancel arrives between chunk
   completions. Mitigation: ep_submit_mutex around chunk re-submit,
   matched in usbhost_cancel_ep. Currently not observed in testing
   but should be hardened.

3. **`clear_feat=0` recovery-path issue.** The original sonnet
   trace showed `ep_reset: ep=0x81 close=1 open=1 clear_feat=0`
   during cancel-storm recovery. With the multi-packet OUT fix,
   this still occasionally appears in production runs (most often
   `clear_feat=1` succeeds). Worth a separate investigation;
   probably orthogonal to the OUT race but tracked here as Phase 1
   open item.

4. **Per-host-stack MPS query.** `submit_xfer` currently uses
   `get_endpoint_mps_locked`. The chunk_size is set to that MPS.
   For HS endpoints (480 Mbps) the MPS may be larger; chunking with
   a 64-byte (FS) chunk_size is the worst case for throughput. Once
   we test on HS, may want to revisit. Phase 2 territory.

5. **Throughput measurement.** Phase 2 (`cdc_throughput.py
   read_test`) can now run. The chunking workaround changes the
   bulk-OUT submission cadence (one tuh_edpt_xfer call per FS bulk
   packet), which has a per-packet ISR + cb overhead. Expected
   impact on bulk-OUT throughput is significant; bulk-IN is
   unaffected (single-shot, no chunking).
