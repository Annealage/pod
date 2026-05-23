# Upstream PR drafts for TinyUSB DWC2 host-mode fixes

Two related fixes against `lib/tinyusb` upstream
(github.com/hathach/tinyusb), uncovered while bringing TinyUSB host
up on ESP32-S3 (DWC2 IP) for the Annealage Pod. They are
independent of each other and can either be filed as one PR each
or combined; the bodies below assume separate PRs.

Reproducer hardware: ESP32-S3-WROOM (DWC2 host mode) -> Pico CDC-ACM
device, FS bus, MicroPython on both ends. Boards in the field
include any ESP32-S3-class board acting as a DWC2 host. PR 1
applies only to the DMA-mode IN path; PR 2 applies to the slave-
mode (non-DMA) OUT FIFO write loop and is independent of DMA mode.

---

## PR 1: hcd/dwc2: Save post-transfer PID in DMA-mode IN handler.

### Title

`hcd/dwc2: Save post-transfer PID in DMA-mode IN handler.`

### Branch / commit

* Fork branch: `r27-fix-txfifo-recheck` (single commit
  `6b0f49b06`); rebase onto current upstream master before filing.
* Diff: ~12 added lines including comment.

### Summary

The DMA-mode IN handler `handle_channel_in_dma` did not save the
hardware's post-transfer PID into `edpt->next_pid` after
XFER_COMPLETE. The slave-mode handler does this at the matching
branch (`handle_channel_in_slave`, around line 954 in the current
`hcd_dwc2.c`). Because `channel_xfer_start` pre-computes
`edpt->next_pid` from the *requested* packet count, a short-packet
completion that ends the transfer early left `next_pid` projecting
a toggle for a packet count that never transferred. The next URB
on the same endpoint armed the channel with that stale toggle and
DWC2 rejected the device's first packet with HCINT_DATATOGGLE_ERR;
the retry path eventually accepted a *later* packet, dropping the
original one silently.

### Reproducer

ESP32-S3 DWC2 host (DMA mode) talking to a Pico CDC-ACM device
running MicroPython:

```python
import serial, time
s = serial.Serial("/dev/ttyACM<N>", 115200, timeout=2.0)
s.write(b"\x03"); time.sleep(0.3); s.read(s.in_waiting or 0)
s.write(b"\r\x01"); time.sleep(0.4)
banner = s.read(s.in_waiting or 0)   # "raw REPL; CTRL-B to exit"
s.write(b"\x05A\x01"); time.sleep(0.4)
resp = s.read(5)
print(resp)
```

Expected: `b'R\x01\x80\x00\x01'` (raw-paste protocol response:
"R\x01" + 16-bit window=128 + flow-control byte).
Without the fix: `b'\x80\x00\x01'` (the leading 2-byte
`R\x01` packet is dropped silently because of the toggle desync
described above).

### Fix

Mirror the slave-mode behaviour in the DMA-mode handler. Inside
`handle_channel_in_dma`, in the
`if (hcint & (HCINT_XFER_COMPLETE | HCINT_STALL | HCINT_BABBLE_ERR))`
branch, after computing `actual_len` and accumulating
`xfer->xferred_bytes`, save the authoritative PID from the channel
size register:

```c
edpt->next_pid = hctsiz.pid;
```

Slave-mode does the same thing at line 954 of the upstream file
(in `handle_channel_in_slave`), and the DMA-mode `_xfer_complete`
periodic-SOF deferral path at line 825 also saves PID. The
DMA-mode IN completion path was the only branch missing the save.

### Diff

```diff
@@ -1123,6 +1140,18 @@ static bool handle_channel_in_dma(dwc2_regs_t* dwc2, uint8_t ch_id, uint32_t hci
       const uint16_t remain_bytes = (uint16_t) hctsiz.xfer_size;
       const uint16_t remain_packets = hctsiz.packet_count;
       const uint16_t actual_len = edpt->buflen - remain_bytes;
       xfer->xferred_bytes += actual_len;

+      // Save the post-transfer PID from the channel size register so
+      // the next URB on this endpoint starts with the correct data
+      // toggle. The slave-mode handler does this at the matching
+      // XFER_COMPLETE branch; the DMA-mode handler was missing the
+      // save, so on a short-packet completion the toggle pre-computed
+      // in channel_xfer_start (based on the requested packet count)
+      // was stale, causing DATATOGGLE_ERR on the next IN URB. The
+      // hardware either retried (dropping the device's first packet)
+      // or coalesced a duplicate (delivering corrupt bytes). Save the
+      // authoritative post-transfer PID so the next URB matches the
+      // device's toggle.
+      edpt->next_pid = hctsiz.pid;
+
       is_done = true;
```

