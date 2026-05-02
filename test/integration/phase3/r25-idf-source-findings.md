# R25 IDF source findings: where the 10 ms-per-URB cost lives

## Lead

**Ambiguous from source - needs runtime instrumentation.** The IDF host
stack pipelines bulk URBs end-to-end at the hardware level (NUM_BUFFERS=2
double-buffered DMA, ISR-context re-arm of the next buffer). NAK retries
for IN are entirely hardware-handled (no CPU interrupt). There is no
software frame-pacing for bulk. The source shows a tight pipeline with
no obvious 10 ms-per-URB bottleneck. The 165 ms avg-round must live in
one of two places that source-reading alone cannot disambiguate:

A. Between IDF's HCD ISR firing the per-URB `endpoint_callback` and the
   user task running `_handle_pending_ep` (task wakeup + scheduling
   latency on a heavily-loaded scheduler), or
B. Inside the DWC2 channel itself, throttling IN-token issuance below
   the ~1 token/microframe rate the FS bus would support, possibly via
   the empirically-mandated SCHED_INFO=0xFF on non-periodic channels
   that doesn't actually unblock fast scheduling.

Resolving requires runtime instrumentation: log timestamps inside
`_intr_hdlr_chan` (ISR side) vs `_handle_pending_ep` (task side) to
split the 165 ms between hardware and scheduling.

## File paths

All paths under
`/home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf/`
unless noted.

- `components/usb/hcd_dwc.c` - HCD layer, owns DWC2 channels, double-buffer state.
- `components/usb/usbh.c` - per-EP pipe wrapper, callback fan-out.
- `components/usb/usb_host.c` - public API + client-event-loop dispatch.
- `components/hal/usb_dwc_hal.c` - DWC2 HAL (channel alloc, ISR decode).
- `components/hal/include/hal/usb_dwc_hal.h` - HAL public API + EN mask.
- `components/hal/esp32s3/include/hal/usb_dwc_ll.h` - register accessors,
  QTD struct, interrupt bit names.

## Q1: NAK retry pacing

**Answer: NAK is a hardware-internal retry; no CPU interrupt at all.**

The HCD enables only three channel interrupts:

```c
#define CHAN_INTRS_EN_MSK   (USB_DWC_LL_INTR_CHAN_XFERCOMPL | \
                             USB_DWC_LL_INTR_CHAN_CHHLTD | \
                             USB_DWC_LL_INTR_CHAN_BNAINTR)
```
- `components/hal/usb_dwc_hal.c:74-76`

`USB_DWC_LL_INTR_CHAN_NAK` is bit 4 in `HCINT`
(`components/hal/esp32s3/include/hal/usb_dwc_ll.h:104`) but it is NOT
in the enabled mask. The HAL's channel-interrupt dispatcher
(`components/hal/usb_dwc_hal.c:493-545`, `usb_dwc_hal_chan_decode_intr`)
only checks `CHAN_INTRS_ERROR_MSK`, `CHHLTD`, and `XFERCOMPL`. There
is no NAK case, no NAK counter, no software retry-interval logic.

DWC2 retries NAK'd IN tokens automatically until either:
- the device responds with data (XFERCOMPL fires), or
- excessive NAKs cause `USB_DWC_LL_QTD_STATUS_PKTERR`
  (`components/hal/esp32s3/include/hal/usb_dwc_ll.h:892` -
  "Data transmitted/received with errors (CRC/Timeout/Stuff/False
  EOP/Excessive NAK)"), which surfaces as a channel error.

Conclusion: **NAK retry pacing is not a software scheduling parameter;
DWC2 paces itself.** This is not the source of the 10 ms gap.

## Q2: `_buffer_fill` re-entry on completion

**Answer: re-armed in ISR context, before any task wake.**

The chain on URB completion:

1. DWC2 channel raises XFERCOMPL or CHHLTD interrupt. The HAL decodes
   it as `USB_DWC_HAL_CHAN_EVENT_CPLT`
   (`components/hal/usb_dwc_hal.c:520-528`).

2. `intr_hdlr_main` (`components/usb/hcd_dwc.c:921`,
   tagged with `HCD_ENTER_CRITICAL_ISR()`) loops over channels with
   pending interrupts and calls `_intr_hdlr_chan`
   (`components/usb/hcd_dwc.c:841`, IRAM-resident). For CPLT it does:

   ```c
   _buffer_done(pipe, stop_idx, ...);              // line 856
   if (_buffer_can_exec(pipe) && conn) {
       _buffer_exec(pipe);                          // line 860 - HW re-arm
   }
   _buffer_parse(pipe);                             // line 863 - decode result
   if (_buffer_can_fill(pipe) && conn) {
       _buffer_fill(pipe);                          // line 867 - pull next URB from tailq
   }
   ```

   Both `_buffer_exec` and `_buffer_fill` are `IRAM_ATTR`
   (`components/usb/hcd_dwc.c:2299` and `:2223`). They run in ISR
   context. `_buffer_exec` calls `usb_dwc_hal_chan_activate`
   (`components/hal/usb_dwc_hal.c:395-407`) which writes the next QTD
   list address to the channel and sets `HCCHARn.CHENA`. This is
   the next URB starting on the wire, **fired from the same ISR that
   handled completion of URB N**.

