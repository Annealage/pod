# R27 plan: resume the TinyUSB host pivot, finish the migration off IDF

## Context

R23 deep-dive (`r23-deep-dive-findings.md`) measured IDF bulk-IN
`avg_round=165 ms` vs OUT `271 us` on this hardware. R25 stage B
(`r25-isr-instrumentation.md`, corrected) narrowed the gap to
~11 ms wall-clock between consecutive bulk-IN ISR fires on the
DWC2 channel; ISR processing is ~14 us so the time is between URB
N completing on the wire and DWC2 raising URB N+1's XFERCOMPL
interrupt. The cause of that 11 ms gap is not established. The
R25 direct-USB baseline (`r25-direct-usb-baseline.md`) measured
677 KiB/s through the same Pico-class device on the same Linux
host on the same FS bus, so the gap is a software or DWC2-config
difference somewhere, not a silicon limit.

The user's call: pivot the throughput investigation onto TinyUSB
host. Reasoning is twofold. First, the 11 ms gap looks more like a
host-stack scheduling/policy issue than silicon, so a different
host stack on the same DWC2 IP block is the cleanest way to
disambiguate. Second, the user has prior contribution history with
TinyUSB and access to commit upstream patches there, where IDF
patches stay vendored locally indefinitely (`r25-isr-instrumentation.md`
keeps a working-tree diff in `/home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf`
that has to be manually re-applied every IDF bump).

The host-stack swap IS the throughput investigation vehicle, not
just an alternative. Either:

- TinyUSB matches Linux EHCI throughput (~600+ KiB/s): IDF
  scheduling was the cause; the migration alone solves it.
- TinyUSB matches IDF (~11 KiB/s): the cause is shared between
  both stacks (DWC2 register configuration, scatter-gather DMA
  semantics, or similar shared layer); subsequent debugging
  happens in TinyUSB code where we have edit rights.
- TinyUSB lands somewhere in between: more nuanced, but the
  delta vs IDF localises which subsystem matters.

All three outcomes are informative.

A separate dependency: R26 (`r26-lwip-pbuf-corruption.md`) is a
latent lwIP heap-corruption bug that surfaces under sustained
bulk-IN streaming. It is in our `usbip_server.c read_exact` lwip
recv path and is independent of which USB host stack is in use.
TinyUSB throughput measurement that uses sustained streaming will
hit it. The plan addresses ordering below.

## Why R24 was parked vs why this resumes

R24's parked recommendation (`r24-bug-findings.md` "Recommendation"
section, written 2026-05-01) was "park r24-wip; the throughput
ceiling is kernel-side TCP RTT, not the USB host stack". That
recommendation was based on the pre-R23-deep-dive understanding
that TCP RTT capped throughput at ~11 KiB/s. R23 deep-dive then
measured µs-resolution IDF timing and refuted that: the cap is in
the IDF host stack itself (`r23-deep-dive-findings.md` corrected).
R25 narrowed it further. So the original "park" rationale no longer
holds; throughput parity with direct USB is now the binding goal,
and the host-stack swap is the lever.

The parked code on `r24-wip` (tip `79ee842`) is still a faithful
TinyUSB-host port of `usbhost.c`; resuming means picking up where
the fs-cp deadlock left off. Phase 1 below is exactly that.

## Phase 0 (foundation) result (2026-05-05)

Branch `r27-tinyusb-migration` created from main HEAD `cba329b`.
Three commits on the branch:

- `51597e7` R27 build config: enable TinyUSB host raw-URB API
  (mpconfigboard.cmake adds `CFG_TUH_API_EDPT_XFER=1`; cmake
  comment block updated; micropython.cmake header rewritten).
- `5730ec0` R27 usbhost.c: replace IDF host backend with TinyUSB
  host primitives (953 ins / 1042 del; the IDF backend is gone,
  the new file is r24-wip's tip with R27 header documenting the
  seven gotchas inline and the pump priority bumped 10 -> 20).