### Testing

* ESP32-S3-WROOM (DWC2, DMA host mode), Full Speed bus, against
  Pico (RP2040) CDC-ACM.
* Direct-Python reproducer above: pre-fix returns 3 bytes
  `\x80\x00\x01`, post-fix returns 5 bytes `R\x01\x80\x00\x01`.
* IRQ-time HCINT trace correlated: pre-fix had three
  `hcint=0x423` events (XFER_COMPLETE | HALTED | ACK |
  DATATOGGLE_ERR) within the reproducer window; post-fix has
  zero. Trace logs preserved at
  `test/integration/phase3/r27-tinyusb-irq-trace-1.log` (pre-fix)
  and `/tmp/r27-30x30-uart.log` (post-fix) in the mpy-pod repo.
* No regression observed on EP0 control transfers, EP=0x81
  notify-IN, or EP=0x02 bulk-OUT.
* Slave-mode path: not regressed (already had the equivalent
  save).

### Risk

Single-line write to a struct field that is otherwise consumed at
the next URB submission. The same field is already written in
adjacent paths (line 825 SOF deferral, line 595 inside
`channel_xfer_start`'s reset path, and the `_xfer_complete` slave
mirror). The risk surface is bounded.

### Generative AI

I used generative AI tools when creating this PR, but a human has
checked the code and is responsible for the code and the
description above.

---

## PR 2: hcd/dwc2: Re-read txsts inside the per-packet FIFO write loop.

### Title

`hcd/dwc2: Re-read txsts inside per-packet FIFO write loop.`

### Branch / commit

* Fork branch: `r27-fix-txfifo-recheck` (commit `a8b5bf4e7`); this
  commit predates the PID-save commit on the same branch and is
  topologically independent.
* Diff: 7 added lines (including comment), 3 removed.

### Summary

