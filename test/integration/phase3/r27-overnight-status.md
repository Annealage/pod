# R27 overnight session status (2026-05-06)

## TL;DR

* Follow-up 1 (cdc-acm cleanup wedge): fix landed and built clean
  on commit `f5e8f8f`. Bench verification deferred; host kernel
  was already in `usb_poison_urb` D-state at session start and
  could not be cleared without a reboot (which the dispatch
  forbade).
* Follow-up 2 (slave-mode upstream PR): PR text refined; PR 2
  now references the correct slave-mode FIFO-recheck commit
  (`a8b5bf4e7`). No code change required in this session beyond
  documentation.
* No mpremote / fs-cp / dmesg verification completed this session.
  All paths to the device-under-test go through cdc-acm, which is
  blocked by the kernel-side wedge from before the session began.

## Last commit

`f5e8f8f` "R27 phase 2: deliver synth completion before EP
recovery; skip CLEAR_FEATURE on non-bulk EPs."

## Verification table

| Item | Status | Notes |
|---|---|---|
| Build (idf.py / make) | PASS | clean; firmware 1857728 B |
| 30/30 mpremote single-call | NOT RUN | bench wedged |
| 20/20 mpremote fs cp | NOT RUN | bench wedged |
| watchdog fires=0 | NOT RUN | bench wedged |
| dmesg `usb_poison_urb` clean | NOT RUN | bench wedged |
| Slave-mode PR text refined | DONE | PR 2 in r27-upstream-pr-draft.md |
| Slave-mode patch in lib/tinyusb | UNCHANGED | already at `a8b5bf4e7` (PR 2) and `6b0f49b06` (PR 1) on branch r27-fix-txfifo-recheck |

## What happened

### Session start state

Branch `r27-tinyusb-migration` at `c6bdf4c`. Phase 1 closed.
Two follow-ups assigned:

1. cdc-acm cleanup wedge investigation + fix.
2. Refine the slave-mode upstream PR text.

### Discovery: host kernel already wedged

`dmesg | grep usb_poison_urb` showed five recent occurrences of
`acm_port_shutdown -> usb_poison_urb` blocked tasks. `ps -ef`
showed `python3:51060` (started at 22:25, before this session)
in D-state on `usb_poison_urb`. `mpy-dev list`, `usbip detach -p
0`, and any cdc-acm read all hang. The wedge predates this
session and is the exact symptom Follow-up 1 is meant to fix.

### Follow-up 1 work

Static analysis of `usbhost_cancel_ep` and the usbip read loop
identified the ordering bug: the synth completion fired AFTER
the close+open+CLEAR_FEATURE recovery block, so a
`tuh_control_xfer` (CLEAR_FEATURE) that the device never
replied to wedged the cancel path. The cancel path is called
synchronously from the usbip read loop, so the read loop's
RET_UNLINK send path is also wedged; the kernel-side
`usb_poison_urb` waits forever for an URB giveback.

Patch: reorder `usbhost_cancel_ep` and `usbhost_watchdog_recover`
so the synthesised user-side completion (sync `done_sem` give or
async `user_cb` invocation) fires BEFORE the close+open+
CLEAR_FEATURE block. Also restrict CLEAR_FEATURE to bulk EPs
(non-bulk EPs do not have the toggle/STALL semantics that
benefit from the control xfer; many devices NAK the request
indefinitely on non-bulk EPs).

Build: clean. Firmware grew by ~144 bytes for the reordered code
plus the per-cancel `ep_xfer_type` capture.

### Follow-up 2 work

`r27-upstream-pr-draft.md` previously had PR 2 = alignment fix.
The dispatch said PR 2 should be the slave-mode FIFO/NAK race
patch (commit `a8b5bf4e7`). The draft text has been rewritten:

* PR 2 now is the slave-mode FIFO-recheck patch.
* PR 2 cites upstream issue #3623 and PR #3632, credits
  HiFiPhile (original PR author), and explains the test
  reproducer.
* The alignment fix moved to a deferred "Out of scope"
  section.
* PR 1 text unchanged from Phase 1 work.

### What I deliberately did NOT do

* Did NOT run `force-reboot.sh` or any equivalent. The dispatch
  was unambiguous on this point.
* Did NOT commit the micropython-submodule WIP changes
  (`tusb_config.h` DMA enable, `mp_usbh.{c,h}` CFG_TUH_*
  guards, `machine_usb_host.c` CFG_TUH_* guards). They are
  load-bearing for the build but the user's previous workflow
  has been to keep them WIP rather than commit them in the
  submodule. Leaving the decision to the user.
* Did NOT attempt to flash the new firmware. The flash path
  goes through the local CH340N USB serial which is independent
  of the wedged cdc-acm, but `mpy-dev` (used by `flash.sh`) is
  itself wedged. Bypassing `mpy-dev` and calling `idf.py flash`
  directly would work, but no point flashing if I cannot then
  verify the result without rebooting first.

## Recommended next steps for user

1. Reboot the host (clears the wedged python3 PID 51060 and
   the wedged usbip detach process; clears the cdc_acm/vhci
   kernel state).
2. `bash src/tools/build.sh` — should be a no-op (already
   built); confirms the build environment.
3. `bash src/tools/flash.sh esp32-s3` — flashes the
   `f5e8f8f` firmware.
4. `sudo usbip attach -r 192.168.0.166 -b 1-1` and run the
   30/30 cycled test (`/tmp/r27-30x30-cycled.sh`). Expected:
   `PASS=30 FAIL=0`, no `usb_poison_urb` in dmesg, watchdog
   fires=0.
5. Run a 20/20 `mpremote fs cp` of a 64-128 KB file (build a
   bigger test file; `dd if=/dev/urandom of=/tmp/r27-test128k.bin
   bs=1024 count=128`). Same expectation.
6. If both pass: commit a verification log under
   `test/integration/phase3/` and close Phase 2.

## Worst-case fallback

If the cancel-ep reorder does NOT clear the wedge, the next
hypothesis to test is that the device-side TinyUSB
`tuh_edpt_close` itself is hanging on a bulk EP whose channel
is half-allocated in DWC2 (gotcha #5; the comment claims
close+open recovers but the dispatch's existing watchdog
trace evidence on Phase 1 showed the recovery path was
sometimes active at wedge time). In that case, swap the
recovery order to "synth user_cb -> abort -> close -> open
without CLEAR_FEATURE, drop CLEAR_FEATURE entirely" and
re-test. The cancel-ep path is then trimmed to "abort + synth
+ EP reset (no control xfer)"; toggle desync between host and
device on a cancelled bulk EP would be tolerated for one URB
(the device's first packet of a re-armed transfer might be
rejected with DATATOGGLE_ERR; the existing slave-mode/DMA-mode
code paths handle that retry).

That fallback is conservative; the current commit
already does the most likely-helpful thing (run synth before
recovery) without dropping CLEAR_FEATURE on bulk EPs.

## Files touched this session

* `src/c_modules/usbhost/usbhost.c` (Follow-up 1 patch)
* `test/integration/phase3/r27-upstream-pr-draft.md` (Follow-up 2 refresh)
* `test/integration/phase3/r27-phase2-followups-findings.md` (new, deliverable)
* `test/integration/phase3/r27-overnight-status.md` (this file)

## Untouched

* `lib/tinyusb` branch `r27-fix-txfifo-recheck` (already in correct shape)
* `src/micropython` submodule WIP (left as-is; user's call)