- `de7e118` R27 lane pipeline depth: drop from 16 to 1 for
  TinyUSB backend (one-line constant change in usbip_server.c
  per gotcha #2).

Build: clean. `idf.py size` reports 1852 KiB application binary
(232 KiB free in the 2 MiB factory partition). No new compile
warnings.

Boot: clean. `annealage_pod.boot` status flags all true (`wifi: True,
usbip: True, dapprobe: True, uartbridge: True, repl_listener:
True, dut_usb: True`). Wi-Fi up at 192.168.0.166. usbip server
exporting Pico CDC at busid 1-1 and synthetic CMSIS-DAP at busid
2-1. MicroPython REPL responsive over UART0.

Single-call mpremote test:
- Round 1: 29/30 pass. Pattern `PPPFPPPPPPPPPPPPPPPPPPPPPPPPPP`.
  The single failure (iteration 4) returned no output and no
  error message; an immediate retry succeeded. Likely transient
  (USB enumeration or scheduler glitch) - did not recur.
- Round 2: 30/30 pass. Pattern
  `PPPPPPPPPPPPPPPPPPPPPPPPPPPPPP`.

Combined 59/60 = 98% pass rate, comparable to r24-wip's 30/30
baseline at tip `79ee842`. Phase 0 exit criterion (single-call
mpremote 30/30) is met by round 2 alone.

### Deviations from r24-wip

- **Pump task priority**: 10 -> 20. Rationale documented in
  `usbhost.c` header. R25 worker-priority work concluded that 20
  is the right tier for a USB host pump task on core 1.
- **Header comment**: rewritten to inventory the seven gotchas
  inline, with cross-references to `r24-wip-history.md`. This
  reduces the load on readers landing on this file cold.
- **`USBIP_PIPELINE_DEPTH` constant**: dropped from 16 to 1 in
  `usbip_server.c`. r24-wip set it to 1 (per `r24-bug-plan.md`
  step 2 list); the change moves with the migration.
- **Managed-component vs submodule include-path workaround**:
  retained as-is (`local_get_device_desc`, `local_get_config_desc`
  wrappers in `usbhost.c`). The structural fix (evict
  `espressif__tinyusb` managed component from the build) is a
  project-wide refactor not in scope for this branch foundation;
  filed as a follow-up consideration.

### Surprises

- The transient mpremote failure in round 1 (1/30) did not show
  on round 2. Could be a clean enumeration race after fresh
  `usbip attach`; could be a r24-wip-era flakiness that simply
  didn't show in the cached baseline. Worth keeping an eye on
  during Phase 1 driving but not a blocker.
- The `traceISR_EXIT_TO_SCHEDULER` workaround in `usbhost.c`
  remains needed and was confirmed by the build behavior. The
  comment in `mpconfigboard.cmake` was updated to reflect the
  fact that the bug DOES surface on this branch (because the
  TinyUSB include path is now active in `usbhost.c`).

### Still on the to-do list before Phase 1 can begin

Nothing blocking. Phase 1 (fs-cp deadlock fix per
`r24-bug-findings.md` hypotheses A/B/C) can dispatch immediately
on this branch. The single-call test passes; the multi-step
test (`mpremote fs cp`) is the known failure mode that Phase 1
targets.

The branch is `r27-tinyusb-migration`, separate from main. Phase 1
work happens on this branch via further commits; merging to
main waits until Phase 4.

## Phase summary

| Phase | Goal | Depends on | Exit criterion |
|---|---|---|---|
| 0 | Migration foundation: TinyUSB backend on a fresh branch from current main | nothing | branch builds, boots clean, single-call mpremote 30/30 passes (parity with r24-wip's known-good baseline) |
| 1 | Stabilise the fs-cp deadlock on the migrated branch | Phase 0 | `mpremote fs cp` of a 4 KB file completes 5/5 times without kernel D-state |
| 2 | Measure TinyUSB throughput against the same Pico CDC device + R26 disambiguation | Phase 1 | `cdc_throughput.py read_test` runs to completion at all bufsizes; per-bufsize KiB/s recorded; never-NAK harness either crashes (R26 stack-agnostic) or runs clean for 30 s (R26 IDF-induced) |
| 3 | Pick R26 path based on Phase 2 step 2.5 outcome: parallel fix track if stack-agnostic, deferral to `r25-idf-backend` branch if IDF-induced | Phase 2 step 2.5 | R26 row in `plan/overview.md` either updated with parallel-track plan or removed |
| 4 | Migration finalisation: rebase `r24-wip` onto main with manual conflict resolution | Phases 1-3 | branch tracking complete; main is on TinyUSB; IDF backend on `r25-idf-backend` frozen branch |
| 5 | If TinyUSB hits the same 11 ms wall: continue throughput investigation in TinyUSB code | Phase 2 measurement (negative) | open; depends on what the TinyUSB-side instrumentation finds |

## Phase 1: stabilise r24-wip (the fs-cp deadlock)

**Status (2026-05-06): closed.** Root cause and fix landed.
TinyUSB DMA-mode IN handler in `hcd_dwc2.c` was missing the
post-transfer `edpt->next_pid = hctsiz.pid` save that the
slave-mode handler does. On a short-packet completion the
projected `next_pid` from `channel_xfer_start` was stale, causing
DATATOGGLE_ERR on the next IN URB and silently dropping the
device's first packet. Fix: one-line save in
`handle_channel_in_dma` mirroring the slave-mode behaviour.
Submodule commit `6b0f49b06` on `r27-fix-txfifo-recheck`. See
`r27-dma-fix-findings.md` "Result (2026-05-06)" section for the
full diagnosis, fix shape, and verification numbers. Upstream PR
draft text in `r27-upstream-pr-draft.md`.

### Phase 1 progress so far (2026-05-05)

Steps 1-3 of the dispatched Phase 1 procedure are complete (sonnet
pass, opus to take over for the fix). Branch state: clean on
`r27-tinyusb-migration` at `d628c5f` plus uncommitted changes to
mpconfigboard.cmake (R27_DEADLOCK_TRACE=1 enabled) and
r25-progress.log. The trace-enabled firmware is currently flashed.

### Step 1: host sanity = PASS

`vhci_hcd` loaded after host reboot, refcount 0 clean,
`usbip list -r 192.168.0.166` enumerates the Pico CDC at 1-1.

### Step 2: watchdog efficacy = PROVED, with refinement

First watchdog efficacy run (`5df3b7d`) showed the watchdog firing
**spuriously** on `ep=0x81` (cdc-acm interrupt-IN modem-status
notification, which legitimately waits forever for line state to
change). Cancelling it broke the cdc-acm session and triggered a
kernel-side cancel storm. **No D-state on host** in either run -
that's the watchdog's job and it worked - but the spurious fire
masked the real bulk-side bug.

**Refinement landed at `d628c5f`**:
- Added `ep_xfer_type` to `usbhost_inflight_t`, cached at submit
  via new `get_endpoint_xfer_type_locked` helper.
- Watchdog scan skips `TUSB_XFER_INTERRUPT` (3) and
  `TUSB_XFER_ISOCHRONOUS` (1).
- Fixed `%lld` printf (newlib-nano doesn't support it; converted
  to `(int32_t)` cast and `PRId32` per R23 deep-dive precedent).

Second run (refined watchdog) showed:
- mpremote returns with proper error message:
  `mpremote: Error with transport: timeout waiting for first EOF
  reception`
- **No D-state on host. vhci_hcd refcount stays 0.**
- Watchdog fires once on `ep=0x82` (bulk-IN) at age=2,053,556us.
- No spurious interrupt-IN fires.

**Watchdog efficacy verdict: PROVED.** The kernel-side
`usb_poison_urb` D-state that motivated the watchdog does not
happen any more. All subsequent iteration cycles are cheap (just
reflash + cycle, no host reboot needed).

UART log: `/tmp/r27-fscp-watchdog2-uart.log` (refined run).

### Step 3: trace capture = PASS

Enabled `R27_DEADLOCK_TRACE=1` in
`src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.cmake`. Rebuild
clean. Trace-enabled firmware reproduces the same deadlock.

UART log: **`/tmp/r27-fscp-trace-uart.log`** (8023 bytes;
preserved on disk for opus pickup).

Headline trace data — the failing URB region:

```
I (31630) usbhost: sub: seq=53 ep=0x82 dir=IN  len=128 ifl=0x3fcf05b8 dev=1
I (31637) usbhost: sub: seq=54 ep=0x02 dir=OUT len=167 ifl=0x3fcf1664 dev=1
I (31644) usbhost: cb:  seq=53 ep=0x82 result=0 alen=1   ifl=0x3fcf05b8
I (31649) usbhost: sub: seq=55 ep=0x82 dir=IN  len=128 ifl=0x3fcf05b8 dev=1
W (33720) usbhost: watchdog: synth seq=54 ep=0x02 dev=1 age=2077543us ifl=0x3fcf1664 (running recovery)
I (61003) usbhost: sub: seq=56 ep=0x00 dir=CTRL len=0   ifl=0x3fcf1808 dev=1
```

Key observations:

- **seq=54 is bulk-OUT, length 167 bytes**, submitted at
  uptime ~31.6s. No matching `cb: seq=54` ever appears.
- **All preceding bulk-OUT URBs (seq=21, 23, 27, 30, 31, 35, 38,
  39, 43, 46, 47, 51) had length ≤ 35 bytes**, and all completed
  cleanly (alen matched len, result=0, sub-millisecond turnaround).
- **seq=54 with len=167 is the first OUT to exceed 64 bytes**.
  64 bytes is the FS bulk MPS; 167 = 2×64 + 39 (3 packets:
  full+full+partial), requiring DATA0/DATA1/DATA0 toggle sequence.
- **seq=55 (bulk-IN immediately after the stuck OUT) is also
  stuck**. Its `cb:` never appears either; the IN URB is queued
  but the OUT before it must complete first.
- Watchdog fires at age=2.08s on seq=54 (the OUT). After
  recovery, no further bulk traffic flows. The 27-second gap
  (uptime 33.7s -> 61.0s) is the kernel-side mpremote waiting
  for the protocol-level response that will never come; eventually
  cdc-acm gives up and starts the cancel storm at uptime 61s.
- During the recovery cancel storm, `tuh_control_xfer rejected
  ep=0x00 addr=1` is logged. The CLEAR_FEATURE in the watchdog
  recovery path failed (`clear_feat=0`). Could be relevant: the
  device-side endpoint state is unrecoverable after the failed
  multi-packet OUT.

**Preliminary hypothesis: A (bulk-OUT URB hangs)**, specifically:
the first multi-packet OUT (>= 64 bytes) wedges TinyUSB's host
DWC2 transfer state. Smaller (single-packet) OUTs work fine.
This is consistent with `_buffer_fill_bulk` in IDF's hcd_dwc.c
filling a single QTD with the full transfer length and the
hardware splitting into MPS-sized packets — but if TinyUSB's
DWC2 HCD driver has a bug in the multi-packet OUT path (e.g.
the channel doesn't get re-armed for the second packet), the
URB stalls indefinitely.

**Flagged as preliminary; opus to confirm.**

### Hand-off context for opus

- Branch `r27-tinyusb-migration`, last committed SHA `d628c5f`.
- Uncommitted changes:
  - `src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.cmake` adds
    `R27_DEADLOCK_TRACE=1`.
  - `test/integration/phase3/r25-progress.log` has Phase 1 step
    records appended.
- Trace UART log: **`/tmp/r27-fscp-trace-uart.log`**.
- Watchdog UART logs (for reference):
  `/tmp/r27-fscp-watchdog-uart.log` (first run, spurious fires),
  `/tmp/r27-fscp-watchdog2-uart.log` (second run after refinement).
- Test file: `/tmp/r27-test4k.bin` (4 KB random).
- ESP32 firmware in flash: trace-enabled build (size 1855552
  bytes; vs 1855136 for trace-off).
- Pico tty when attached: `/dev/ttyACM13` (consistent across
  runs in this session).
- Reproducer:
  ```
  sudo usbip attach -r 192.168.0.166 -b 1-1
  timeout 30 mpremote connect /dev/ttyACM13 resume fs cp \
      /tmp/r27-test4k.bin :test4k.bin
  ```
- **Watchdog protects iteration cost**: each cycle is reflash +
  cycle + attach + run + detach. ~1-2 minutes per cycle. The host
  does NOT need to reboot.

### Recommended next steps for opus (from the trace)

1. Add an extra trace point in `xfer_complete_cb` to log the
   tuh_xfer_t result code in detail (we have result=0 for all
   completed; the failing case never enters cb so we don't
   directly observe what TinyUSB thinks). Or add a "submit
   accepted by tuh_edpt_xfer" log so we know whether
   `tuh_edpt_xfer` returned true for the 167-byte OUT.
2. Test smaller multi-packet OUTs (e.g. 65 bytes — exactly
   one MPS + 1 byte, the smallest multi-packet) to find the
   exact threshold. If 65 bytes also hangs, the issue is
   purely "any multi-packet OUT". If only larger sizes hang,
   the threshold itself is informative.
3. Inspect TinyUSB's `hcd_dwc2.c` for known issues with bulk-
   OUT multi-packet transfers. The IDF HCD that R23/R25 used
   filled a single QTD per URB with HOC; TinyUSB-on-DWC2 may
   use a different scheme (descriptor-list mode? per-packet
   QTDs?) that has a bug.
4. The watchdog and `tuh_edpt_abort_xfer` recovery may also
   have a bug visible here: `clear_feat=0` after the abort
   suggests the EP isn't recoverable cleanly. That might
   matter independently for any OTHER deadlock path; even if
   the multi-packet OUT issue is fixed, the recovery semantics
   are worth a look.

### Symptom recap

Per `r24-bug-findings.md` and `r24-wip-history.md`: `mpremote ...
resume fs cp <local> :remote` (or `cdc_throughput.py read_test`,
which uses the same multi-step raw-REPL paste pattern) hangs the
Linux mpremote process in kernel D-state on `usb_poison_urb`.
65 URBs deliver naturally, then one stops completing; firmware
appears healthy but no RET_SUBMIT and no RET_UNLINK ever follow.
Single-call mpremote (30/30) works because the close-storm cancel
path is fixed.

### Three hypotheses (from `r24-bug-findings.md`)

- **A**: a bulk-OUT URB hangs during the script-write loop.
  Kernel submits, firmware accepts, no RET_SUBMIT.
- **B**: bulk-IN data corruption. cdc-acm receives data, drops it
  for not matching expected raw-REPL framing.
- **C**: control-vs-bulk race. Raw-REPL paste-mode includes Ctrl-D
  bytes inline; a control xfer mid-stream desynchronises us.

### Step 1.1: per-URB submit/complete instrumentation

Add a verbose-gated trace block in `src/c_modules/usbhost/usbhost.c`
on `r24-wip`. Match the existing `s_urb_verbose` flag pattern
(global toggle, gated `ESP_LOGI`). Two anchor points:

```c
/* In usbhost_submit_async, just after tuh_edpt_xfer / tuh_control_xfer: */
if (s_urb_verbose) {
    ESP_LOGI(TAG, "submit seq=%" PRIu32 " ep=0x%02x dir=%s len=%u rc=%d",
             seqnum, ep_addr, is_in ? "IN" : "OUT", payload_len, rc);
}

/* In xfer_complete_cb, just after the result is known: */
if (s_urb_verbose) {
    ESP_LOGI(TAG, "done   seq=%" PRIu32 " ep=0x%02x dir=%s status=%d actual=%u",
             inflight->seqnum, ep_addr, is_in ? "IN" : "OUT",
             status, (unsigned)actual_num_bytes);
}
```

The `seqnum` field already lives in `usbip_decoded_header_t` (R20
step 2). Cost: 2 ESP_LOGI per URB while verbose enabled; off in
production.

Build flag for the verbose instrumentation: define
`USBHOST_R27_VERBOSE` in `src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.cmake`
behind a CMake option that defaults off. Setting `s_urb_verbose=true`
from boot.py during the deadlock repro flips it on at runtime.

### Step 1.2: faster D-state recovery

The R24 history says `vhci_hcd` module unload may also hang once a
poisoned URB is stuck. **Test in advance**: with no usbip device
attached, `sudo modprobe -r vhci_hcd` should unload cleanly. Find
out (separately from any deadlock test) whether `sudo modprobe -r
vhci_hcd && sudo modprobe vhci_hcd` clears a stuck mpremote
process. If it does, that's the recovery procedure; if it
doesn't, full host reboot is required between iterations and the
iteration cost is high.

If modprobe -r works:

```
# After deadlock detected:
sudo usbip detach -p 0 || true
sudo modprobe -r vhci_hcd
sudo modprobe vhci_hcd
mpy-dev cycle esp32-s3 ; sleep 18
# ESP32 firmware re-enumerates fresh; iterate.
```

If modprobe -r hangs, document it and accept the per-iteration
host reboot.

### Step 1.3: drive the deadlock and capture

`mpremote ... resume fs cp <local> :remote` is the canonical repro.
Choose a small file (~2-4 KB; the R24 history captured "65 URBs
flow through naturally then stops" on a 2 KB cp, so 4 KB should
trigger reliably).

```
# r24-wip firmware flashed. usbip attached. esp32-s3 cycled.
# Pico CDC visible at /dev/ttyACM<N>.

# Test file: 4 KB of repeating data on the host.
dd if=/dev/urandom of=/tmp/r27-cp-test.bin bs=4096 count=1

# Drive deadlock with UART captured:
cat /dev/serial/by-id/usb-1a86_USB_Single_Serial_5A45040839-if00 \
    > /tmp/r27-deadlock-uart.log &
UART_PID=$!
mpremote connect /dev/ttyACM<N> resume fs cp /tmp/r27-cp-test.bin :test.bin
# (this hangs)

# After ~30 s of confirmed hang:
sudo kill -9 $(pgrep -f "mpremote.*fs cp") 2>/dev/null
kill $UART_PID
```

The mpremote process will be unkillable in D-state; `kill -9` on
it is a no-op. Document it and move to recovery.

The UART log captures the verbose URB submit/done lines from
step 1.1. The pattern to look for:

- Hypothesis A (bulk-OUT hangs): `submit ... ep=0x02 dir=OUT seq=N`
  with NO matching `done seq=N`. Diagnostic action: dump
  `tuh_edpt_get_state(ep=0x02)` immediately after the hang to see
  if TinyUSB thinks the EP is still busy or if our submit was
  silently rejected.
- Hypothesis B (bulk-IN data corruption): all submits have
  matching `done` lines but cdc-acm on the kernel side stops
  consuming. Diagnostic action: dump the actual IN payload bytes
  (first 16 of each URB) and compare to the expected raw-REPL
  framing. Cross-reference `dmesg | grep cdc_acm` for "ignoring
  ill-formed" type errors.
- Hypothesis C (control-vs-bulk race): the `done` lines for ep=0
  control xfers interleave with bulk-OUT/IN at suspicious points.
  Diagnostic action: time-correlate the control xfer vs the next
  bulk that fails to complete.

Each diagnostic is one bench cycle. The pattern is "run, look at
log, identify hypothesis, design fix" not "run all three in
parallel".

### Step 1.4: fix shapes

For hypothesis A (bulk-OUT hangs):

If `tuh_edpt_xfer` is silently rejecting (possibly because of
gotcha #2: only one transfer in flight per (dev,ep), and we
have a residual flag stuck busy), the fix is to add a watchdog
on the responder lane: any URB without a completion in N ms
(say 250 ms or pipeline-depth-aware) gets manually
synthesised and the EP gets `tuh_edpt_close + tuh_edpt_open +
CLEAR_FEATURE(ENDPOINT_HALT)` to fully reset (gotchas #5, #6).
This is the same shape as the existing cancel-storm path but
extended to non-cancel timeout cases.

For hypothesis B (data corruption):

If actual bulk-IN bytes diverge from expected, the candidate
causes are: data-toggle desynchronisation that gotcha #6 was
supposed to fix but didn't fully (test by checking PID toggle
state via `tuh_edpt_get_state` if exposed, or by reading
HCCHARn registers via the R25 stage B-style ISR trace), or a
buffer-aliasing issue where two URBs share a payload buffer
under fast-cycle. Buffer aliasing inspection: walk the
`inflight_urb_t.in_buf` allocation sites in `usbip_server.c
intake_submit` and trace lifetime through the responder.

For hypothesis C (control-bulk race):

The fix shape is to serialise the control xfer against the
bulk EPs on the same device. R23 IDF backend has a per-EP
submit mutex that serialises submit calls; R24's TinyUSB port
also has `ep_submit_mutex` (see r24-wip's `usbhost.c`). Verify
control-xfer path uses the EP0 mutex correctly; if not, add it.

### Step 1.5: acceptance for Phase 1

`mpremote ... resume fs cp /tmp/r27-cp-test.bin :test.bin`
succeeds 5 times in a row without:

- mpremote process entering D-state
- ESP32 panic
- vhci_hcd D-state on the Linux host

Run `mpremote 30/30` after the first success to confirm the
single-call cancel-storm path is still working (no regression
on the existing r24-wip behaviour).

### Phase 1 result (2026-05-05, opus pickup)

Root cause and fix shape detailed in `r27-phase1-findings.md`.
Headline:

- Root cause: TinyUSB DWC2 slave-mode multi-packet bulk-OUT race
  (upstream issue hathach/tinyusb#3623, upstream open PR #3632 is a
  partial fix). ESP32-S3 cannot use the DMA-mode workaround
  recommended upstream (no L1 cache handling for DWC2 DMA).
- Fix 1: submodule patch backporting upstream PR #3632 (txsts
  re-read in handle_txfifo_empty inner loop). lib/tinyusb pin
  3af1bec1a -> a8b5bf4e7.
- Fix 2: caller-level OUT chunking in usbhost.c. Multi-packet OUT
  URBs are submitted as a sequence of MPS-sized single-packet
  sub-URBs. Continuation runs from xfer_complete_cb.
- 5/5 fs-cp PASS, 0 watchdog fires.
- 30/30 single-call mpremote PASS.
- Open follow-ups (see findings doc): cancel-vs-chunk UAF
  theoretical race, clear_feat=0 recovery-path issue, throughput
  impact measurement (Phase 2).

## Phase 2: measure TinyUSB throughput

Once fs-cp doesn't deadlock, run the same benches that ran on R23
IDF:

- `cdc_throughput.py read_test /dev/ttyACM<N>` (chunked bench used
  through R20-R25). Capture the per-bufsize KiB/s table.
- The R23 µs instrumentation (`idf_timing` / per-direction
  breakdown lines) ported into the TinyUSB build's `usbhost.c`.
  This requires `esp_timer_get_time()` calls around `tuh_edpt_xfer`
  submit and inside `xfer_complete_cb`. Stage the same
  `(int32_t)` cast pattern from R23.

Expect: `cdc_throughput.py read_test` runs to completion at all
bufsizes (chunked bench has quiescent gaps; should not trigger
R26).

### Comparisons

| Metric | IDF baseline (R25) | TinyUSB result | Interpretation |
|---|---|---|---|
| Throughput bufsize=256 | 11.2 KiB/s | ? | direct comparison |
| Throughput bufsize=4096 | 9.5 KiB/s | ? | direct comparison |
| `avg_round` IN | 165 ms | ? | per-URB IDF -> TinyUSB equivalent |
| `min_round` IN | 87-110 us | ? | wire-time floor |

If TinyUSB hits 600+ KiB/s on bufsize=256: IDF scheduling was
the cause; investigation closes here, migration becomes the
answer.

If TinyUSB hits ~11 KiB/s on bufsize=256 with similar `avg_round`:
the cause is in a layer shared between IDF and TinyUSB. Phase 5.

If TinyUSB hits something in between (say 50-200 KiB/s): partial
win; one of the IDF-specific layers contributed but a shared
factor remains. Phase 5 with a narrower target.

### Step 2.1: port R23 µs instrumentation to r24-wip's usbhost.c

The R23 instrumentation is currently in main's
`src/c_modules/usbhost/usbhost.c` `transfer_done_cb` (around line
624 per the corrected `r25-isr-instrumentation.md`). r24-wip's
`xfer_complete_cb` is the equivalent. The instrumentation's
`inflight_urb_t.t_submit_pre / t_submit_post` fields and the
ESP_LOGI shape with `(int32_t)` casts port directly.

### Step 2.2: run the chunked bench

```
sudo usbip attach -r 192.168.0.166 -b $(sudo usbip list -r 192.168.0.166 \
    | grep -oE "1-[0-9]+" | head -1)
ls /sys/bus/usb/devices/5-1/5-1:*/tty/   # find the resulting ttyACM<N>

ESP32_PORT="/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A45040839-if00"
stty -F "$ESP32_PORT" 115200 raw -echo
cat "$ESP32_PORT" > /tmp/r27-throughput-uart.log &
UART_PID=$!
python3 test/integration/phase3/cdc_throughput.py /dev/ttyACM<N> \
    2>&1 | tee /tmp/r27-throughput-bench.log
kill $UART_PID
sudo usbip detach -p 0
```

### Step 2.3: extract numbers

```
strings /tmp/r27-throughput-uart.log | grep idf_timing
grep "summary\|kib_s" /tmp/r27-throughput-bench.log
```

Compare against the R25 stage B numbers in
`r25-isr-instrumentation.md` (avg_proc=14us, avg_gap=11ms,
throughput_256=11.2 KiB/s).

### Step 2.4: if TinyUSB lands ~11 KiB/s, port stage B ISR trace

If the chunked bench shows similar IDF-class numbers, port the
R25 stage B ISR-side timing into TinyUSB's
`lib/tinyusb/src/portable/synopsys/dwc2/dwc2_hcd.c`
`dwc2_hcd_int_handler`-style routines. We have edit rights to
TinyUSB (the user's submodule pin), so the patch can land in the
TinyUSB tree directly without the IDF-tree-vendoring concern.

Goal: confirm whether the 11 ms gap is identical between IDF and
TinyUSB or whether it's slightly different (which would localise
which layer the gap lives in).

### Step 2.5: run the never-NAK harness for R26 disambiguation

After step 2.2's chunked bench completes successfully, run the
`/tmp/r25-nonak-harness.py` continuous-fill harness against the
same TinyUSB-migrated firmware (full reproduction steps in
`r26-lwip-pbuf-corruption.md`). Two outcomes:

- **Crash recurs (LoadProhibited in `pbuf_free` from
  `lwip_recv_tcp`)**: R26 is stack-agnostic. Phase 3 picks the
  parallel-fix-track route; R26 stays an active risk on main.
- **No crash for >30 s of continuous streaming**: R26 was
  IDF-induced. Phase 3 picks the deferral route; the R26 doc
  moves to the `r25-idf-backend` frozen branch and the active
  R26 row in `plan/overview.md` is removed.

This step is the disambiguation that the R26 origin diagnosis
(2026-05-05) flagged as needed. Adds maybe 15 minutes to Phase
2 wall-clock; outcome decides Phase 3 plan.

## Phase 3: R26 (lwIP pbuf corruption) ordering decision

R26 is a latent lwIP heap-corruption bug surfaced by sustained
bulk-IN streaming (`r26-lwip-pbuf-corruption.md`). The crash is
in `pbuf_free` called from `lwip_recv_tcp` called from
`usbip_server.c read_exact`. The 2026-05-05 origin diagnosis at
the bottom of `r26-lwip-pbuf-corruption.md` could not determine
from static reading alone whether the bug is IDF-induced
(disappears on TinyUSB) or stack-agnostic (persists on TinyUSB).
Static analysis points more strongly at the stack-agnostic
candidate (Wi-Fi-driver / esp_netif / lwIP issue), but the
USB-DMA candidate cannot be ruled out without runtime testing.
The disambiguating experiment is R27 Phase 2 itself: run the
never-NAK harness on TinyUSB-migrated firmware and observe.

### Two orderings

**Option 3a: fix R26 first, then sustained-streaming bench.**

Cleaner data. Sustained streaming on TinyUSB will need this fixed
to even run for >5 seconds. Risk: the bug may be a deep lwIP-side
issue requiring upstream IDF or lwIP patches; could delay
throughput data for weeks.

**Option 3b: defer R26, use chunked benches only for now.**

The `cdc_throughput.py read_test` chunked bench has 50-200 ms
quiescent gaps between bufsizes that let lwIP drain; it ran to
completion on IDF and should on TinyUSB. We can get bufsize=256
through 16384 throughput numbers without hitting R26. Risk:
chunked benches don't stress the host stack the same way
sustained streaming does; some race conditions or scheduling
issues only show up under sustained load.

### Recommendation

**Defer R26 to after Phase 2 chunked bench, then re-evaluate.**
The chunked bench gives us the IDF-vs-TinyUSB throughput delta
that drives the rest of the plan. R26 is necessary for production
safety regardless, but its priority depends on whether Phase 2
shows a clean migration win (in which case R26 becomes the next
focused task) or a 11-ms-gap continuation (in which case the
TinyUSB-side throughput debugging in Phase 5 is more urgent
than R26).

If Phase 5 happens, R26 still needs fixing eventually, but the
sustained-streaming requirement is more about throughput
verification than functional correctness once the chunked bench
already validates per-URB-class timing.

### If Phase 2 shows TinyUSB matches Linux EHCI

R26 fix is the next focused task immediately. Sustained streaming
becomes a primary use case (mpremote fs cp at 600+ KiB/s, large
file transfers); the latent crash is now a hard ship-blocker.

### If Phase 2 shows TinyUSB matches IDF

R26 fix is still needed but lower priority than Phase 5 throughput
debug. Both run in parallel as backlog items.

## Phase 4: migration finalisation

Once Phases 1-3 decide the throughput question, the migration off
IDF onto TinyUSB needs to be made permanent on main.

### Open decisions for the user

These need a call before Phase 4 executes:

1. **Branch and merge strategy.** `r24-wip` has 11 commits
   including investigation work (`d9f192f` rewrite, `2ed6ea2` and
   `7aead05` build fixes, `1697eea f1de4b3 1130bbc` early bug
   fixes, `75f2b35 acc2c5b a832f62 3285e6c` cancel-storm
   handling, `79ee842` history doc). For a clean main:
   - **Option a**: merge `r24-wip` as-is via merge commit; preserve
     full investigation history.
   - **Option b**: rebase + interactively squash into 2-4 logical
     commits (rewrite, cancel-storm fix, instrumentation, history
     doc). Cleaner main but loses commit-by-commit attribution.
   - **Option c**: cherry-pick the load-bearing commits onto a
     fresh branch off current main (which has R23, R25, R26 work
     not on r24-wip); resolve any conflicts; new clean history.
     Most work, cleanest result.

   Recommendation: option c. r24-wip forks at `1e656cf`; main is
   ~30 commits ahead with R23/R25/R26 docs and R23 µs
   instrumentation. A rebase will conflict on `usbhost.c` since
   both branches modified it heavily. Cherry-pick of just the
   TinyUSB conversion onto current main, with conflicts resolved
   manually, gives a single coherent commit on main.

2. **IDF backend retention.** The current `usbhost.c` on main is
   the IDF host backend. Three options:
   - **Drop IDF entirely.** Smaller maintenance surface; loses
     the fallback if TinyUSB regresses on a future IDF or
     TinyUSB bump. The R23 deep-dive instrumentation in IDF gets
     deleted.
   - **Build flag.** `MICROPY_USBHOST_BACKEND=tinyusb` (default)
     vs `=idf`. Two `usbhost.c` files (or one with `#ifdef`
     blocks; getting messy). Keeps fallback at the cost of
     maintaining both.
   - **Branch fallback.** Drop IDF on main but keep an
     `r25-idf-backend` branch as a frozen reference. Lighter
     than dual-maintenance but still recoverable.

   Recommendation: branch fallback. The IDF backend is well-
   tested for 30/30 mpremote and chunked bench; preserving it
   as a branch lets a future regression fall back without
   carrying the dual-build complexity.

3. **R23 / R25 IDF instrumentation removal.** With the IDF
   backend gone or moved to a branch, the R23 µs instrumentation
   in `usbhost.c` (commit `d4f1e3b` and the per-direction
   counters from R23 deep-dive) and the R25 stage B IDF tree
   patch all become irrelevant. Either delete with the IDF
   backend, or mirror the equivalent in TinyUSB's
   `dwc2_hcd.c` for ongoing comparison capability.

   Recommendation: port the R23 `idf_timing` shape into the
   TinyUSB-equivalent ISR (the dwc2 host completion handler).
   Lose the R25 stage B IDF-tree patch but document its
   shape in a backlog doc so it can be re-applied if any
   future IDF-backend bench is needed.

4. **Upstream `machine.USBHost()` API alignment.** Locked in
   2026-05-05: stay on raw `tuh_edpt_xfer`. See "Decisions
   locked in" section for context. r24-wip already uses this
   path; the migration commit must document this explicitly so
   the next reader doesn't try to "improve" by switching to
   class drivers.

5. **`src/VERSIONS` updates.** Once the migration lands, bump
   the MicroPython entry to reflect any submodule changes the
   user makes upstream (TinyUSB submodule pin, machine-usbhost
   branch tip), and add a TinyUSB row.

6. **`docs/spec.md` and `docs/architecture.md`.** Search for
   "IDF host" / "usb_host_*" references and update to TinyUSB.
   This is bookkeeping but it matters for new contributors.

### Step 4.1: file the decisions for user review

The plan stops at "open decisions for the user" before any
merge or branch operation. Each decision above gets a short
backlog entry; the user picks; only then does Phase 4 execute.

### Step 4.2: risk register cleanup

After migration:

- R24 (TinyUSB host pivot incomplete): close. Pivot complete.
- R25 (IDF bulk-IN throughput): mark "superseded by TinyUSB
  migration" if Phase 2 showed migration solved it; or "open,
  see R27 Phase 5" if migration did not solve it.
- R26 (lwIP pbuf corruption): unchanged (independent of host
  stack).
- Add an R28-or-similar row for any TinyUSB-on-DWC2 issue
  that Phase 5 reveals.

## Phase 5: TinyUSB-side throughput debug (only if Phase 2 negative)

Triggered only if Phase 2 measurement shows TinyUSB lands at
~11 KiB/s with the same 11 ms `avg_gap` shape. The cause is in
a shared layer.

### Concrete first steps

1. **Compare TinyUSB's DWC2 HCD register configuration to
   Linux's `drivers/usb/dwc2/`.** Both run on the same
   Synopsys IP. Specifically check:
   - `HCFG.PerSchedEna` (periodic schedule enable)
   - `HFIR` (frame interval register)
   - `HCCHARn.MC` (multi-count for OUT, NOT for IN bulk)
   - `HCFG.DescDMA` and `HCFG.AHBSingle`
   - The FIFO partitioning between RX, NPTX, PTX

   If TinyUSB sets these conservatively and Linux sets them
   permissively (or vice versa), patch TinyUSB to match.

2. **Add the R25-style ISR-internal timestamps inside TinyUSB's
   DWC2 channel completion path.** In `dwc2_hcd.c`'s
   `handle_channel_in_irq` / `handle_channel_out_irq` (function
   names approximate; the actual paths vary by TinyUSB version).
   Capture entry/exit timestamps and channel state into a ring
   buffer same as the R25 stage B IDF patch.

3. **Patch `dwc2_hcd.c` to issue IN tokens more aggressively.**
   If TinyUSB's bulk-IN path sets a NAK retry interval or polls
   on a slower cadence than expected, the patch can either
   shorten the retry interval or pre-issue IN tokens
   speculatively. Speculative-issue is dangerous (NAK floods
   waste bus time) but on a dedicated FS-host link with one
   active device, the NAK rate is acceptable.

4. **USB protocol analyzer trace.** If hardware available, capture
   actual bus traffic during the 11 ms gap. Distinguishes
   "device NAKs and host waits" from "host issues no token at
   all" from "host issues IN token, device delays response".
   Most direct way to localise the cause once we know it's not
   in the host software.

### Upstream contribution path

Any TinyUSB DWC2 fix discovered here should be filed upstream
(`hathach/tinyusb` PR). The user's contribution history makes
this realistic. Filing IDF patches upstream is a longer
process; preferring TinyUSB is part of why this migration is
the chosen path.

## Risk register (this plan)

| Risk | Detection | Mitigation |
|---|---|---|
| The fs-cp deadlock has more than three causes (A/B/C) and our instrumentation misses the actual one | Step 1.3 log shows none of A/B/C cleanly; some other URB-completion gap appears | Add more granular instrumentation (per-channel state, TinyUSB ep_status fields) and re-run; iterate |
| `vhci_hcd` module unload also hangs after deadlock; iteration cost is full host reboot | First iteration of step 1.3 leaves a stuck mpremote | Document, pre-arrange remote reboot capability, batch fewer experiments per session |
| Phase 2 chunked bench triggers R26 (it should not but might if any chunked iteration crosses a threshold) | ESP32 panic during cdc_throughput.py | Switch to the smallest bufsize range first (256-1024 only); if those run clean, ramp up |
| TinyUSB bench result is same ~11 ms gap; Phase 5 turns into a long DWC2 dive | Phase 2 step 2.3 shows IDF-class numbers | Treat as the more likely outcome and budget Phase 5 generously; the user is committed to this lever |
| The TinyUSB submodule pin we're on (0.20.0 per VERSIONS) has known DWC2 host bugs that newer pins fix | Phase 2 result; cross-check against TinyUSB issue tracker | Bump TinyUSB submodule; this is one of the user's edit-rights advantages |
| The cherry-pick onto current main hits massive conflicts in usbhost.c | Phase 4 step 4.1 trial cherry-pick | Fall back to merge commit (option a) and do a separate cleanup commit later |
| Phase 1 step 1.1 verbose instrumentation perturbs the timing enough to mask the bug | Phase 1 captures clean URB chain logs but the deadlock doesn't reproduce with verbose on | Trace via lower-overhead mechanism (counters incremented in callback, dumped on demand) instead of per-URB ESP_LOGI |

## Decisions locked in (2026-05-05)

User decisions made before R27 starts:

1. **API alignment: raw `tuh_edpt_xfer`.** `r24-wip` already uses
   the raw URB-forwarding path, not class drivers. Confirmed:
   stay on raw `tuh_*` API. usbip forwards URBs at sub-class
   level; the kernel-side cdc-acm is already doing class-level
   demux, so wrapping in TinyUSB CDC class driver gains nothing
   and would require a major rewrite of `usbip_server.c` URB
   dispatch. Phase 4 step 4 (originally listed as an open
   decision) is closed: stay on the raw API.

2. **Branch strategy: rebase, not cherry-pick.** Rebase test run
   2026-05-05 (see "Rebase test outcome" below) showed the
   rebase is not clean — substantive conflicts in `usbhost.c`
   and `mpconfigboard.cmake` on the very first commit
   (`d9f192f`). Proceed with rebase + manual conflict
   resolution as Phase 4 step 4.1 below; cherry-pick remains a
   fallback if rebase becomes too painful in practice.

3. **R26 ordering: depends on origin.** If R26 is IDF-induced
   it goes away on TinyUSB; document on `r25-idf-backend`
   branch and remove the active row. If R26 is stack-agnostic
   it persists on TinyUSB and needs a parallel fix track.
   Origin diagnosis appended to `r26-lwip-pbuf-corruption.md`
   (Origin diagnosis 2026-05-05) reads "cannot determine from
   static reading alone; static analysis points more strongly
   at Wi-Fi-driver / esp_netif / lwIP candidate (stack-agnostic)
   than USB DMA candidate (IDF-induced), but cannot rule out
   the latter without runtime testing." The disambiguating
   experiment is R27 Phase 2 itself: run the never-NAK harness
   on TinyUSB-migrated firmware. Decision is therefore
   delegated to Phase 2 outcome rather than chosen now.

## Rebase test outcome (2026-05-05)

Sandbox rebase of `r24-wip` (tip `79ee842`) onto current main
(`79c65e8`):

```
git checkout -B test-rebase-r24 r24-wip
git rebase main
```

First commit (`d9f192f` "R24 step1: rewrite usbhost.c on
TinyUSB host primitives") fails to apply with conflicts in:

- `src/c_modules/usbhost/usbhost.c`: 5 conflict regions
  spanning approximately lines 85-99, 123-155, 692-943, 983-999,
  and 1041-1463 in the conflicted file. The two large regions
  (250 and 420 lines) cover the main body of the IDF backend
  vs the TinyUSB rewrite; substantive content conflict, not
  trivial. Includes the R23 µs instrumentation
  (`transfer_done_cb` timing block, R23 deep-dive commit
  `d4f1e3b`) and the R25 step 3 worker-priority comment
  (`USBHOST_WORKER_TASK_PRIORITY=20`, commit `4519860`) on
  the main side, against the `tuh_*` rewrite of every callback
  and inflight allocator on the r24-wip side.
- `src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.cmake`: 1
  conflict region. Both branches modified the comment near the
  TinyUSB CFG_TUH_* defines block; r24-wip added
  `CFG_TUH_API_EDPT_XFER=1` and changed the surrounding
  comment. The conflict is small but both branches care.
- `src/c_modules/usbhost/micropython.cmake`: shown as
  `M` (auto-merged), so cleanly resolved.

Sandbox cleanup performed:

```
git rebase --abort
git checkout main
git branch -D test-rebase-r24
```

After cleanup, working tree on main is clean; no leftover
artifacts.

### Implication

Rebase is workable but not free. The main `usbhost.c` conflict
will require manual integration of three things:

1. The base TinyUSB rewrite from r24-wip (the `tuh_*` API
   plumbing, inflight allocator changes, cancel-storm
   handling).
2. The R23 µs instrumentation from main (port the
   `transfer_done_cb` timing block onto r24-wip's
   `xfer_complete_cb` equivalent — already listed in R27
   Phase 2 step 2.1).
3. The R25 worker-priority change from main
   (`USBHOST_WORKER_TASK_PRIORITY=20`).

The R24 cancel-storm handling on r24-wip and the R23/R25
instrumentation on main are not architecturally
incompatible; they touch overlapping code regions but are
logically additive. The rebase resolution is "take r24-wip's
shape, port main's instrumentation onto it" rather than "pick
one or the other".

The 11-commit rebase will likely need 5-10 manual conflict
resolutions across the early commits as each touches files
that main also modified. The later commits (cancel-storm
fixes `75f2b35` through `3285e6c`) probably go cleanly since
they only touch r24-wip-specific code paths. The history doc
commit `79ee842` is doc-only and may conflict only on the
`r24-wip-history.md` banner that main has since updated
(small, easy).

Recommended rebase flow for Phase 4 step 4.1:

1. Resolve `d9f192f` conflicts: take r24-wip's `usbhost.c`
   wholesale, then integrate the R23 timing block and R25
   priority change as separate-but-fixup commits during the
   rebase.
2. Continue through `f1de4b3` ... `1130bbc` ...
   `75f2b35` etc.; each is a small fix on r24-wip's own code.
3. `79ee842` (the history-doc commit) needs its banner
   reconciled with the corrected version on main
   (`r24-wip-history.md` was rewritten 2026-05-03).

If conflict resolution exceeds ~3 hours wall-clock, fall back
to cherry-pick: cherry-pick `d9f192f` onto main with manual
resolution, then squash subsequent r24-wip fixes into that
single migration commit. Loses commit-by-commit attribution
but is faster.

## Other open decisions (for user before starting)

1. **Phase 4 IDF backend retention**: default plan is "branch
   fallback" (drop from main, keep on r25-idf-backend frozen
   branch). User can override to dual-build or full delete.

2. **Phase 5 trigger criterion**: default is "if Phase 2 chunked
   bench bufsize=256 < 50 KiB/s". Anything above that is treated
   as a partial win and Phase 5 budget is reduced. User can
   override the threshold.

## Hardware required

Same as R25:

- ESP32-S3 dev board on `mpy-dev` label `esp32-s3` (CH340N serial
  `5A45040839`).
- A Pico-class MicroPython device exposed via USB/IP from
  192.168.0.166 (busid 1-N). The MicroPython Pico W on remote is
  the established device.
- For Phase 1 fs-cp repro: a small test file (4 KB random data
  is fine).
- For Phase 5 if hardware analyser is acquired: any USB FS
  protocol analyzer that can capture between the ESP32-S3 USB
  host port and the Pico CDC device.

## Out of scope

- The 7 documented TinyUSB-on-DWC2 gotchas in
  `r24-wip-history.md`. Treat as confirmed prior art; do not
  re-derive.
- The R24-era `mp_usbh_init_tuh()` / `tuh_mount_hook` /
  `tuh_umount_hook` integration with upstream
  `machine.USBHost()`. Already correct on r24-wip; just inherits
  into the migrated code.
- Class-driver-based forwarding (option B in R24-wip-history's
  "Architectural decision the migration didn't anticipate").
  R27 stays on raw `tuh_edpt_xfer` (option A) per the decision
  in Phase 4 step 4 above.
- TinyUSB submodule pin bump as a separate task. If Phase 5
  finds the existing 0.20.0 pin has a fixable issue in newer
  TinyUSB, that's done as part of Phase 5 not separately.
- ESP32-P4 or any board change. The R25 corrections established
  that the gap is not silicon-bound; a board change is not
  justified by the current data. The plan stays on ESP32-S3.

## Cross-references

- `r23-deep-dive-findings.md`: original µs IDF timing showing
  165 ms IN avg_round.
- `r25-isr-instrumentation.md` (corrected): 11 ms ISR-fire gap;
  cause unestablished; eight prioritised next steps (this plan
  is one of them).
- `r25-direct-usb-baseline.md`: 677 KiB/s direct-USB through the
  same Pico-class device on the same FS bus; rules out silicon.
- `r24-wip-history.md`: load-bearing resume doc for r24-wip;
  seven gotchas, branch state, what works.
- `r24-bug-findings.md`: fs-cp deadlock post-mortem; three
  hypotheses A/B/C.
- `r24-bug-plan.md`: original investigation plan for the deadlock;
  step 1 instrumentation shape.
- `r26-lwip-pbuf-corruption.md`: latent lwIP bug; affects Phase 3.
- `plan/overview.md` risk register: R24 (parked), R26 (open).
- `andrewleech/micropython#7`: upstream `machine.USBHost()` PR;
  TinyUSB submodule pin 0.20.0; `mp_usbh_init_tuh()` is from
  `shared/tinyusb/mp_usbh.c` in this PR.

## Notes from reading r24-wip not surfaced in resume docs

While preparing this plan I noticed two things on r24-wip that the
existing `r24-wip-history.md` doesn't explicitly call out and that
matter for resuming:

1. **Managed-component conflict workaround.** r24-wip's
   `usbhost.c` (lines 64-87) has local wrapper functions
   `local_get_device_desc` etc. because the IDF managed component
   `espressif__tinyusb` (device-only) wins the include-path race
   ahead of the upstream submodule pin (host-mode). The managed
   component's `libespressif__tinyusb.a` is device-only and does
   not provide host-side `_sync` implementations. r24-wip papers
   over this with local async-then-block wrappers. The structural
   fix would be to remove the managed component from the build
   (it's pulled in transitively by something IDF-side). Worth
   confirming whether this workaround is still needed once the
   migration cleanup happens; if not, the wrappers can go away.

2. **`traceISR_EXIT_TO_SCHEDULER` pre-define.** r24-wip's
   `usbhost.c` (lines 27-40) pre-defines this macro to a no-op
   to work around a FreeRTOS conditional that the second
   (`micropython.elf`) build target doesn't see ESP_PLATFORM
   for. The `mpconfigboard.cmake` comment on r24-wip explicitly
   notes the bug DOES reproduce on this branch (the comment on
   main says it does NOT reproduce, which is true for the IDF
   backend but not for r24-wip with TinyUSB host). Carry the
   workaround into the migration; it's a known quirk of dual-
   target compilation in the MicroPython esp32 port.

These are not blockers but are non-obvious mid-migration foot-
guns. Worth flagging here so the next agent doesn't waste cycles
re-discovering them.
