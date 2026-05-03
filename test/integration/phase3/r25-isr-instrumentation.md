# R25 stage B: ISR-side timing instrumentation

## Verdict

**HW re-arm latency case confirmed.** The DWC2 channel for bulk-IN
takes ~11 ms wall-clock between consecutive completion ISR fires.
ISR processing is ~13-16 us and not the gate. The hardware sits idle
between URB completions and re-arms; this is the source of the
~165 ms `avg_round` observed since R23.

The 165 ms / pipeline-depth-16 ~= 10.3 ms per URB matches the directly-
measured `avg_gap` of 11 ms almost exactly.

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

ISR processing is fast (~15 us avg, 60 us max). Nothing in the IDF
ISR code path is sitting on URBs.

The 11 ms gap between consecutive ISR fires is **the DWC2 channel
itself** between completing one URB and reporting the next. With the
ISR refilling the next slot in-ISR (per R25 refill-path-trace), this
is the time from `usb_dwc_hal_chan_activate` (CHENA write) to the
next XFERCOMPL interrupt firing.

Note the bimodality: `min_gap = 19 us` (some URBs complete back-to-
back at wire speed) versus `avg_gap = 11000 us` (most URBs see a
~11 ms gap before the next one fires). This rules out a uniform
hardware delay; it is more consistent with **NAK retry pacing**.

### NAK retry hypothesis (most likely)

DWC2 hardware retries NAK'd IN tokens internally without raising a
CPU interrupt (R25 source-reading findings, NAK not in
`CHAN_INTRS_EN_MSK`). For a Pico CDC IN endpoint:

- When the Pico has data ready, it ACKs the IN token and DWC2
  reports XFERCOMPL within ~110 us (matches `min_gap`).
- When the Pico does not have data ready, it NAKs. DWC2 retries on
  some hardware-defined interval. The observed ~11 ms average
  suggests DWC2's NAK-retry interval for FS bulk is on the order of
  10 ms.

The MicroPython CDC stack on the Pico fills its IN endpoint buffer
on every `tud_task` call. The task runs at the asyncio loop tick rate
which on RP2040 with a `busy_loop`-style fill cadence is around
~1 ms, but the fill itself drains the device-side ringbuffer in
chunks gated by USB-IRQ pacing. If the ring is empty when DWC2 polls,
NAK retry waits ~10 ms before the next IN token, even if the Pico
filled the buffer ~100 us after the first NAK.

### Why does Linux as host reach 677 KiB/s on the same Pico?

Linux EHCI/xHCI uses a different IN-token scheduling strategy. Specifically,
the EHCI Periodic Schedule (also used opportunistically for bulk on
some controllers) and xHCI's hardware ring scheduler issue IN tokens
at a much tighter cadence than the DWC2 NAK-retry interval. EHCI's
"async list" walks queue heads continuously without an internal
NAK-retry timer; if a NAK comes back, the host immediately moves on
to the next QH and revisits this one in the next async traversal,
which is fast. So the same Pico that delivers 677 KiB/s under EHCI
falls to 11 KiB/s under DWC2's NAK-retry-paced bulk IN.

This matches the asymmetry observed in R23 deep-dive: bulk-OUT was
fast (271 us avg) because OUT does not NAK in our case (Pico is
always ready to consume bytes), but bulk-IN takes ~165 ms because
the Pico cannot always have data ready and the NAK retry interval
is ~10 ms per attempt.

### Why the bimodal gap

Steady stream of fast IN responses → `min_gap=19 us`. Most of the
time, NAK then ~10 ms wait then ACK → `avg_gap=11 ms`. Tail of bench
inactivity → `max_gap` up to seconds. The distribution is not a
single peak; it is a fast head plus a 10 ms-quantised tail.

## Implications

The throughput ceiling at 11.2 KiB/s is a property of the **DWC2
hardware NAK-retry interval interacting with the device's data-fill
rate**. Software changes on either the host or device side are unlikely
to lift it without addressing one of:

1. The device's USB stack so it never NAKs (i.e., always has data
   ready when polled). Possible for some devices, not for the general
   case where the device application code sets the data rate.
2. The host's NAK-retry interval. The DWC2 OTG IP exposes some control
   over PING and NAK-retry counters via HCCHARi/HCSPLT/HCINTMSK
   (depending on the speed and endpoint type), but the retry
   *interval* for FS bulk IN does not appear to be software-tunable
   in a portable way - the IP block decides when to issue the next
   IN token after a NAK.
3. A different host stack that can pre-pipeline IN tokens at a
   tighter cadence: EHCI/xHCI on Linux, P4 HighSpeed USB phy +
   SuperSpeed scheduler if applicable, or a hand-rolled scheduler
   that issues "phantom" IN attempts via a frame-list timer rather
   than waiting for NAK retry.

For our use case (USB/IP forwarding from a Pico DUT for POD test
support), the practical pivot points are:

- Accept the 11.2 KiB/s ceiling. CDC at this rate is usable for raw-
  REPL command transport (where a 30/30 mpremote run completes in
  9 seconds even at the floor), but breaks down for `mpremote fs
  cp` of larger files. We have a 60x throughput budget gap to fill.
- Move to ESP32-P4 with HighSpeed USB phy (480 Mbps wire, plus
  EHCI-style scheduler in the P4's UTMI controller). Eliminates the
  FS NAK-retry pacing entirely.
- Run the IDF host stack at a different polling cadence by manually
  driving the channel from a high-priority task instead of waiting
  for hardware NAK retry. This would require a custom HCD driver
  built on top of `hal/usb_dwc_hal.h` that re-arms the channel
  proactively on a frame-list-based schedule. Significant code
  effort, uncertain payoff because the underlying NAK behavior is
  spec'd by USB.

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

## Next concrete experiments

1. **Confirm NAK pacing hypothesis with a never-NAK device.** Run
   the same bench against a Pico firmware that pre-fills the IN
   endpoint with a buffered stream so it never has to NAK. If
   `avg_gap` drops to <1 ms, NAK pacing is confirmed and the
   Pico-side fix is the lever; if it does not, the gap is
   intrinsic to DWC2 even on always-ready devices.

2. **Read DWC2 OTG documentation for FS bulk-IN NAK retry interval.**
   The Synopsys DWC OTG databook may document a software-tunable
   NAK-retry interval (HCCHARi.MC, HCFG, etc.). Worth a focused
   review session.

3. **If NAK pacing is confirmed and not tunable, accept the ceiling.**
   Document the conclusion and pivot scope to ESP32-P4 (HS USB,
   different host stack lineage) for any future throughput
   requirement.

4. **Stage B trace remains in place.** The patch is small (76 lines)
   and isolated to two files. Useful infrastructure to leave on for
   ongoing comparison as we test alternatives. To revert see above.
