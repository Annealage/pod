# R27 Phase 2 follow-ups: cdc-acm cleanup wedge + slave-mode upstream PR

Two follow-ups carried over from Phase 1 closure
(`r27-dma-fix-findings.md`, "Open follow-ups"): the host-kernel
`usb_poison_urb` D-state on cdc-acm tty close, and the upstream
slave-mode FIFO/NAK race PR text.

## Status (2026-05-06 overnight session)

| Follow-up | Status | Verification |
|---|---|---|
| (1) cdc-acm cleanup wedge | Patch landed on `r27-tinyusb-migration` (commit `f5e8f8f`). Build clean. | Bench verification deferred: host kernel was already wedged at session start and remains wedged on `usb_poison_urb` from a Phase 1 test python3 process (PID 51060, in D-state). New mpremote/mpy-dev/usbip detach attempts also wedge. The hard rule from the dispatch was DO NOT use force-reboot; user must reboot before verification. |
| (2) slave-mode upstream PR text | Refined in `r27-upstream-pr-draft.md`. Slave-mode FIFO-recheck is now PR 2 (commit `a8b5bf4e7` in `lib/tinyusb`). Alignment fix moved to a deferred PR 3 section. | Reviewable as code+text; no bench run required (the patch is a backport of upstream PR #3632, already widely tested). |

## Follow-up 1 fix details

### Symptom recap

`acm_port_shutdown` (kernel `cdc_acm.c`) calls `usb_kill_urb` then
`usb_poison_urb` on each of its URBs. `usb_poison_urb` blocks on
`wait_event(use_count == 0)` which only resolves when the URB is
given back via `usb_hcd_giveback_urb`. dmesg evidence
(`/sys/...vhci_hcd.0` and `dmesg | grep usb_poison_urb`):

```
[ 3687.310]  usb_poison_urb+0xb2/0xf0
             acm_port_shutdown+0x78/0x1a0 [cdc_acm]
             acm_tty_close+0x22/0x30 [cdc_acm]
             tty_port_shutdown+0x64/0xa0
             tty_port_close+0x3b/0xa0
```

Process `python3:51060` was blocked > 614 s by the time of session
start; subsequent `mpy-dev list` and `usbip detach -p 0` also got
stuck (see `ps -ef` of the wedged PIDs). The kernel-side URB never
got given back.

### Root cause

`usbhost_cancel_ep` ran the post-abort EP recovery
(`tuh_edpt_close` + `tuh_edpt_open` + `tuh_control_xfer
CLEAR_FEATURE`) BEFORE delivering the synthesised user-side
completion. `tuh_control_xfer` with `complete_cb=NULL` is a polled
blocking wait without a built-in timeout (upstream
`lib/tinyusb/src/host/usbh.c:773` carries
`// TODO probably some timeout to prevent hanged`). On a Pico CDC
device that does not reply to `CLEAR_FEATURE(ENDPOINT_HALT)` on a
non-bulk EP (interrupt-IN at 0x81 in this case), the synchronous
control xfer never completed and `usbhost_cancel_ep` blocked
indefinitely.

`usbhost_cancel_ep` is invoked synchronously from the usbip
server's UNLINK read loop
(`usbip_server.c handle_urb_stream` line 1552). Blocking there
prevented the read loop from reaching either:

* `xSemaphoreTake(u->cancel_done_sem, 250 ms)` and the subsequent
  `tx_ret_unlink` path; or
* the synthesised RET_SUBMIT(-ECONNRESET) path that `user_cb`
  would have triggered via `lane_completion_cb`.

The kernel's `usb_poison_urb` thus waited forever for a giveback
that no path could deliver.

### Patch shape

`src/c_modules/usbhost/usbhost.c` `usbhost_cancel_ep`:

1. Reorder so the user-side completion delivery (sync `done_sem`
   give or async `user_cb` invocation) fires BEFORE the
   close+open+CLEAR_FEATURE block.
2. Capture `ep_xfer_type` at the same time as the `inflight`
   pointer claim (under `state_mutex`).
3. Skip CLEAR_FEATURE for non-bulk EPs (interrupt and
   isochronous). Their toggle/STALL semantics do not benefit
   from the control xfer, and devices commonly NAK the request.
   Bulk EPs still get CLEAR_FEATURE because the toggle-resync
   matters for subsequent bulk URBs on the same EP.

`usbhost_watchdog_recover` mirrors the same ordering and the same
non-bulk skip. The watchdog runs on its own task so a wedge there
is less catastrophic for kernel-side URB lifecycle, but the change
is symmetric and consistent.

### Why this should resolve the wedge

After the patch, the kernel's URB is given back as soon as the
synth completion is delivered (via responder's RET_SUBMIT for
async or sync caller's done_sem wake for sync). The recovery
runs after the kernel-side URB is already given back, so even if
`tuh_control_xfer` blocks forever, the kernel-side
`usb_poison_urb` will see `use_count == 0` and unblock. Subsequent
submissions on the same EP are correctly serialised behind
`ep_submit_mutex` and will block only if the recovery takes time;
they do not block the kernel-side cancellation.

### Verification plan (for user, post-reboot)

After rebooting the host (clearing the wedged python3 PID
51060 and the wedged usbip detach):

1. Flash the new firmware (`src/tools/flash.sh`) onto the
   ESP32-S3.
2. Boot, attach: `sudo usbip attach -r 192.168.0.166 -b 1-1`.
3. Run the cycled 30/30 mpremote at `/tmp/r27-30x30-cycled.sh`
   (or its successor): looking for `PASS=30 FAIL=0` and zero
   `usb_poison_urb` lines in `dmesg` after the run completes.
4. Run a 20/20 fs cp: `for i in {1..20}; do mpremote connect
   /dev/ttyACM<N> resume fs cp /tmp/r27-test4k.bin :test4k.bin
   || break; done`. Same expectation: no `usb_poison_urb` and
   no D-state on the spawned mpremote process.
5. Confirm `watchdog: fires=0` in the UART trace (no wakes).

The dispatch's acceptance criteria for follow-up 1 (30/30, 20/20,
no D-state, watchdog=0) are reachable but require the bench
session the user can run after a reboot.

### What was NOT verified in this session

The host kernel state at session start already had the wedge in
place; `mpy-dev list`, `usbip detach -p 0`, and any cdc-acm
operation hangs immediately. The dispatch's hard rule (DO NOT use
force-reboot) means I did not attempt to clear the wedge from
inside the agent. This is the explicit handoff: the user reboots
the host, then runs the verification plan above against the
flashed firmware tip `f5e8f8f`.

