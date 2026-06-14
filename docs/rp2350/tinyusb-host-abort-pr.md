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