3. After `_intr_hdlr_chan` returns, `intr_hdlr_main` calls
   `pipe->callback` (`components/usb/hcd_dwc.c:936`), which is
   `epN_pipe_callback` (`components/usb/usbh.c:485`), which calls
   `endpoint_callback` (`components/usb/usb_host.c:412`). That:
   - adds the EP to a pending list (idempotent if already pending)
   - calls `_unblock_client` -> `xSemaphoreGiveFromISR(event_sem)`
     (`components/usb/usb_host.c:197`).

4. The user task running `usb_host_client_handle_events`
   (`components/usb/usb_host.c:913`) wakes from
   `xSemaphoreTake(event_sem, ...)` (line 929), calls
   `_handle_pending_ep` (line 758), which iterates the EP's done
   URBs via `usbh_ep_dequeue_urb` and runs each
   `urb->transfer.callback` in task context (line 788).

**Deferral mechanism:** binary semaphore `event_sem` from ISR to task.

**Critical detail for the bottleneck question:** the next URB is
already executing on the wire (step 2's `_buffer_exec` at line 860)
BEFORE the user-callback chain runs. So if buffer N+1 was pre-filled
when URB N completed, the inter-URB gap on the wire is at most the
ISR overhead (~few microseconds) plus a CHENA-to-first-IN-token
hardware latency. The user-callback latency only matters if the
client's lane task has not yet resubmitted URB N+2 to fill buffer 0
again before the channel finishes URB N+1.

For our 16-deep lane queue running flat-out, buffer-N+1-pre-filled
should be the steady state. For that to break, either:

- The pending_urb_tailq runs dry (lane task can't keep up). Our R23
  intake_count instrumentation showed avg_depth=13-15 at the
  application layer, which suggests URBs ARE accumulating in our
  tailq. But that's the application-layer queue, not the HCD's
  pending_urb_tailq. They are different queues; URBs flow:
  app -> usbip lane queue -> usbhost_submit_async ->
  usb_host_transfer_submit -> usbh_ep_enqueue_urb ->
  hcd_urb_enqueue -> pipe->pending_urb_tailq -> _buffer_fill ->
  hardware. We don't have visibility into the HCD's pending tailq
  depth; that gap is unmeasured.

- OR DWC2 itself takes >1 ms between channel halt-on-completion and
  becoming ready to accept the next QTD list. This would be a
  hardware-side limit not visible in the source.

## Q3: frame-level pacing of IN tokens

**Answer: no software frame-pacing for bulk. Hardware-only.**

Periodic frame-list scheduling is wrapped in
`if (ep_char->type == ISOCHRONOUS || ep_char->type == INTR)` at
`components/hal/usb_dwc_hal.c:344`. Bulk and Control are not added
to the frame list and do not get `tokens_per_frame` set there.

There is no reference to `HFNUM` (host frame number register) in any
bulk path:

- `usb_dwc_hal_port_get_cur_frame_num`
  (`components/hal/include/hal/usb_dwc_hal.h:485-488`) is the only
  HFNUM accessor.
- It is used exactly once:
  `components/usb/hcd_dwc.c:2254`, inside
  `case USB_DWC_XFER_TYPE_ISOCHRONOUS` of `_buffer_fill`. Bulk does
  not consult it.

The HAL DOES set `SCHED_INFO=0xFF` on every channel including bulk
(`components/hal/esp32s3/include/hal/usb_dwc_ll.h:765-779`,
`usb_dwc_ll_hctsiz_init`):

```c
hctsiz.xfersize |= 0xFF;
```

with the comment

```
Although the hardware documentation suggests that SCHED_INFO is only
used for periodic channels, empirical evidence shows that omitting
this configuration on non-periodic channels can cause them to freeze.
Therefore, we set this field for all channels to ensure reliable
operation.
```

`SCHED_INFO` in the DWC OTG spec is an 8-bit bitmap of microframes
within a frame in which the channel is allowed to issue tokens.
0xFF = all 8 microframes enabled = "issue as fast as possible".

Conclusion: bulk IN tokens are paced "as fast as the channel can
re-arm" with no software frame-gating.

## Bonus: hardware queue mode

**Already on.** Scatter-Gather DMA mode is enabled at port init:

```c
usb_dwc_ll_hcfg_en_scatt_gatt_dma(hal->dev); // Enable Scatther-Gather DMA mode
```
- `components/hal/usb_dwc_hal.c:261`

DMA mode is enabled at AHB level too:

```c
usb_dwc_ll_gahbcfg_en_dma_mode(hal->dev);
```
- `components/hal/usb_dwc_hal.c:88`

The channel uses a QTD list (per-buffer) at activation:

```c
usb_dwc_ll_hcdma_set_qtd_list_addr(chan_obj->regs, xfer_desc_list, start_idx);
usb_dwc_ll_hctsiz_set_qtd_list_len(chan_obj->regs, desc_list_len);
usb_dwc_ll_hcchar_enable_chan(chan_obj->regs);
```
- `components/hal/usb_dwc_hal.c:403-405`

The QTD's `xfer_size` field is 17 bits
(`components/hal/esp32s3/include/hal/usb_dwc_ll.h:121`), so a single
QTD can describe up to 128 KB. DWC2 will split this into MPS-sized
packets (USB protocol level) without software intervention.

For bulk this is exploited only for the multi-MPS-within-one-URB case:
the bulk fill at `components/usb/hcd_dwc.c:2130-2147`
(`_buffer_fill_bulk`) creates exactly ONE QTD with
`USB_DWC_HAL_XFER_DESC_FLAG_HOC` (Halt-On-Complete) for IN.
For OUT it's also one QTD unless `USB_TRANSFER_FLAG_ZERO_PACK` is set,
which adds a zero-length-packet QTD.

`#define XFER_LIST_LEN_BULK 2` (`components/usb/hcd_dwc.c:60`,
"One descriptor for transfer, one to support an extra zero length
packet"). So the hardware sees at most 2 QTDs per URB execution, but
in practice 1 for bulk-IN.

### What hardware queue mode could potentially do but IDF does not

Each `dma_buffer_block_t` allocates `desc_list_len * sizeof(qtd_t)`
bytes of QTD list (`components/usb/hcd_dwc.c:1631`). For bulk that's
2 * 16 = 32 bytes per buffer. NUM_BUFFERS=2 buffers per pipe, so 64
bytes of QTD list per bulk pipe, holding at most 2 in-flight URBs.

There's no architectural reason DWC2 couldn't process a longer QTD
list (e.g., 16 QTDs end-to-end on one channel, one per URB) in a
single channel activation. The hardware would step through the list
without a CHENA cycle between URBs. This would eliminate the per-URB
ISR re-arm cost. **IDF does not do this** for bulk; each URB gets its
own QTD list with HOC, forcing a halt-and-reactivate per URB.

This would be a non-trivial code change (QTD list management,
stop_idx parsing across URB boundaries, error/cancel semantics on
mid-list URBs) and would touch the lower `hcd_*` API only. Worth
considering for a R26 if the runtime instrumentation in question 2
points at the per-URB ISR re-arm overhead.

## What would resolve this from runtime data

Add `esp_timer_get_time()` timestamps at:

1. ISR completion of URB N (inside `_intr_hdlr_chan` for the CPLT
   case, just before `pipe->callback` is invoked). This is in IDF
   source - we'd need to instrument by patching the IDF source or by
   measuring `endpoint_callback` entry on the user side as a proxy.
2. User callback entry (we already have this:
   `inflight->t_complete = esp_timer_get_time()` in
   `transfer_done_cb`).
3. `usb_host_transfer_submit` enter (we have this:
   `inflight->t_submit_pre`) and exit (`t_submit_post`).

The unmeasured gap between ISR-fires and user-callback-entry is the
`event_sem` wakeup + `_handle_pending_ep` scheduling cost. If that
gap is ~10 ms per URB, the bottleneck is task scheduling. If that
gap is small but `t_complete - t_submit_post` for adjacent URBs
shows ~10 ms inter-URB on the channel, the bottleneck is in DWC2
itself or in the lane task's resubmit latency.

Easier proxy: instrument inside the IDF source path. Patch
`components/usb/hcd_dwc.c` `_intr_hdlr_chan` to log
`esp_timer_get_time()` on each CPLT entry, and compare to the user
callback timestamp. This requires modifying the IDF tree (out of
mpy-pod scope unless we vendor that file).

## Cross-references for the next agent

- Bulk IN flow already hits FS wire speed when the queue is empty
  (R23 deep-dive measured `min_round = 87-110 us` matching the
  ~110 us bulk-IN wire floor).
- The 165 ms average kicks in only at ~16-deep pipeline. So whatever
  is at fault scales with pipeline depth, which is consistent with
  EITHER a per-URB scheduling overhead (15 URBs * 10 ms wake = 150
  ms each), OR a per-URB hardware re-arm cost (15 URBs * ~10 ms
  channel-halt-to-reactivate = 150 ms each).
- The first hypothesis is testable cheaply (priority bump, see plan
  step 3). The second would need to bypass IDF's per-URB HOC pattern
  (multi-URB QTD list, see "What hardware queue mode could
  potentially do" above).
