# R25 stage B: ISR-side timing instrumentation

## Correction (2026-05-03)

This document originally led with "HW re-arm latency case confirmed"
and elaborated a NAK-retry-pacing causal story as if it had been
proven. That overstates what the data actually shows. The measured
finding is just that **`avg_gap` between consecutive bulk-IN
completion ISR fires is ~11 ms while ISR processing is ~14 us**;
the cause is not established. NAK retry pacing is one plausible
hypothesis among several; the never-NAK experiment intended to
test it (`r25-nonak-attempt.md`) crashed before producing clean
data. Linux EHCI hosts achieve 677 KiB/s through the same Pico-class
device on the same FS protocol, which proves the gap is a software
or configuration difference somewhere; it is not a silicon
limitation. The "implications" and "next concrete experiments"
sections below have been rewritten to reflect this.

## Verdict (corrected)

**Measured: ~11 ms wall-clock between consecutive bulk-IN ISR fires
on the DWC2 channel; ISR processing itself is ~14 us.** The 165 ms
`avg_round` observed since R23 is consistent with this gap once
multiplied by the 16-deep pipeline (16 * 10.3 ms ~= 165 ms). The
gap therefore lives between URB N completing on the wire and URB
N+1's XFERCOMPL being raised by the DWC2 IP. **Why it sits at
~11 ms is not yet established.**

## Headline numbers (bulk-IN ch=3 ep=0x82 type=2)

Steady-state windows (n=300 to n=900, before bench bufsize transitions):

| Metric | min | typical avg | max | comment |
|---|---|---|---|---|
| `avg_proc` | 13 us | 14-16 us | 16 us | ISR processing |
| `min_proc` | 1 us | 1-3 us | 3 us | floor: register reads only |
| `max_proc` | 33 us | 33-60 us | 60 us | even worst case is sub-ms |
| **`avg_gap`** | **10906 us** | **~11000 us** | **11159 us** | **DWC2 channel idle between URBs** |
| `min_gap` | 19 us | 19-42 us | 42 us | wire-time floor when channel re-arms instantly |
| `max_gap` | 49 ms | 100-410 ms | 1.6 s | bench bufsize transitions, idle gaps |
| `lost` | 0 | 0 | 0 | ring buffer never overflowed |

Throughput unchanged: 11.2 KiB/s at bufsize=256 (matches every prior
R25 bench).

## Interpretation

ISR processing is fast (~14 us avg, 60 us max). Nothing inside the
IDF ISR sub-routines (`_buffer_done`, `_buffer_exec`, `_buffer_parse`,
`_buffer_fill`) is sitting on URBs at the per-call timescale we
sampled.

The 11 ms gap between consecutive ISR fires is the wall-clock time
between URB N completing on the wire and DWC2 raising URB N+1's
XFERCOMPL interrupt. With the ISR refilling the next slot in-ISR
(per R25 refill-path-trace), this gap is `usb_dwc_hal_chan_activate`
(CHENA write) to next-XFERCOMPL.

The bimodality `min_gap=19 us` vs `avg_gap=11000 us` rules out a
uniform delay floor and instead suggests "channel completes
quickly when the conditions are right, sits idle ~11 ms when they
are not." Several mechanisms could produce that shape; we have
not yet measured which one applies.

### Why the 11 ms ceiling is not silicon-bound

Linux EHCI/OHCI hosts running USB-FS achieve hundreds of KiB/s on
this same class of Pico CDC device. The R25 direct-USB baseline
(`r25-direct-usb-baseline.md`) measured **677 KiB/s** through a
direct USB connection on the same Linux host. The wire is FS,
the device firmware is MicroPython CDC, the framing is identical.
60x more data goes through the same wire-speed link.

So whatever causes the 11 ms gap, it is not a property of FS bulk
IN per se, nor of the USB protocol, nor of the Pico-class device.
It is a property of the IDF host stack or the DWC2 register-level
configuration as set up by IDF, and Linux's host driver does
something different that gets ~60x more throughput. The DWC2
IP block itself (Synopsys USB OTG) is widely deployed in Linux
embedded systems running USB-FS host at full wire speed.

### Hypotheses for the 11 ms gap (NONE measured-confirmed)

In rough order of plausibility from the data we have, but all
unconfirmed:

- **NAK retry pacing.** DWC2 hardware retries NAK'd IN tokens on
  some hardware-defined interval. If the device's IN-endpoint
  buffer is empty at poll time, NAK; channel sits ~10 ms before
  re-issuing. Linux EHCI's async-list traversal would not have
  this same pacing because it walks all queue heads before
  re-visiting this one, naturally yielding shorter retry intervals.
  Test attempted in `r25-nonak-attempt.md`; firmware crashed
  before producing clean data.
