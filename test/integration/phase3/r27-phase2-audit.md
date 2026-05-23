# R27 Phase 2 audit: cdc-acm cleanup wedge, post-`f5e8f8f` failure

Bench run on tip `f5e8f8f` reproduced the exact pre-fix wedge:
mpremote PID 7335 stuck in D-state on `usb_poison_urb` for 49 minutes,
identical kernel stack:

```
usb_poison_urb -> acm_port_shutdown -> tty_port_shutdown
              -> tty_port_close -> acm_tty_close -> tty_release
              -> __fput -> fput_close_sync -> __x64_sys_close
```

Recovery: `sudo usbip detach -p 0` cleared the wedge cleanly without
needing reboot. mpremote's close() returned, host fully recovered. The
overnight session's claim that `usbip detach` itself wedged is not
reproducing now; either the prior wedge was deeper (vhci_hcd module
state corrupted) or that report was wrong. The detach path is a
viable iteration recovery route for further bench work.

## What the device-side log showed

UART trace through close (post-`f5e8f8f`):

```
I (120884) usbhost: cb:  seq=39 ep=0x00 result=0 alen=8         <- last successful CTRL xfer (cdc SET_CONTROL_LINE_STATE)
I (120922) usbhost: cancel_ep: dev=1-1 ep=0x81 (pre)
I (120922) usbhost: cancel_ep: dev=1-1 ep=0x81 (post=1)         <- abort_xfer returned ok
I (120951) usbhost: synth: seq=15 ep=0x81 ifl=0x3fcf143c WON busy_pre=0 (calls=1 wins=1)
I (120959) usbhost: ep_reset: ep=0x81 type=3 close=1 open=1 clear_feat=0 skip=1     <- non-bulk skip held
I (120966) usbhost: cancel_ep: dev=1-1 ep=0x82 (pre)
I (120997) usbhost: cancel_ep: dev=1-1 ep=0x82 (post=1)
I (121002) usbhost: synth: seq=38 ep=0x82 ifl=0x3fcf0950 WON busy_pre=0 (calls=2 wins=2)
I (121011) usbhost: sub: seq=40 ep=0x82 dir=IN len=128 ifl=0x3fcf0950 dev=1   <- lane picks the next queued read URB
I (126706) usbhost: watchdog: ticks=1200 fires=0
I (186822) usbhost: watchdog: ticks=1800 fires=0     <- nothing else for the rest of the wedge window
```

So the device-side completion chain DID fire correctly for the URBs
the kernel asked us to UNLINK:

* synth WON for both EPs (busy_pre=0 in each case, no natural completion racing).
* CLEAR_FEATURE skip on non-bulk EP (`clear_feat=0 skip=1`) held as designed.
* Watchdog fires=0 for the entire wedge window.

But after the second `cancel_ep` and the new `sub: seq=40` submit, the
kernel stopped sending CMDs. No further `cancel_ep` events, no further
`sub:` events. Just watchdog ticks.

## What the kernel was doing

cdc-acm `acm_port_shutdown` calls `usb_poison_urb` sequentially on
each tracked URB:

```c
usb_poison_urb(acm->ctrlurb);             // interrupt 0x81
for (i = 0; i < ACM_NR; i++)
    usb_poison_urb(acm->wb[i].urb);       // bulk-OUT 0x02 (write buffers)
for (i = 0; i < acm->rx_buflimit; i++)
    usb_poison_urb(acm->read_urbs[i]);    // bulk-IN 0x82 (read URBs)
```

`usb_poison_urb` internally does `usb_kill_urb` (sync UNLINK then wait)
then sets POISON. So this is NOT a parallel storm; it is a serial
walk, one URB at a time, blocking until each is given back.

Our 2 `cancel_ep` events match the first 2 steps:
1. ctrlurb (0x81): synth, RET_SUBMIT, kernel gives back, poison returns.
2. First bulk-IN read URB (0x82): synth, RET_SUBMIT, kernel gives back, poison returns.

The kernel then proceeds to step 3, the second read URB. We never see
its UNLINK arrive. The kernel is hung in `usb_poison_urb` on this
URB, generating the periodic 120s hung-task warnings.

## Hypotheses for the missing UNLINK

