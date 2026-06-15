# Draft PR: rp2040: Implement host endpoint abort and close.

Draft description for an upstream PR to hathach/tinyusb. Touches `src/portable/raspberrypi/rp2040/hcd_rp2040.c` (shared by the rp2040 and rp2350 host controllers). Two commits, abort and close. Written to the MicroPython PR template since that's the review workflow we use.

---

### Summary

This extends the rp2040/rp2350 host driver to implement two HCD endpoint methods that were left as TODOs when host support landed, `hcd_edpt_abort_xfer` and `hcd_edpt_close`. I came at them building raw bulk endpoint forwarding over the rp2 host, but they're on the normal host paths too, `tuh_edpt_close` aborts then closes an endpoint and usbh aborts every endpoint on a SET_CONFIGURATION, so filling them in rounds out the existing host support.

The abort runs the EP_ABORT / EP_ABORT_DONE handshake so the controller stops accessing an interrupt endpoint before we clear its buffer, and leaves the endpoint configured so the next submit re-arms cleanly. Without that the buffer stays armed while the stack thinks the endpoint is idle, so the next transfer arms an already-available buffer and trips `panic("ep %02X was already available")`. Control (EPX) just clears its buffer and resets. The USB interrupt is masked across the teardown since `hw_endpoint_lock_update` is a no-op on this port.

The close deconfigures the endpoint the same way `hcd_device_close` already does per endpoint, which frees the `ep_pool` slot for reuse. Without it `tuh_edpt_close` aborts the transfer but leaves the endpoint allocated, so a later `tuh_edpt_open` of the same endpoint takes a fresh slot and works through the 15 interrupt slots.

Both land on rp2350 as well since it shares the driver.

### Testing

Tested on RP2350 (Pico 2 W) acting as a USB host against a full-speed CDC device, driving raw bulk endpoint transfers with `CFG_TUH_API_EDPT_XFER=1`. Before this, aborting a pending bulk-IN read and resubmitting on the same endpoint panicked, confirmed over SWD with the endpoint still `active` and its buffer armed. After, repeated cancel/resubmit cycles run clean with no panic and no duplicate or stuck endpoint-pool entries.

EP_ABORT and EP_ABORT_DONE are tagged "Device only" in the datasheet, so I instrumented the spin to check the handshake actually works host-side: across 48 aborts from a clean boot, EP_ABORT_DONE asserted within 0 spin iterations every time, so it does work in host mode for these interrupt endpoints. Haven't tested on an actual rp2040 yet though, the abort register path is identical on both.

### Trade-offs and Alternatives

The abort waits on `abort_done` before clearing the buffer, the same handshake the device side uses in `rp2040_usb.c`. It runs at task level, not in the ISR, and done asserts within a few cycles for these endpoints (measured, see Testing), so the spin's fine. The alternative that avoids the "Device only" register entirely is to disable the endpoint's `int_ep_ctrl` polling and then clear the buffer (what `hcd_device_close` does), but given the handshake measurably works and gives a clean "controller is idle" signal, I went with EP_ABORT.

### Generative AI

I used generative AI tools when creating this PR, but a human has checked the code and is responsible for the code and the description above.

---

## Review round 1: response and verified fixes (pending hardware re-validation)

PR #3702 status: OPEN, `mergeable: CONFLICTING` (master moved; needs a rebase),
`REVIEW_REQUIRED`. The maintainer ran `@claude review`; a Copilot review also ran.
CI is green except one unrelated `build-cmake-stm32h7 ...-arm-clang` job - this PR
changes only `hcd_rp2040.c`, which that port does not compile, so it is not ours
(expected to clear on a rebase onto current master).

Each finding was cross-reviewed against the code, the device-side `dcd_rp2040.c`
reference, and the RP2040/RP2350 register access types before deciding.

| # | Finding | Verdict | Note |
|---|---|---|---|
| 1 | Critical: EPX abort re-enables the IRQ without clearing latched `buf_status`/`sie_status` | Adopt (modified) | `buf_status` bit 0 and a TRANS_COMPLETE for an in-flight SETUP genuinely panic on `assert(ep->active)`; STALL_REC does **not** panic (no assert on that path) - clear it anyway to avoid a spurious stale completion, with corrected rationale |
| 2 | Moderate: clear `abort_done` via the CLR alias | Adopt | `abort_done` is write-1-to-clear; the CLR alias writes only the masked bit through the bus fabric (not a CPU read-modify-write), proven correct by `dcd_rp2040.c:172`. The earlier worry that it would invert was unfounded |
| 3 | Moderate: unbounded spin with the IRQ masked | Adopt bounded spin; **reject** the chip-rev guard | Bound the wait and fall back to disabling `int_ep_ctrl`. Do **not** port the `rp2040_chip_version()>=2` guard: it is not a timeout, is a no-op on RP2350 (hardcoded to 2), and on RP2040 B0/B1 would skip the abort entirely and reintroduce the buffer-clear race |
| 4 | Minor: assert idle in `hcd_edpt_close` | Adopt (modified) | Use plain `assert(!ep->active)` (matches the file idiom, compiles out under NDEBUG), **not** `TU_ASSERT` - which does an unconditional `ebreak` on the RP2350 Hazard3/RISC-V cores and changes release builds to a silent `return false` |
| 5 | Nit: masking asymmetry vs `hcd_device_close` | Adopt by adding masking | The bot's justification ("`hcd_device_close` is post-disconnect-only") is false - it is also called mid-enumeration (closing dev0) and on unplug with a transfer in flight - so add the `hcd_int_disable/enable` masking to `hcd_device_close`, do not just comment |