## Follow-up 2 PR text refinement

### Existing state

`lib/tinyusb` branch `r27-fix-txfifo-recheck` carries three
commits on top of upstream `0.20.0`:

| Commit | Subject | PR target |
|---|---|---|
| `a8b5bf4e7` | `hcd/dwc2: Re-read txsts inside per-packet FIFO write loop.` | PR 2 |
| `6b0f49b06` | `hcd/dwc2: Save post-transfer PID in DMA-mode IN handler.` | PR 1 |
| `6fa7cfbb4` | `R27 DEBUG: Add IRQ-time HCINT tracing for DMA-mode IN channels.` | not for upstream |

The two PR commits are topologically independent: PR 2's diff is
in `handle_txfifo_empty`, PR 1's is in `handle_channel_in_dma`.
The user can cherry-pick either order onto a fresh branch off
upstream master.

### What changed in this session

`r27-upstream-pr-draft.md` (commit `f5e8f8f`):

* PR 2 section now describes the slave-mode FIFO-recheck fix
  (the actual `a8b5bf4e7` commit), not the alignment fix the
  earlier draft had under PR 2.
* The alignment fix is moved to an "Out of scope" section at
  the bottom; recommended as a follow-up PR after PR 1+PR 2 land.
* PR 2 body cites upstream issue #3623 and PR #3632, credits
  HiFiPhile (the original PR author), and explains the dwc2
  slave-mode reproducer environment (mpy-pod cdc-acm bulk-OUT
  on ESP32-S3 multi-packet writes).
* PR 1 body unchanged from previous draft (already correct).

### What still needs human review

* Confirm with HiFiPhile (the original #3632 author) that filing
  this re-PR with credit is appropriate, vs. simply commenting
  on #3632 with "tested on ESP32-S3 with this reproducer" and
  pushing for merge. The latter is probably preferable; the PR
  body in `r27-upstream-pr-draft.md` PR 2 section anticipates
  that path with explicit "primarily serves to push that PR
  forward with a second tested reproducer" language.
* Decide whether the alignment fix gets submitted as a
  documentation-only PR (note in `hcd_dwc2.c`) or stays
  caller-side only. Current draft recommends "Option B"
  (defensive bounce buffer) for upstream; that's a more
  invasive change than caller-side alignment and a maintainer
  call.

## Branch state at session end

* Tip: `f5e8f8f` "R27 phase 2: deliver synth completion before EP
  recovery; skip CLEAR_FEATURE on non-bulk EPs."
* Build: clean. Firmware size 1857728 bytes (was 1857584; +144 B
  for the reordered cancel path and ep_xfer_type capture).
* `lib/tinyusb` submodule on `r27-fix-txfifo-recheck` at
  `6fa7cfbb4`; parent submodule pin already bumped to that hash
  in commit `419b773`. No further submodule pin bump needed for
  this session's work (the cancel-ep change is in the parent
  repo's `usbhost.c`).
* Uncommitted submodule WIP: `extmod/machine_usb_host.c`,
  `shared/tinyusb/mp_usbh.{c,h}`, and
  `ports/esp32/tinyusb_port/tusb_config.h`. These are pre-existing
  Phase 0 WIP build-config changes (CFG_TUH_CDC=0 guards around
  CDC pools, DMA-enable on S2/S3, `tuh_mount_hook`/`tuh_umount_hook`
  weak overrides). Not touched in this session; the user's earlier
  workflow has been to keep them WIP rather than commit them in
  the submodule.

## Open residuals

* The host kernel python3 PID 51060 (and downstream wedged
  processes) cannot be cleared without a reboot. The hard rule
  in the dispatch prevented me from running force-reboot.sh.
* Bench verification of the cdc-acm wedge fix is deferred until
  the user runs the verification plan above on the flashed
  firmware after rebooting.
* The DMA-enable WIP in the micropython submodule is required
  for the build (CFG_TUH_DWC2_DMA_ENABLE=1 on S3); committing it
  inside the submodule and bumping the parent pin is a small
  follow-up, but it changes the upstream-submission story for the
  micropython side, so I left it for the user.