- **Frame-aligned bulk scheduling.** Even though bulk does not have
  to be frame-aligned, IDF's hcd_dwc could be configured (via
  `SCHED_INFO=0xFF` set defensively per the IDF source comment)
  to align bulk on microframe boundaries, which on FS gives ~125 us
  granularity but possibly stacks under FIFO pressure to longer
  intervals.
- **AHB FIFO contention with Wi-Fi.** ESP32-S3 shares AHB with
  Wi-Fi DMA. If the USB-DWC block's RX FIFO drain depends on AHB
  bandwidth and Wi-Fi is using it heavily, the channel stalls
  waiting for AHB. Untested. Could explain bimodality.
- **Scatter-gather descriptor-list end policy.** For bulk IDF
  uses a 1-QTD (sometimes 2) list per URB with HOC. After HOC
  the channel halts and must be reactivated. The 11 ms could be
  the channel's halt-to-reactivate hardware interval, not NAK.
  Linux EHCI does not use this pattern.
- **HCFG / HFIR / HPRT register difference.** Some host-config
  bit set differently between IDF and Linux that paces the
  channel.

These are hypotheses for the next investigation. None has been
proven or disproven by measurement so far.

## Implications

We measured an 11 ms gap that accounts for the 11.2 KiB/s
throughput. We have not established what causes the gap.

The gap is **not** a silicon limit: Linux EHCI hosts the same
class of Pico device at 677 KiB/s on the same FS bus. The 60x
slowdown is a software difference somewhere in the IDF host stack
or its DWC2 register configuration.

This investigation is **not concluded**. The next round needs to
identify the actual mechanism (or rule out the listed hypotheses
one at a time) before a fix can be designed.

## Files modified

In our project:

- `src/c_modules/usbhost/usbhost.c`: added `dwc_isr_trace_drainer_task`
  (low-priority drainer reading from the IDF ring buffer via two
  extern symbols, emits per-channel summary every 100 ISR fires).
  Stack 8192 to fit ESP_LOGI scratch + the 64-entry batch array.

In the shared IDF tree at
`/home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf`
(branch state: `fcae3288` v5.5.1 + this patch as a working-tree diff):

