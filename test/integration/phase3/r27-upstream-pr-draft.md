# Upstream PR drafts for TinyUSB DWC2 host-mode fixes

Two related fixes against `lib/tinyusb` upstream
(github.com/hathach/tinyusb), uncovered while bringing TinyUSB host
up on ESP32-S3 (DWC2 IP) for the Annealage Pod. They are
independent of each other and can either be filed as one PR each
or combined; the bodies below assume separate PRs.

Reproducer hardware: ESP32-S3-WROOM (DWC2 host mode, DMA path) ->
Pico CDC-ACM device, FS bus, MicroPython on both ends. Boards in
the field include any ESP32-S3-class board acting as a DWC2 host;
the slave-mode path is unaffected by fix #1 and the fix #2
alignment issue is platform-agnostic on DWC2.

---

## PR 1: hcd/dwc2: Save post-transfer PID in DMA-mode IN handler.

### Title

`hcd/dwc2: Save post-transfer PID in DMA-mode IN handler.`

### Branch / commit

* Branch in fork: `r27-fix-txfifo-recheck` (or a clean
  upstream-targeted branch, single commit cherry-picked from
  `6b0f49b06`).
* Diff: see below; ~12 added lines including comment.

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

## PR 2: hcd/dwc2: Align IN-direction DMA destination buffer to 4 bytes.

### Title

`hcd/dwc2: Align IN-direction DMA destination buffer to 4 bytes.`

### Branch / commit

In our fork the alignment fix is at
`micropython/extmod/machine_usb_host.c` plus
`shared/tinyusb/mp_usbh.c` -- it's currently caller-side. Either:
(a) leave it caller-side and require all consumers to pre-align,
documenting that requirement in `tusb_config.h` /
`hcd_dwc2.c` comments; or (b) make the DMA-mode IN handler
defensively align via a bounce buffer when the supplied pointer
isn't 4-byte aligned. (b) is what we'd propose upstream because
it is impossible for arbitrary class drivers (CDC, MSC, HID) to
guarantee 4-byte alignment for every transfer buffer they receive
from their consumers.

### Summary

DWC2 internal DMA on ESP32-S3 silicon writes IN-direction data to
the host buffer in 4-byte words and rounds the destination address
*down* to a 4-aligned boundary, overwriting up to 3 bytes before
the supplied pointer. With an unaligned 75-byte configuration
descriptor target buffer at offset `...b6`, the descriptor data
landed at `...b4`, shifting it 2 bytes left and corrupting the
preceding bytes plus losing the trailing 2 bytes of the
descriptor.

This was first hit on enumeration where `cfg_buf` for the device
configuration descriptor parse landed unaligned and caused the
descriptor to mis-parse (wrong total length, wrong interface
count, wrong endpoint MPS for one EP).

### Reproducer

Plug any FS or HS device with a configuration descriptor whose
length is non-zero-mod-4 into a DWC2 host running TinyUSB in DMA
mode, on a build where the caller's `cfg_buf` happened to land at
a non-4-aligned offset. Symptoms: enumeration parses the wrong
total/intf/EP count, or random byte-level corruption on bulk-IN
URBs whose buffer pointer happens to be unaligned.

### Two possible fixes

**Option A (caller-side, what we landed locally):**
Add `__attribute__((aligned(4)))` to all DWC2-IN-bound buffers
in callers, e.g. `enumerate_device.cfg_buf[512]`. Documented in
`hcd_dwc2.c` as a hard requirement. Forces every class driver to
do the same.

**Option B (defensive in `hcd_dwc2.c`):**
In `handle_channel_in_dma` (or before arming the channel for IN),
detect non-4-aligned `edpt->buffer` and substitute a bounce buffer
that is 4-aligned and at least `edpt->buflen + 3` bytes; on
completion, `memcpy` to the caller's pointer. Cost: one bounce
buffer per channel, plus the memcpy on every IN URB whose buffer
isn't pre-aligned.

We recommend Option B for upstream because it is robust against
caller bugs and matches user expectation that the HCD takes
arbitrary buffer alignment.

### Diff (Option B sketch)

```diff
--- a/src/portable/synopsys/dwc2/hcd_dwc2.c
+++ b/src/portable/synopsys/dwc2/hcd_dwc2.c
@@ ... channel arm path for IN direction
   if (((uintptr_t)edpt->buffer & 3) != 0) {
     // DWC2 internal DMA writes in 4-byte words and rounds destination
     // down to a 4-aligned boundary. Use a bounce buffer to avoid
     // overwriting bytes preceding the caller's pointer.
     xfer->using_bounce = true;
     channel->hcdma = (uintptr_t)xfer->bounce_buf;
   } else {
     xfer->using_bounce = false;
     channel->hcdma = (uintptr_t)edpt->buffer;
   }
@@ ... in completion delivery
   if (xfer->using_bounce) {
     memcpy(edpt->buffer, xfer->bounce_buf, actual_len);
   }
```

(Final shape will need a `bounce_buf` field in `hcd_xfer_t` sized
to the maximum URB the platform supports.)

### Testing

* ESP32-S3-WROOM (DWC2, DMA host mode), Full Speed bus, against
  Pico CDC-ACM and a few keyboards/MSC.
* Pre-fix: enumeration logs `parse_config: total=73 intf=1 eps=2`
  for a device whose true config is `total=75 intf=2 eps=3`.
* Post-fix (Option A applied): correct total, intf, eps, and MPS
  values for all three EPs.
* Option B has not been bench-tested upstream-style; the
  caller-side fix has.

### Generative AI

I used generative AI tools when creating this PR, but a human has
checked the code and is responsible for the code and the
description above.

---

## Suggested filing order

PR 1 first (the toggle-save fix) -- it's a pure correctness
regression that any DWC2-DMA-host user is exposed to and the diff
is trivial. PR 2 (alignment) is a more invasive change with
design decisions to make about Option A vs B; PR 1 should not
block on it.

Once PR 1 lands, the mpy-pod branch can drop our local submodule
patch in favour of a submodule pin bump.