### Verified code

`hcd_edpt_abort_xfer` (EPX latched-status clear + CLR alias + bounded spin with fallback):

```c
  if ( ep == &epx )
  {
    // Control endpoint is driven directly by the SIE, not the EP_ABORT register.
    // Drop any armed buffer and reset our view; leave the SIE_CTRL SOF /
    // keep-alive base untouched.
    *ep->buffer_control = 0;
    hw_endpoint_reset_transfer(ep);
    // Drop any completion the SIE already latched for this control transfer so the
    // ISR does not run after we re-enable it: a pending EPX buf_status bit would
    // hit _handle_buff_status_bit -> assert(ep->active); a pending TRANS_COMPLETE
    // for an in-flight SETUP would hit hw_trans_complete -> assert(ep->active); a
    // pending STALL_REC would deliver a spurious stale completion for the aborted
    // transfer. These SIE status bits are write-1-to-clear and are only ever
    // driven by the SIE-driven control (EPX) transfer, so clearing them cannot
    // disturb an interrupt endpoint (those complete via their own buf_status bits).
    usb_hw_clear->buf_status = USB_BUFF_STATUS_EP0_IN_BITS;
    usb_hw_clear->sie_status = USB_SIE_STATUS_TRANS_COMPLETE_BITS | USB_SIE_STATUS_STALL_REC_BITS;
  }
  else
  {
    // Interrupt endpoint (host bulk is implemented using these here). Run the
    // EP_ABORT / EP_ABORT_DONE handshake so the controller stops touching the
    // buffer before we clear it. EP_ABORT/EP_ABORT_DONE are tagged "Device only"
    // in the datasheet but work for host interrupt endpoints on rp2040/rp2350.
    uint32_t const bit = 1ul << ((ep->interrupt_num + 1) * 2 + (ep->rx ? 0 : 1));
    uint32_t const int_ep_bit = 1u << (ep->interrupt_num + 1);
    usb_hw_clear->abort_done = bit;   // EP_ABORT_DONE is write-1-clear: drop any stale done so we wait on a fresh abort
    usb_hw_set->abort = bit;

    // Bound the wait: the USB IRQ is masked here, so a stalled or disconnected
    // endpoint that never asserts done must not hang. Normal completion is a few
    // cycles; this is the pathological-case escape.
    uint32_t timeout = 100000;
    while ( !(usb_hw->abort_done & bit) && timeout ) { timeout--; }

    if ( timeout == 0 )
    {
      // Abort never acknowledged. Stop the controller polling this endpoint (as
      // hcd_device_close does) before touching the buffer, then re-enable so the
      // endpoint stays usable. int_ep_addr_ctrl and ep->configured are left
      // intact: this is an abort, not a close.
      usb_hw_clear->int_ep_ctrl = int_ep_bit;
      *ep->buffer_control = 0;
      usb_hw_clear->buf_status = bit;
      usb_hw_clear->abort_done = bit;
      usb_hw_clear->abort = bit;
      hw_endpoint_reset_transfer(ep);
      usb_hw_set->int_ep_ctrl = int_ep_bit;
    }
    else
    {
      // Controller is idle on this endpoint: clear the armed buffer, drop any
      // completion latched for it, clear done for the next abort, release abort.
      *ep->buffer_control = 0;
      usb_hw_clear->buf_status = bit;
      usb_hw_clear->abort_done = bit;
      usb_hw_clear->abort = bit;
      hw_endpoint_reset_transfer(ep);
    }
  }
```

`hcd_edpt_close` (add after the `ep == NULL || ep == &epx` guard):

```c
  // The caller (tuh_edpt_close) aborts any pending transfer first, so the
  // endpoint must be idle here.
  assert(!ep->active);
```

`hcd_device_close` (mask the teardown, mirroring `hcd_edpt_close`): add after
`(void) rhport;`

```c
  // Mask the USB IRQ across teardown: hw_endpoint_lock_update is a no-op on this
  // port, so without this the buff_status ISR could run hw_endpoint_xfer_continue
  // on an endpoint we are concurrently clearing (a transfer still active at
  // unplug, or dev0 closed mid-enumeration). Matches hcd_edpt_close.
  hcd_int_disable(rhport);
```

and `hcd_int_enable(rhport);` immediately before the function's closing brace.

### Plan (gated on the pod power-cycle)

1. Rebase `andrewleech:rp2-host-edpt-abort` onto current `master` (clears the
   `CONFLICTING` state and the stray stm32h7 job).
2. Apply the verified code above.
3. Build the pod firmware with the carried tinyusb and re-run the hardware tests
   on the recovered RP2350 pod: repeated cancel/resubmit on a bulk-IN (the
   original panic), plus an EPX/control abort to exercise the new latched-status
   clear.
4. Push and reply to the review.

Steps 3-4 need the bricked pod back (see `plan/resume-after-power-cycle.md`).
Nothing is pushed to the public PR without maintainer sign-off.
