# R24 (TinyUSB host pivot) — work history

> **Pivot rationale confirmed (2026-05-03, per `r23-deep-dive-findings.md`):**
> µs-resolution IDF timing data collected. Case C confirmed: avg_round=157 ms
> for bulk-IN (vs 110 µs wire floor). The IDF host stack is the bottleneck.
> New structural finding: bulk-OUT is fast (271 µs avg), bulk-IN is slow
> (165 ms avg); this suggests IDF serialises IN tokens per endpoint.
> Before retrying TinyUSB, check whether TinyUSB also serialises per-ep
> bulk-IN, since TinyUSB gotcha #2 (`tuh_edpt_xfer` one-in-flight per ep)
> implies it may have the same limitation. The D-state deadlock is the more
> urgent blocker. Next task: `r25-fix-fs-cp-deadlock-plan.md`.

This branch holds an attempt to migrate `src/c_modules/usbhost/usbhost.c`
from the IDF `usb_host_*` API to TinyUSB's host primitives. Triggered
by the (later-questioned, now confirmed) hypothesis from R23-corrected
findings that the IDF host stack on DWC2 ESP32-S3 was rate-limiting bulk
URB processing at ~88/sec.

The attempt is **parked, not abandoned**. This file documents what was
tried, what worked, what broke, and the non-obvious facts learned —
so any future retry can start from where this left off without
re-discovering the same gotchas.

## Branch state

HEAD: `3285e6c`. Eleven commits forking from `1e656cf` on main.

Architecture summary: replaces the IDF backend in `usbhost.c` with
`tuh_*` primitives. Calls `mp_usbh_init_tuh()` (from upstream PR #7's
shared/tinyusb/mp_usbh.c) for stack init. Disables TinyUSB CDC/MSC/HID
class drivers via `CFG_TUH_*=0` so they don't claim DUT interfaces.
Forwards URBs via `tuh_edpt_xfer` (raw EP transfer API) and
`tuh_control_xfer`. Spawns a single pump task running
`tuh_task_ext` on APP_CPU.

## What works on r24-wip

Validated on hardware (ESP32-S3 dev board, Pico CDC plugged in):

- Build clean, boot clean.
- TinyUSB enumeration: device descriptor + config descriptor + per-EP
  open via `tuh_descriptor_get_*_sync` and `tuh_edpt_open`.
- USB/IP attach succeeds; kernel cdc-acm claims the device; ttyACM
  appears; mpremote reaches it.
- **Single-call mpremote: 30/30 pass.** PPPPPPPPPPPPPPPPPPPPPPPPPPPPPP.
  This validates the cancel-storm handling: each mpremote close
  triggers cdc-acm to UNLINK 16 URBs; we abort each, close+open the
  EP, send CLEAR_FEATURE(ENDPOINT_HALT), synthesise -ECONNRESET
  completion, and the next mpremote attach starts cleanly.

## What does NOT work on r24-wip

- **`mpremote fs cp` hangs** the kernel-side mpremote process in
  uninterruptible D-state on `usb_poison_urb`. cdc-acm cancels a URB
  via UNLINK, our firmware accepts the UNLINK but never sends a
  giveback (no RET_SUBMIT, no RET_UNLINK), kernel waits forever.
  Power-cycling the S3 does not release it. `vhci_hcd` module unload
  may also hang. **Only a host reboot fully clears.**
- **`cdc_throughput.py read_test` hangs** at the first iteration with
  the same root cause — multi-step protocols within a single TTY
  session break.

The cancel-storm path (single-call workflow) is fixed. Multi-step
within-session is not.

## Diagnostic data captured

From verbose UART instrumentation during a 2 KiB `fs cp` attempt:

```
natural_won (TinyUSB callback fires for bulk URBs): 65 wins, then stops
cancel_ep invocations: 3 total (2 EP0, 1 EP 0x81 interrupt)
cancel_ep on bulk-IN ep 0x82: 0
tuh_edpt_xfer rejected: 0
ep_reset (close+open): 0
synth_won (we synthesised completion): 1 (EP0)
```

Interpretation: the close-storm cancel handling is not involved during
fs cp. 65 URBs flow through naturally, then one stops completing. The
firmware appears healthy (no panic, TCP still up); something in normal
flow URB delivery is broken on the multi-step pattern. We didn't pin
down which URB or why before stopping.

## Seven non-obvious TinyUSB-on-DWC2 gotchas

Documented as code comments and structural decisions in r24-wip; useful
for any future TinyUSB host work:

1. **`tuh_edpt_xfer` requires `CFG_TUH_API_EDPT_XFER=1`** to fire user
   `complete_cb`s at all. Default is 0 and silently drops them.
   `usbh.c:1037` only stores the callback when this define is set.

2. **`tuh_edpt_xfer` allows only one transfer in flight per (dev, ep)**
   simultaneously. `usbh_edpt_claim` fails on a busy endpoint;
   subsequent submits return false. Pipeline depth at the lane layer
   must be 1 if not using class drivers (which manage this internally).

3. **`tuh_xfer_t.setup` and `.buflen` are in a UNION** (`usbh.h:64-67`).
   Setting both for a control transfer overwrites the `setup` pointer
   with the integer cast as a pointer. Result: `tuh_control_xfer:744`
   dereferences address `(uint32_t)xfer_payload` (e.g., 64) → NULL+0x40
   LoadProhibited fault.

4. **`tuh_edpt_abort_xfer` does NOT reliably fire the user
   `complete_cb`** for bulk EPs. The DWC2 channel-disable interrupt
   fires `usbh_control_xfer_cb` for EP0 but not the generic
   `ep_callback` path for non-EP0. User code must synthesise the
   completion explicitly, with an atomic CAS to deconflict any
   late-firing TinyUSB callback.

