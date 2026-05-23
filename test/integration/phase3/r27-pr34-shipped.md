# R27 phase 2: PR 3 + PR 4 SHIPPED

Status: both upstream-bound TinyUSB fixes implemented in
`lib/tinyusb` on branch `r27-fix-txfifo-recheck`, submodule pin
bumped, verified 60/60 PASS=N FAIL=0 across two consecutive 30/30
mpremote cdc-acm cycled-close runs.

## Final commits

`lib/tinyusb` branch `r27-fix-txfifo-recheck`:
* PR 1 `6b0f49b06` - hcd/dwc2: Save post-transfer PID in DMA-mode IN handler.
* PR 2 `a8b5bf4e7` - hcd/dwc2: Re-read txsts inside per-packet FIFO write loop.
* PR 3 `7d112f9c8` - hcd/dwc2: hcd_edpt_abort_xfer should fire xfer_complete callback.
* PR 4 `d56fbc69c` - host: Honour timeout_ms in tuh_control_xfer.
* `6fa7cfbb4` - R27 DEBUG hcint trace (not for upstream)

`src/micropython` pin: `dbb5d71c7` (bumps lib/tinyusb to 7d112f9c8).
`mpy-pod` `src/micropython` pin: bumped in commit `010649f`.

Patches saved at:
* `test/integration/phase3/r27-pr3-abort-callback.patch`
* `test/integration/phase3/r27-pr4-control-timeout.patch`

## What the patches do

### PR 4 (`r27-pr4-control-timeout.patch`)

Honour `xfer->timeout_ms` in the synchronous (`complete_cb == NULL`)
branch of `tuh_control_xfer`. The field was previously a placeholder
("not supported yet"). On non-zero timeout, returns `false` with
`xfer->result = XFER_RESULT_FAILED` and aborts the EP0 channel via
`hcd_edpt_abort_xfer` so a subsequent control transfer on the same
daddr can proceed.

Two non-obvious fixes folded in:

1. **Wait-forever path bit-identical to baseline.** The deadline
   timestamp (`tusb_time_millis_api()`) is captured ONLY when a
   non-zero timeout is requested. The earlier draft called the time
   API unconditionally before the loop and that single extra call
   into FreeRTOS shifted task scheduling enough to introduce a ~5%
   flake rate on the cdc-acm cycled-close test even when the caller
   passed `timeout_ms = 0`. Gating the call removes that path
   entirely from the hot path.

2. **Designated init for three callers.** `usbh.c:1464`
   (process_enumeration kick-off), `hid_host.c:586`
   (hidh_set_config), and `cdc_host.c:785` (cdch_set_config) all
   declared `tuh_xfer_t xfer;` then assigned individual fields. With
   the new `timeout_ms` at offset 28, those declarations left the
   field reading FreeRTOS stack canary (`0xA5A5A5A5`). Converted to
   designated init so the field is zero-initialised. Bug only
   surfaces when something reads `timeout_ms`, but the underlying
   uninit-stack-field issue is a latent bug across the codebase.

### PR 3 (`r27-pr3-abort-callback.patch`)

`hcd_edpt_abort_xfer` now reliably fires `hcd_event_xfer_complete`
with `XFER_RESULT_FAILED` for the in-flight transfer. The previous
implementation disabled the channel but left the IRQ handler with
no XFER_COMPLETE / STALL / BABBLE / XACT_ERR bit set, so the
dispatch handlers returned `is_done = false` and the natural
callback never fired. Downstream callers had to synthesise
completions externally.

Mechanism: an `aborted : 1` bit added to `hcd_xfer_t`.
`hcd_edpt_abort_xfer` sets it before calling `channel_disable`. The
outer dispatch loop in `handle_channel_irq` checks the flag. If set
AND the channel is no longer enabled (`hcchar.enable == 0`), force
`is_done = true` and report FAILED. The `enable` check
distinguishes a genuine abort halt from a NAK retry that re-armed
the channel mid-IRQ - the earlier draft of PR 3 fired FAILED on
re-armed channels too, deallocating them mid-flight and causing a
~33% flake rate.

Also tightened the contract: `hcd_edpt_abort_xfer` now returns
`false` when no in-flight transfer was found (no channel allocated
for the EP), instead of returning `true` unconditionally.

The fix is symmetric across DMA and slave-mode IN/OUT handlers
because they all dispatch through `handle_channel_irq`.

## Verification

| Item | Result |
|---|---|
| Build (idf.py / make) | PASS, 1857328 B |
| 30/30 cycled mpremote (run 1) | PASS=30 FAIL=0 |
| 30/30 cycled mpremote (run 2) | PASS=30 FAIL=0 |
| dmesg `usb_poison_urb` | 0 entries |
| Watchdog `synth` fires | 0 |
| cdc-acm cleanup wedge regression | none |

Cumulative bench since the cdc-acm cleanup wedge was fixed in
`d846d31`: 90+/90+ PASS, no regressions.

## Bisection history (for the record)

The path to 60/60 PASS went through several false starts. Captured
here so the same hypotheses are not re-pursued:

| Step | Build | Result |
|---|---|---|
| Baseline | no PR 3, no PR 4 | 30/30 PASS |
| PR 4 v1 | timeout loop without init fix | boot enum TIMEOUT (canary garbage) |
| PR 4 v2 | + designated init for 3 callers | 30/30 PASS once, then 8/9 once - flaky |
| PR 4 v3 | + gate `tusb_time_millis_api()` behind `timeout_ms > 0` | 30/30 PASS, stable |
| PR 4 v3 + PR 3 v1 | abort fires callback unconditionally on HALTED | 2/3 PASS - flaky (re-arm dealloc) |
| PR 4 v3 + PR 3 v2 | abort callback gated on `hcchar.enable == 0` | 30/30 PASS, stable (60/60 across two runs) |

Two distinct bugs in the same area: an unconditional time-API call
on a hot path shifts scheduling, and an unconditional FAILED
override on HALTED races against NAK retry re-arm. Both fixed by
gating the conditions tighter.

## Open follow-up: device-side synth removal

The synth path in `src/c_modules/usbhost/usbhost.c` is now defensive
only. PR 3 makes the natural callback fire reliably; the existing
atomic CAS on `inflight->completed` lets natural always win. The
~150 lines of synth code (counters, CAS arbitration in cancel_ep,
mirror in watchdog_recover) can be deleted as a follow-up.

Holding off on the deletion until:
1. PR 3 has landed upstream and the submodule pin tracks the merged
   commit (current pin is on a local branch).
2. A longer bench run (multi-hour cycled mpremote + fs cp) confirms
   PR 3 doesn't have a long-tail edge case where the natural
   callback skips.

If both hold, the synth code drops out cleanly. The watchdog stays
as a final safety net for any URB that goes >2 s without a callback
of any kind.

## What this means

* The cdc-acm cleanup wedge fix from `d846d31` (drop CLEAR_FEATURE)
  remains in place and is the primary host-stability fix.
* PR 3 + PR 4 in lib/tinyusb make the upstream USB host stack robust
  enough that downstream callers do not need to synthesise
  completions or worry about hung control transfers.
* mpy-pod can be the upstream reference for both PRs - we have
  bench-verified them across the cdc-acm close-storm scenario that
  exercises both abort paths and control transfers under load.