1. **vhci_tx blocked on TCP send.** The device's responder task holds
   `tx_mutex` during `lwip_writev`. If the kernel TCP recv is being
   slow (it shouldn't be - vhci_rx is a kthread) or the kernel TCP
   send buffer back-pressures, the device's writev blocks holding
   tx_mutex. The read loop's `tx_ret_unlink` would also block on
   tx_mutex. Symptom would be no progress on the TCP socket in either
   direction. Plausible but unverified.

2. **DWC2 channel left in bad state by close+open recovery.** The
   first cancel_ep on 0x82 ran `tuh_edpt_close + tuh_edpt_open` per
   the gotcha #5 workaround. Lane then submitted URB seq=40 cleanly
   (`sub:` log entry). But that URB never completes - no `cb:`,
   no `R27/dwc2: post` for it. Could be that `tuh_edpt_open` returned
   success but the channel is still half-allocated in the DWC2 IP
   block. Lane is now blocked forever in `usbhost_bulk_transfer`
   awaiting completion. Kernel-side use_count for URB seq=40
   eventually drops via something (RET_SUBMIT for seq=38 got it back
   already), but the kernel's NEXT poison waits for the URB after
   that, and vhci has it in `priv_rx` so vhci_tx would queue UNLINK.
   That UNLINK would have to be sent over TCP. Same dependency on
   the device's read loop and tx path being live.

3. **Off-by-one in inflight ownership across a retire+resubmit.**
   The `current_inflight[ep]` slot transitions during cancel:
   - Before cancel: slot holds URB seq=38.
   - Synth grabs slot under state_mutex, sets to NULL, fires user_cb.
   - Lane retires, picks URB seq=40 from queue, submits via TinyUSB.
   - `usbhost_submit_xfer` re-populates current_inflight[ep] = URB 40.
   The window between "synth clears slot" and "lane re-populates" is
   non-zero. If a second UNLINK arrives in that window, cancel_ep
   logs `no_current_inflight` and returns without firing synth. We
   would see the log line; we did NOT see it, so this is unlikely
   to be the missing-UNLINK explanation. Worth ruling out
   conclusively with verbose logging.

4. **Kernel only had 2 URBs at close time.** `acm->rx_buflimit` and
   ACM_NR depend on the device's interface descriptor; cdc-acm sets
   them from `ep_in->wMaxPacketSize` reading. If the Pico's CDC
   descriptor reports unusual values, rx_buflimit could be 1. That
   would explain "only 2 cancel_ep events" without invoking any
   bug - close finished fine and the wedge is on something else
   (a write URB queued in `wb[i]` that never completed). Plausible
   but the `usb_poison_urb` stack only fires from `acm_port_shutdown`
   which means kernel is currently inside that function on URB[N];
   it should have moved past the read URBs already if rx_buflimit
   was 1. Verify with ftrace or the rx_buflimit value via debugfs.

## What we need next on the bench

Re-run with these changes, then capture a fresh trace:

* `s_urb_verbose=true` to log every RX_UNLINK / TX_RET_UNLINK /
  TX_RET_SUBMIT / UNLINK_CLAIM. Currently both files have these
  guarded by `if (s_urb_verbose)`; flipping to unconditional in a
  diagnostic build is safe (UART overflow risk is minor).
* Add a `cancel_ep no_current_inflight` count to the synth diagnostic
  line so we can tell "kernel sent UNLINKs we couldn't match" apart
  from "kernel never sent more UNLINKs."
* Capture the kernel-side counterpart: enable usbip dyndbg
  (`echo "module usbip_core +p" > /sys/kernel/debug/dynamic_debug/control`
  and similarly for vhci_hcd) so vhci_tx send / vhci_rx receive
  are logged on the host side. Compare device-side counts vs
  kernel-side counts for UNLINK.

Recovery between iterations: `sudo usbip detach -p 0` clears the wedge
in <1 s without reboot. Each test cycle is bounded by ~30 s flash + 1 s
detach + connect-attach + run.

## Architectural finding: synth path is a TinyUSB workaround

The synth path in `usbhost_cancel_ep` (and its mirror in
`usbhost_watchdog_recover`) exists for one reason: TinyUSB's
`tuh_edpt_abort_xfer` does not reliably fire the natural
`tuh_edpt_xfer_cb` (gotcha #4 in the file's docstring). Without the
synth, every UNLINK leaks an inflight URB on the device and the
kernel-side `cancel_done_sem` always times out at 250 ms.

The synth runs an atomic-CAS race against any natural completion that
TinyUSB might fire. If synth wins, the natural callback is a no-op.
If natural wins, the synth is a no-op. In practice, with `abort_xfer`
not firing the callback, synth always wins. The race is fragile but
correct as long as the CAS holds.

The clean fix is upstream: make `hcd_edpt_abort_xfer` always fire a
completion callback (either with `XFER_RESULT_FAILED` or a new
`XFER_RESULT_ABORTED`). Once that lands, the synth path on the
device side has no purpose; we delete it and rely entirely on the
natural callback from TinyUSB.

This is upstream PR3 (alongside PR1 DMA-mode IN PID save and PR2
slave-mode FIFO recheck).

## Proposed PR3 (TinyUSB abort_xfer always-callback)

Target file: `lib/tinyusb/src/portable/synopsys/dwc2/hcd_dwc2.c`

`hcd_edpt_abort_xfer(rhport, dev_addr, ep_addr)` currently calls
`channel_disable` on the matching channel and sets the channel state
to idle. It does not invoke `hcd_event_xfer_complete` for the
in-flight transfer. Result: the host stack (TinyUSB usbh.c or our
device-side code) never sees a callback for the aborted transfer; if
the aborter wants a completion signal, it has to synthesise one.

Patch shape:

```c
bool hcd_edpt_abort_xfer(uint8_t rhport, uint8_t dev_addr, uint8_t ep_addr)
{
    dwc2_regs_t *dwc2 = DWC2_REG(rhport);
    hcd_endpoint_t *edpt = edpt_find_opened(rhport, dev_addr, ep_addr);
    if (edpt == NULL) return false;

    uint8_t ch_id = ...;  // resolve channel
    dwc2_channel_t *channel = &dwc2->channel[ch_id];
    bool was_active = (channel->hccharx_bm.enable == 1);

    channel_disable(dwc2, channel);
    edpt->next_pid = ...;  // preserve toggle (existing code)

    if (was_active) {
        // Fire completion callback so caller does not need to synthesise.
        // XFER_RESULT_FAILED is an existing enum value with the right
        // semantics: "transfer aborted/failed, do not deliver bytes."
        hcd_event_xfer_complete(rhport, dev_addr, ep_addr,
                                /* xferred_bytes= */ 0,
                                XFER_RESULT_FAILED,
                                /* in_isr= */ false);
    }
    return true;
}
```

Open questions for the upstream PR:
* `hcd_event_xfer_complete` is normally called from the channel ISR
  with `in_isr=true`. Calling it from a sync caller path with
  `in_isr=false` is supported (the function is gated by `in_isr` only
  for queueing into the event queue).
* `XFER_RESULT_FAILED` vs adding a new `XFER_RESULT_ABORTED`. Existing
  callers may treat FAILED as "device failure" and trigger reset
  paths. ABORTED would be cleaner semantically but requires a new
  enum value, which is a wider change. Start with FAILED and let
  upstream review pick.
* Slave-mode (non-DMA) `hcd_edpt_abort_xfer` path also needs the
  callback. The code has slave-mode and DMA-mode branches; both
  branches need the same fix. Inspect both.
* Reproducer: same as PR2 (mpy-pod close-storm test). After PR3
  lands, the device-side synth path can be removed; the cancel
  path collapses to "abort_xfer + EP recovery + (TinyUSB callback
  fires) -> RET_SUBMIT/RET_UNLINK". No CAS, no synth, no win/lost
  counters.

## Synth-removal sketch (post-PR3)

Once `tuh_edpt_abort_xfer` always fires the natural callback, the
device-side cleanup is:

* `usbhost.c usbhost_cancel_ep`:
  * Drop `s_synth_*` counters and the `__atomic_compare_exchange` block.
  * Drop the synth-side `done_sem` give and `user_cb` invocation.
  * Keep the `tuh_edpt_abort_xfer` call.
  * Keep the EP recovery (`tuh_edpt_close + tuh_edpt_open + CLEAR_FEATURE
    on bulk EPs`).
  * The natural callback fires asynchronously; lane_completion_cb
    populates comp_status (-ECONNRESET via the lane's `was_cancelled`
    override in run_inflight, or directly from XFER_RESULT_FAILED in
    `usbhost_xfer_done_cb`).
* `usbhost.c usbhost_watchdog_recover`:
  * Same simplification. The watchdog is now a backstop for genuine
    lost transfers (TinyUSB callback never fires due to a deeper bug);
    expected to almost never fire after PR3.
* `usbhost.h`:
  * Drop the `completed` atomic field on `usbhost_inflight_t`.
* The seven-gotchas docstring at the top of `usbhost.c`:
  * Remove gotcha #4 (`abort_xfer doesn't fire callback`) once PR3
    lands and we bump the lib/tinyusb pin.
  * Update gotcha #5 to note that the natural-completion-only model
    requires close+open AFTER the natural callback fires (not before).

Estimated diff: ~150-200 lines removed from usbhost.c, ~20 lines
added to lib/tinyusb hcd_dwc2.c. Net code reduction.

## Branch / verification plan

After this audit:

1. Draft PR3 in `r27-upstream-pr-draft.md` (this audit's "Proposed PR3"
   section formatted as a PR body).
2. Implement the lib/tinyusb abort_xfer change on a new submodule
   branch `r27-fix-abort-xfer-callback`. Commit the change there.
   Bump the parent submodule pin in a separate parent commit.
3. Verbose-logging diagnostic build of the device side: flip
   `s_urb_verbose=true` in `usbip_server.c`, add an UNLINK_NO_MATCH
   counter to `usbhost_cancel_ep`. Re-run the cdc-acm test on this
   build with `usbip detach` recovery between iterations. Capture
   fresh UART + dmesg trace; compare UNLINK send/receive counts on
   both sides.
4. If the verbose run confirms the kernel never sent UNLINK for
   URB[N+1]: focus on the kernel-side path (vhci_tx wedge, TCP
   back-pressure on RET_SUBMIT). If verbose run shows UNLINK arrived
   but cancel_ep no_current_inflight: focus on the inflight tracking
   gap.
5. Implement the synth-removal sketch on top of PR3 (lib/tinyusb pin
   bumped). Re-run cdc-acm test. Expected: PASS=30 FAIL=0 with
   recovery via natural callback only, no synth wins counter, watchdog
   fires=0.

The user's iteration constraint (shared host, no host crashes) is
satisfied by the `usbip detach` recovery path; each test cycle is
bounded and reversible.