```diff
diff --git a/components/usb/CMakeLists.txt b/components/usb/CMakeLists.txt
index 1d8057c8d1..83a81f0ad1 100644
--- a/components/usb/CMakeLists.txt
+++ b/components/usb/CMakeLists.txt
@@ -10,7 +10,7 @@ set(priv_includes)
 # As CONFIG_SOC_USB_OTG_SUPPORTED comes from Kconfig, it is not evaluated yet
 # when components are being registered.
 # Thus, always add the (private) requirements, regardless of Kconfig
-set(priv_requires esp_driver_gpio esp_mm)  # usb_phy driver relies on gpio driver API
+set(priv_requires esp_driver_gpio esp_mm esp_timer)  # usb_phy driver relies on gpio driver API; esp_timer for mpy-pod R25 stage B ISR instrumentation

 # Explicitly add psram component for esp32p4, as the USB-DWC internal DMA can access PSRAM on esp32p4
 if(${target} STREQUAL "esp32p4")
diff --git a/components/usb/hcd_dwc.c b/components/usb/hcd_dwc.c
index dd77d27cd2..6aa76c3448 100644
--- a/components/usb/hcd_dwc.c
+++ b/components/usb/hcd_dwc.c
@@ -27,6 +27,58 @@
 #include "esp_cache.h"
 #include "esp_private/esp_cache_private.h"

+#include "esp_timer.h"   /* mpy-pod R25 stage B: ISR timing instrumentation */
+
+// ============================================================================
+// mpy-pod R25 stage B: ISR-side timing instrumentation
+// ============================================================================
+// Per-ISR-fire ring buffer of (t_entry, t_exit, chan_idx, ep_addr, xfer_type,
+// event). Producer is _intr_hdlr_chan (ISR context). Consumer is a periodic
+// drainer task in our application (mpy-pod src/c_modules/usbhost/usbhost.c)
+// which calls __dbg_dwc_isr_trace_drain() to copy out entries.
+//
+// Single producer (ISR), single consumer (task). head/tail are 32-bit so
+// reads/writes are atomic on Xtensa. No critical section needed; if the ISR
+// laps the task the oldest entries are silently overwritten (with a counter).
+// ============================================================================
+
+#define DBG_DWC_ISR_TRACE_RING_LEN 256u  /* must be power of 2 */
+
+typedef struct {
+    int64_t t_entry;
+    int64_t t_exit;
+    uint8_t chan_idx;
+    uint8_t ep_addr;     /* pipe->ep_char.bEndpointAddress, with dir bit */
+    uint8_t xfer_type;   /* USB_DWC_XFER_TYPE_* (bulk=2, intr=3, ...) */
+    uint8_t event;       /* usb_dwc_hal_chan_event_t */
+} __dbg_dwc_isr_trace_entry_t;
+
+static __dbg_dwc_isr_trace_entry_t s_dbg_dwc_isr_trace_ring[DBG_DWC_ISR_TRACE_RING_LEN];
+static volatile uint32_t s_dbg_dwc_isr_trace_head = 0;  /* written by ISR */
+static volatile uint32_t s_dbg_dwc_isr_trace_tail = 0;  /* written by task */
+static volatile uint32_t s_dbg_dwc_isr_trace_lost = 0;  /* incremented by ISR on overrun */
+
+/* Public symbol: drain up to max_n entries into out_buf, return count. */
+size_t __dbg_dwc_isr_trace_drain(__dbg_dwc_isr_trace_entry_t *out_buf, size_t max_n)
+{
+    size_t copied = 0;
+    while (copied < max_n) {
+        uint32_t tail = s_dbg_dwc_isr_trace_tail;
+        uint32_t head = s_dbg_dwc_isr_trace_head;
+        if (tail == head) {
+            break;
+        }
+        out_buf[copied++] = s_dbg_dwc_isr_trace_ring[tail & (DBG_DWC_ISR_TRACE_RING_LEN - 1u)];
+        s_dbg_dwc_isr_trace_tail = tail + 1u;
+    }
+    return copied;
+}
+
+uint32_t __dbg_dwc_isr_trace_lost_count(void)
+{
+    return s_dbg_dwc_isr_trace_lost;
+}
+
 // ----------------------------------------------------- Macros --------------------------------------------------------

 #define ALIGN_UP(num, align)    ((align) == 0 ? (num) : (((num) + ((align) - 1)) & ~((align) - 1)))
@@ -840,6 +892,9 @@ static hcd_port_event_t _intr_hdlr_hprt(port_t *port, usb_dwc_hal_port_event_t h
  */
 static hcd_pipe_event_t _intr_hdlr_chan(pipe_t *pipe, usb_dwc_hal_chan_t *chan_obj, bool *yield)
 {
+    /* mpy-pod R25 stage B: capture entry timestamp and channel context. */
+    int64_t __dbg_t_entry = esp_timer_get_time();
+
     usb_dwc_hal_chan_event_t chan_event = usb_dwc_hal_chan_decode_intr(chan_obj);
     hcd_pipe_event_t event = HCD_PIPE_EVENT_NONE;

@@ -903,6 +958,27 @@ static hcd_pipe_event_t _intr_hdlr_chan(pipe_t *pipe, usb_dwc_hal_chan_t *chan_o
         abort();
         break;
     }
+
+    /* mpy-pod R25 stage B: capture exit timestamp and write trace entry.
+     * Single-producer (this ISR) into a power-of-2 ring; on overrun we
+     * silently overwrite the oldest entry and bump the lost counter. */
+    int64_t __dbg_t_exit = esp_timer_get_time();
+    uint32_t __dbg_head = s_dbg_dwc_isr_trace_head;
+    uint32_t __dbg_tail = s_dbg_dwc_isr_trace_tail;
+    if ((__dbg_head - __dbg_tail) >= DBG_DWC_ISR_TRACE_RING_LEN) {
+        s_dbg_dwc_isr_trace_lost++;
+        s_dbg_dwc_isr_trace_tail = __dbg_tail + 1u;  /* drop oldest */
+    }
+    __dbg_dwc_isr_trace_entry_t *__dbg_e =
+        &s_dbg_dwc_isr_trace_ring[__dbg_head & (DBG_DWC_ISR_TRACE_RING_LEN - 1u)];
+    __dbg_e->t_entry   = __dbg_t_entry;
+    __dbg_e->t_exit    = __dbg_t_exit;
+    __dbg_e->chan_idx  = (uint8_t)chan_obj->flags.chan_idx;
+    __dbg_e->ep_addr   = pipe->ep_char.bEndpointAddress;
+    __dbg_e->xfer_type = (uint8_t)pipe->ep_char.type;
+    __dbg_e->event     = (uint8_t)chan_event;
+    s_dbg_dwc_isr_trace_head = __dbg_head + 1u;
+
     return event;
 }
```