5. **`tuh_edpt_abort_xfer` leaves the DWC2 channel half-allocated**.
   Subsequent `tuh_edpt_xfer` calls on the same EP fail (`hcd_edpt_xfer`
   rejects the channel allocation) until the EP is fully
   `tuh_edpt_close` + `tuh_edpt_open` cycled.

6. **Even after `close + open`, the device-side data toggle is not
   reset.** `close + open` resets the host-side `next_pid` to DATA0,
   but the device keeps its own toggle. After mid-transfer cancel,
   the two sides disagree on the next expected PID; alternating IN
   transactions get rejected as stale, producing exactly a 50%
   intermittent failure rate.
   
   The fix is `CLEAR_FEATURE(ENDPOINT_HALT)` via `tuh_control_xfer` to
   force device-side toggle reset to DATA0. This is what the IDF
   backend's halt+flush+clear sequence was doing under the hood
   (clear → CLEAR_FEATURE).

7. **Holding any user-level mutex around `tuh_edpt_abort_xfer`
   deadlocks** against TinyUSB's internal `_usbh_mutex` if a
   class-callback is concurrently active on the pump task. We hit this
   trying to serialise abort against submit; symptom was a multi-second
   hang inside `tuh_edpt_abort_xfer`. Fix: don't hold any mutex around
   abort. The mutex is needed for close+open (the reopen window is a
   real race against new submits) but NOT around abort itself.

## Architectural decision the migration didn't anticipate

The user's `andrewleech/micropython#7` PR's `machine.USBHost` (which
runs successfully on directly connected devices) goes through TinyUSB's
**class drivers** (CDC/MSC/HID), not raw `tuh_edpt_xfer`. R24 disables
those class drivers because USB/IP forwarding is URB-level (not class-
level), but in doing so it lands on the experimental `tuh_edpt_xfer +
CFG_TUH_API_EDPT_XFER=1` path that nothing else exercises.

If a future TinyUSB pivot is attempted, two options exist:

- **Option A (R24's approach, broken)**: raw URB forwarding via
  `tuh_edpt_xfer`. Required if usbip protocol semantics are kept.
  Hits all seven gotchas above.

- **Option B (alternative)**: forward at the class-driver level. CDC
  class driver delivers data bytes to us; we wrap them in
  RET_SUBMIT-like packets to the kernel. This loses USB/IP fidelity
  (kernel sees a CDC stream, not raw URBs) but uses the well-tested
  TinyUSB API. Major rewrite of `usbip_server.c` URB dispatch.

R24 implicitly chose A. If retrying, B might be worth scoping.

## Why this attempt was triggered (the questionable rationale)

R23 corrected findings (`r23-findings.md` §"Correction") concluded the
IDF host stack on DWC2 ESP32-S3 was rate-limiting bulk URB processing
at ~88 URBs/sec/pipe. By Little's Law on intake_count=16, this implies
182 ms per-URB residence time vs ~110 µs wire time. We INFERRED the
slowness was in the IDF host stack and migrated to TinyUSB to escape it.

**What we did not do**: instrument the IDF submit-to-callback path
with microsecond-resolution timing. R21's instrumentation used
`xTaskGetTickCount()` which at `CONFIG_FREERTOS_HZ=100` returns 10ms
ticks — too coarse to actually localise per-URB latency.

So the migration was triggered by an unverified hypothesis. The next
investigation cycle should instrument R23 IDF directly with
`esp_timer_get_time()` (microsecond) before committing to any further
architectural changes.

## Files of interest on this branch (vs main)

- `src/c_modules/usbhost/usbhost.c` — entire backend rewrite (1487
  lines vs main's 1078). Heavy comments document the seven gotchas.
- `src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.cmake` — adds
  `CFG_TUH_CDC=0 CFG_TUH_MSC=0 CFG_TUH_HID=0` and
  `CFG_TUH_API_EDPT_XFER=1` to MICROPY_DEF_BOARD.
- `src/c_modules/usbhost/micropython.cmake` — link/include adjustments.
- `test/integration/phase3/r24-bug-findings.md` (committed to main)
  — earlier write-up of the bug investigation; this file supersedes
  with full context.

## To resume this work

1. Power-cycle the host (vhci_hcd D-state from prior failed fs cp may
   linger).
2. `git checkout r24-wip` and rebuild.
3. Pick a hypothesis from "what's left to debug" below and instrument.
4. The seven gotchas above are confirmed; don't re-derive them.

## What's left to debug (if resumed)

The fs cp hang is in normal-flow URB delivery, not the cancel path.
Three plausible candidates:

- **A**: A bulk-OUT URB hangs at some point during the script-write
  loop. Possibly the kernel submits bulk-OUT, our firmware accepts,
  but never returns RET_SUBMIT.
- **B**: Bulk-IN data corruption mid-stream. cdc-acm receives the URB
  data but discards because it doesn't match expected raw-REPL framing.
- **C**: A control xfer (raw-REPL paste-mode includes Ctrl-D bytes
  inline) races with bulk transfers on the same connection.

Each is one debug cycle to confirm with verbose URB tracing gated by
EP address and direction. **Fix likely requires either a watchdog/
timeout that ensures every URB always returns SOMETHING (even if
late), or a switch from option A to option B (class-driver forwarding).**

The kernel D-state hazard is the bigger blocker than the throughput
investigation: a USB/IP server that can lock the host kernel state on
a failed transfer is unsafe to ship at any throughput.