This is a backport of the open upstream PR
[hathach/tinyusb#3632](https://github.com/hathach/tinyusb/pull/3632)
which fixes
[hathach/tinyusb#3623](https://github.com/hathach/tinyusb/issues/3623)
"USB HOST MSC hangs on write10". `handle_txfifo_empty` walks open
OUT channels and writes each pending packet to the non-periodic
TX FIFO. Before this fix the `txsts` register was sampled once
*before* the inner per-packet loop, so after writing one packet the
cached `fifo_available` and `req_queue_available` values were
stale. For a multi-packet bulk-OUT (>= 2 packets) the second write
could be issued without enough FIFO or request-queue space,
leaving the channel in a state where XFER_COMPLETE never fires and
the URB hangs forever.

The fix moves the `txsts` read inside the per-packet loop so each
iteration sees fresh FIFO and request-queue space.

This PR is the same code change as #3632 by HiFiPhile; filing
again here primarily to push it forward to merge with a second
real-world reproducer (ESP32-S3 DWC2 host running MicroPython,
not just an ESP32-P4 host MSC use case as in the original
report). HiFiPhile and any other co-author from #3632 should
remain credited.

### Reproducer (slave-mode build)

Hardware: ESP32-S3-WROOM acting as USB host via DWC2, talking to
a Pico CDC-ACM device running MicroPython. Slave-mode build (no
DMA): set `CFG_TUH_DWC2_DMA_ENABLE=0` in `tusb_config.h` (or omit
the macro; default is 0 on ESP32-S3 in upstream).

Steps:

1. Boot host, attach Pico CDC-ACM via the host's TinyUSB stack
   (raw `tuh_edpt_xfer` API, or via the CDC-host class driver).
2. Send a multi-packet bulk-OUT to the device. The smallest
   reliable trigger is a 2*MPS+1 byte transfer (e.g. 129 bytes
   on a 64-byte MPS bulk-OUT EP).
3. Without the fix, the URB does not complete: `XFER_COMPLETE`
   for the channel never fires, `tuh_edpt_xfer`'s
   `complete_cb` is not called, and the next OUT submission on
   the same EP is silently dropped because the channel still
   thinks the previous one is live.

Concretely, the mpy-pod `mpremote fs cp <local> :remote`
operation (which sends multi-packet bulk-OUT writes during
raw-REPL paste mode) reproduces this on slave-mode builds. With
the fix the same operation completes; without it the operation
hangs and the kernel-side cdc-acm tty enters
`usb_kill_urb`/`usb_poison_urb` waiting for a giveback.

### Fix

```c
// return true if there is still pending data and need more ISR
static bool handle_txfifo_empty(dwc2_regs_t* dwc2, bool is_periodic) {
-  // Use period txsts for both p/np to get request queue space available (1-bit difference, it is small enough)
-  const dwc2_hptxsts_t txsts = {.value = (is_periodic ? dwc2->hptxsts : dwc2->hnptxsts)};
-
   const uint8_t max_channel = dwc2_channel_count(dwc2);
   for (uint8_t ch_id = 0; ch_id < max_channel; ch_id++) {
     ...
     while (xfer->fifo_bytes && remain_bytes > 0) {
       const uint16_t xact_bytes = tu_min16(remain_bytes, hcchar.ep_size);

       // skip if there is not enough space in FIFO and RequestQueue.
-      // Packet's last word written to FIFO will trigger a request queue
+      // Packet's last word written to FIFO will trigger a request queue.
+      // Re-read inside the loop: a previous packet write consumes both
+      // FIFO space and request-queue entries, so the values from before
+      // the loop are stale (fixes hathach/tinyusb#3623).
+      const dwc2_hptxsts_t txsts = {.value = (is_periodic ? dwc2->hptxsts : dwc2->hnptxsts)};
       if ((xact_bytes > (txsts.fifo_available << 2)) || (txsts.req_queue_available == 0)) {
         return true;
       }
```

### Testing

* ESP32-S3-WROOM (DWC2, slave host mode, no DMA), Full Speed bus,
  against Pico (RP2040) CDC-ACM.
* Pre-fix: first multi-packet bulk-OUT URB hangs deterministically.
  `mpremote fs cp /tmp/test4k.bin :test.bin` of a 4 KB random file
  hangs on the first chunk that crosses the MPS boundary (byte 65
  onwards). Watchdog instrumentation in the consumer caught the
  channel as never completing; trace logs preserved at
  `test/integration/phase3/r27-tinyusb-irq-trace-1.log` (the
  pre-fix capture from earlier R27 work shows the wedged
  bulk-OUT pattern).
* Post-fix: `mpremote fs cp` of 4 KB file completes; intermittent
  failures still observable on slave-mode builds because of the
  underlying NAK race that #3623 also flagged (this PR is only
  the FIFO-status freshness fix; the NAK race is independent and
  fully solved by switching to DMA host mode, which the mpy-pod
  build now does). For a slave-mode-only target, this fix changes
  the failure mode from "deterministic hang on first multi-packet
  OUT" to "intermittent hang on heavy multi-packet OUT traffic".
* Bulk-IN, control, and interrupt paths: not touched.

### Note on the NAK race (out of scope here)

Issue #3623 also identified a slave-mode race between FIFO writes
and device NAK processing. That race is *not* fixed by this PR;
this PR only fixes the stale-`txsts`-read aspect. The remaining
NAK race is solved by switching the host to DMA mode (which is
PR 1's environment). For platforms that cannot support DMA host
mode, additional caller-level chunking is required (mpy-pod's
`usbhost.c` carries an example chunked-OUT submitter for this
case). A follow-up upstream PR addressing the NAK race fully
inside `hcd_dwc2.c` is left for future work.

### Risk

Single-register-read move from outside-loop to inside-loop. The
read targets a different MMIO register depending on the
periodic/non-periodic flag (already the case before this change);
the per-iteration cost is two MMIO reads instead of one (two
because the conditional may pick `hptxsts` or `hnptxsts`). This
is amortised across the per-packet write that follows; the cost
is negligible compared to the FIFO-write cost. No new state.

### Generative AI

I used generative AI tools when creating this PR, but a human has
checked the code and is responsible for the code and the
description above.

---

## PR 3: hcd/dwc2: hcd_edpt_abort_xfer should fire xfer_complete callback.

### Problem

`hcd_edpt_abort_xfer` disables the active channel and clears state,
but does not invoke `hcd_event_xfer_complete` for the in-flight
transfer. Callers that need a completion signal after abort have to
synthesise one externally. The TinyUSB host stack has its own
mitigation in `usbh.c`, but downstream callers (third-party host
backends like our usbip server) are forced to track per-EP active
URBs and synthesise `-ECONNRESET` themselves under an atomic-CAS
race against any natural completion that might still fire.

### Symptom

In the mpy-pod ESP32-S3 USB-host-over-IP forwarder, every kernel-side
`usb_kill_urb` triggered by a Linux `cdc-acm` close issues a USB/IP
CMD_UNLINK. The handler calls `tuh_edpt_abort_xfer`. Without a
follow-up callback:

* The lane task waiting on the synchronous `done_sem` for the in-flight
  URB never wakes (no `usbhost_xfer_done_cb` call from TinyUSB).
* The inflight URB struct, in/out buffers, and pipeline slot leak.
* On a busy pipe (cdc-acm typically tracks 16 read URBs + 16 write
  URBs + 1 control URB), one close-storm leaks all of them.
* Subsequent `tuh_edpt_xfer` submits on the same channel are
  rejected by `hcd_edpt_xfer` because the channel is half-allocated;
  recovery requires `tuh_edpt_close` + `tuh_edpt_open`.

The mitigation we currently carry on the mpy-pod device side is a
synth path in `usbhost_cancel_ep`: read `current_inflight[ep]` under
a mutex, atomic-CAS a `completed` flag against any concurrent natural
completion, then deliver `user_cb(-ECONNRESET, 0)`. This adds
~150 lines of brittle race-handling code that exists solely to paper
over the missing TinyUSB callback. With the upstream fix in place,
that code can be deleted entirely.

### Proposed fix

```c
bool hcd_edpt_abort_xfer(uint8_t rhport, uint8_t dev_addr, uint8_t ep_addr)
{
    dwc2_regs_t *dwc2 = DWC2_REG(rhport);
    hcd_endpoint_t *edpt = edpt_find_opened(rhport, dev_addr, ep_addr);
    if (edpt == NULL) return false;

    uint8_t ch_id = ep_to_channel(rhport, dev_addr, ep_addr);
    dwc2_channel_t *channel = &dwc2->channel[ch_id];
    bool was_active = channel->hccharx_bm.enable;

    channel_disable(dwc2, channel);

    /* Existing toggle preservation per PR 1 / PR 2 stays here. */
    edpt->next_pid = channel->hctsiz_bm.pid;

    if (was_active) {
        /* Fire the host-side completion so the caller does not need
         * to synthesise one. XFER_RESULT_FAILED is an existing enum
         * value; semantically the transfer was aborted, not
         * naturally failed. Adding XFER_RESULT_ABORTED is cleaner
         * but a wider change; leave that decision to maintainers. */
        hcd_event_xfer_complete(rhport, dev_addr, ep_addr,
                                /* xferred_bytes= */ 0,
                                XFER_RESULT_FAILED,
                                /* in_isr= */ false);
    }
    return true;
}
```

Both the slave-mode and DMA-mode `hcd_edpt_abort_xfer` paths need the
same change. Inspect both branches; the file currently has separate
implementations for each.

### Test reproducer

mpy-pod `r27-tinyusb-migration` with the synth-path removal patch
(prepared once PR3 lands) is the test bed. Build the firmware with
the device-side synth removed; run the cdc-acm close-storm test:

```bash
sudo modprobe vhci_hcd
sudo usbip attach -r 192.168.0.166 -b 1-1
PICO=/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_<sn>-if00
for i in $(seq 1 30); do
    timeout 15 mpremote connect "$PICO" resume exec "print('iter $i ok')" \
        || sudo usbip detach -p 0
done
```

Without PR3 + synth removal: lane wedges on `done_sem` after the
first UNLINK, watchdog fires, kernel `cdc_acm_close` D-state on
`usb_poison_urb`.

With PR3 + synth removal: every UNLINK delivers a natural callback,
RET_SUBMIT(-ECONNRESET) flows back to the kernel, `usb_poison_urb`
returns, all 30 iterations PASS=30 FAIL=0.

### AI usage

I used generative AI tools when creating this PR, but a human has
checked the code and is responsible for the code and the
description above.

---

## PR 4: host: tuh_control_xfer should honour timeout_ms.

### Problem

`tuh_xfer_t` already has a `timeout_ms` field (placeholder, currently
commented out at `lib/tinyusb/src/host/usbh.h:73`); the function
header at `usbh.c:723` carries the explicit comment:

```c
// TODO timeout_ms is not supported yet
bool tuh_control_xfer (tuh_xfer_t* xfer) {
```

When called with `complete_cb=NULL`, the function blocks in a busy
loop driving `tuh_task()` until the transfer completes:

```c
while (result == XFER_RESULT_INVALID) {
    if (tuh_task_event_ready()) tuh_task();
    // TODO probably some timeout to prevent hanged
}
```

If the device never replies (NAK forever, or refuses the request),
the caller is wedged forever. There is no escape.

### Symptom

In the mpy-pod ESP32-S3 USB-host-over-IP forwarder we previously
issued `CLEAR_FEATURE(ENDPOINT_HALT)` after a bulk-EP cancel to
reset the device-side data toggle. The Pico CDC-ACM device NAKs
this request indefinitely on its bulk-IN EP, which wedges
`tuh_control_xfer` and leaves the DWC2 EP0 channel in a stuck
state. Every subsequent control transfer then also hangs, which
in turn wedges `cdc_acm_close` on the host kernel side in
`usb_poison_urb` D-state. Trace:

```
W (144546) usbhost: watchdog: synth seq=101 ep=0x00 dev=1 age=2092746us (running recovery)
W (158471) usbhost: watchdog: synth seq=170 ep=0x00 dev=1 age=2099948us (running recovery)
W (160675) usbhost: watchdog: synth seq=172 ep=0x00 dev=1 age=2042784us (running recovery)
```

We mitigated locally by removing the CLEAR_FEATURE call (PR 1's
DATATOGGLE_ERR retry covers the toggle desync), but any other
caller of `tuh_control_xfer` that talks to a NAKky device is
exposed to the same hang.

### Proposed fix

Honour `xfer->timeout_ms`. Default to `OSAL_TIMEOUT_WAIT_FOREVER`
(preserves current behaviour for callers that don't set it).

Patch shape in `lib/tinyusb/src/host/usbh.c`:

```c
bool tuh_control_xfer (tuh_xfer_t* xfer) {
    ...

    if (xfer->complete_cb != NULL) {
        TU_ASSERT(usbh_setup_send(daddr, (uint8_t const *) &_usbh_epbuf.request));
    } else {
        volatile xfer_result_t result = XFER_RESULT_INVALID;
        ctrl_info->user_data   = (uintptr_t) &result;
        ctrl_info->complete_cb = _control_blocking_complete_cb;

        TU_ASSERT(usbh_setup_send(daddr, (uint8_t const *) &_usbh_epbuf.request));

        const uint32_t timeout_ms = (xfer->timeout_ms != 0)
                                    ? xfer->timeout_ms
                                    : 0xFFFFFFFFu; /* preserve forever default */
        const uint32_t deadline_ms = tusb_time_millis_api() + timeout_ms;
        const bool wait_forever    = (timeout_ms == 0xFFFFFFFFu);

        while (result == XFER_RESULT_INVALID) {
            if (tuh_task_event_ready()) tuh_task();

            if (!wait_forever &&
                (int32_t)(tusb_time_millis_api() - deadline_ms) >= 0) {
                /* Timeout. Tear down the in-flight control xfer cleanly so
                 * subsequent control xfers on the same daddr work. */
                (void) osal_mutex_lock(_usbh_mutex, OSAL_TIMEOUT_WAIT_FOREVER);
                if (ctrl_info->stage != CONTROL_STAGE_IDLE) {
                    /* abort_xfer EP0 to clear the active channel (HCD-specific). */
                    hcd_edpt_abort_xfer(usbh_get_rhport(daddr), daddr, 0);
                    ctrl_info->stage       = CONTROL_STAGE_IDLE;
                    ctrl_info->complete_cb = NULL;
                    ctrl_info->user_data   = 0;
                }
                (void) osal_mutex_unlock(_usbh_mutex);
                xfer->result     = XFER_RESULT_TIMEOUT;
                xfer->actual_len = 0;
                return false;
            }
        }

        if (xfer->user_data != 0) {
            *((xfer_result_t*) xfer->user_data) = result;
        }
        xfer->result     = result;
        xfer->actual_len = ctrl_info->actual_len;
    }

    return true;
}
```

Also uncomment `uint32_t timeout_ms;` in `tuh_xfer_t` at
`usbh.h:73` and remove the obsolete `// TODO timeout_ms is not
supported yet` comment.

### Test reproducer

Any device that NAKs a control transfer indefinitely. Easiest
reproducer is the Pico CDC-ACM device's response to
`CLEAR_FEATURE(ENDPOINT_HALT)` on its bulk-IN EP after the EP has
been aborted. Test code:

```c
tuh_xfer_t cf = { .daddr = pico_addr, .ep_addr = 0,
                  .setup = &clear_feature_setup,
                  .complete_cb = NULL,    /* synchronous */
                  .timeout_ms = 250 };    /* 250 ms cap */
bool ok = tuh_control_xfer(&cf);
TEST_ASSERT(!ok);
TEST_ASSERT(cf.result == XFER_RESULT_TIMEOUT);
/* Subsequent control xfer on same daddr should still work: */
ok = tuh_control_xfer(&get_descriptor_xfer);
TEST_ASSERT(ok);
```

Without PR 4 the TEST_ASSERT(!ok) line is unreachable - the
function never returns. With PR 4 it returns false after 250 ms
and the EP0 channel is clean for subsequent use.

### Trade-offs

* New enum value: `XFER_RESULT_TIMEOUT` for `xfer_result_t`. Could
  reuse `XFER_RESULT_FAILED` to avoid the enum bump, but timeout
  has distinct semantics (transfer was never ack'd vs transfer
  ack'd a failure status). Maintainer call.
* The `hcd_edpt_abort_xfer` call on EP0 in the timeout path
  depends on PR 3 (abort_xfer always firing the natural callback)
  to be properly testable. Without PR 3 the abort leaves a stuck
  channel anyway.
* Default behaviour unchanged: callers that don't set
  `timeout_ms` get the current "wait forever" semantics.

### AI usage

I used generative AI tools when creating this PR, but a human has
checked the code and is responsible for the code and the
description above.

---

## Suggested filing order

PR 1 first (the toggle-save fix); it is a pure correctness
regression that any DWC2 DMA-host user is exposed to and the diff
is trivial. PR 2 second; it is a backport of an existing upstream
PR (#3632) and primarily serves to push that PR forward with a
second tested reproducer. PR 3 third; it is a host-API correctness
issue that affects any caller of `tuh_edpt_abort_xfer` who expects
a completion signal, but it is not a regression - the API has
behaved this way since the DWC2 host port landed. PR 4 fourth; it
addresses the known TODO on `tuh_control_xfer` timeout_ms support
and protects any caller from an indefinite NAK hang on the device
side.

Once PR 1 lands, the mpy-pod branch can drop our local submodule
patch in favour of a submodule pin bump. Once PR 3 lands, the
mpy-pod device side drops the synth path in `usbhost_cancel_ep`
and `usbhost_watchdog_recover` (~150 lines), the `completed` atomic
field on `usbhost_inflight_t`, and the seven-gotchas docstring
gotcha #4. PR 4 lets us tighten any future call to
`tuh_control_xfer` (CLEAR_FEATURE was already removed locally
before PR 4 was drafted; the timeout would have been a defence
in depth had the call stayed).

---

## Out of scope: 4-byte alignment requirement on DMA IN buffers

This was previously drafted as PR 3 here. The conclusion from R27
DMA-mode work is that it is a *caller* responsibility (specifically
documented in the DWC2 hardware manual: the AHB master rounds the
destination address down to a 4-byte boundary on IN). The fix on
the mpy-pod side is `__attribute__((aligned(4)))` on the
on-stack `cfg_buf` in `usbhost.c enumerate_device`. Filing this
upstream as a doc-only change (note in `hcd_dwc2.c` near the IN
channel arm path) is worthwhile but lower priority than PR 1/PR 2;
deferred to a separate PR once the two correctness fixes here have
landed.