The IDF working tree is **left in this patched state** per the dispatch.
To revert if needed:
`git -C /home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf checkout fcae3288 -- components/usb/CMakeLists.txt components/usb/hcd_dwc.c`

## Stack overflow on first run (resolved)

Initial drainer task stack was 3072 bytes; ESP_LOGI's printf scratch
plus the 64-entry batch array exceeded that and triggered a
FreeRTOS stack overflow detection mid-bench (after one summary line
emitted). Bumped to 8192 in our `usbhost.c` and re-ran. The ring
buffer's `lost=0` counter on the rerun confirms the drainer kept up
under load with the larger stack.

## Raw bench output reference

UART log: `/tmp/r25-isr-uart.log`
Bench log: `/tmp/r25-isr-bench.log`
Throughput summary (matches every prior R25 bench, no regression):

```
bufsize=256    rate=11511   kib_s=11.2
bufsize=512    rate=11479   kib_s=11.2
bufsize=1024   rate=11435   kib_s=11.2
bufsize=2048   rate=11132   kib_s=10.9
bufsize=4096   rate=9743    kib_s=9.5
bufsize=8192   rate=7638    kib_s=7.5
bufsize=16384  rate=7340    kib_s=7.2
```

## Next concrete experiments (none proves "accept the ceiling")

The investigation is open. Proposed steps in approximate priority
order; the first unconfirmed-hypothesis to rule in or out should
go first:

1. **Compare Linux's EHCI/DWC2 host driver to IDF's `hcd_dwc.c`.**
   Linux is open source. The DWC2 IP is the same family used in
   `drivers/usb/dwc2/` (host mode) on the kernel side. Walking the
   schedule semantics in `dwc2_hsotg_irq` and the URB queue
   management in `dwc2_handle_chan_done` against IDF's
   `_intr_hdlr_chan` and `_buffer_*` functions should expose any
   register-config or scheduler-walking difference. EHCI hosts
   (`drivers/usb/host/ehci-q.c`) are also worth comparing for the
   broader async-list pattern.

2. **Add intermediate timestamps inside the ISR sub-routines.**
   Stage B measured the whole-ISR span. Splitting the timestamp
   between `_buffer_done`, `_buffer_exec` (channel activation),
   `_buffer_parse`, `_buffer_fill` would confirm where time goes
   inside the ISR. Probably negligible (already saw 14 us total)
   but cheap to do.

3. **Add register dumps at gap boundaries.** Snapshot HCCHARn,
   HCINTn, HCTSIZn, HFNUM, HPRT, HFIR, HCFG into the trace ring
   on entry to `_intr_hdlr_chan`. During the 11 ms idle gap the
   channel state should reveal whether the channel is halted-
   waiting-for-reactivate, active-waiting-for-NAK-retry, or
   active-but-suspended. Each register state implies a different
   underlying mechanism.

4. **Try `CONFIG_USB_HOST_HW_BUFFER_BIAS_IN`.** This kconfig knob
   was listed in the original plan (step 2) but never run. Bias
   gives the IN FIFO 600 bytes vs 408 in the balanced default.
   If FIFO pressure is gating the channel, this should help.

5. **Search Espressif issue tracker / IDF git log for FS bulk-IN
   throughput reports.** Filed bugs and fix attempts on this
   exact problem upstream are direct prior art. Possible repository
   keywords: "USB host throughput", "bulk-IN NAK", "FS host
   slow", "DWC2 host bulk".

6. **Read the ESP32-S3 TRM USB-OTG host section** for any host-
   config register where the IDF default may be conservative
   (HCFG.PerSchedEna, HFIR, HPTXSTS, HNPTXSTS).

7. **Compare to TinyUSB-host on the same ESP32-S3.** The
   `andrewleech/micropython#7` `USBHOST` variant uses TinyUSB host
   on the same DWC2 silicon. If TinyUSB also reaches ~11 KiB/s
   bulk-IN, the issue is the silicon-or-the-shared-DWC2-config.
   If TinyUSB reaches Linux-class throughput, the issue is in IDF
   `hcd_dwc.c` specifically and the comparison localises the
   difference.

8. **USB protocol analyzer trace** during the bench, if a hardware
   analyzer is available, to see what is on the wire during the
   11 ms gap. NAKs? SOFs? Idle? This is the most direct way to
   distinguish "device NAKs and host waits" from "host issues no
   token at all".

The stage B ring-buffer infrastructure can be reused for steps 2,
3 without further IDF changes beyond inserting more timestamps.

## Stage B trace status

The patch is small (76 lines) and isolated to two files. Useful
as infrastructure for steps 2 and 3 above. Revert command in the
"Files modified" section.
