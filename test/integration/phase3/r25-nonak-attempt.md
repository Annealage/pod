# R25 NAK-retry hypothesis test: ESP32 firmware crashed under continuous load

## Correction (2026-05-03)

Suggestion 4 of the original "Suggested next steps" section
proposed "accept the stage B verdict as final ... throughput
ceiling is intrinsic to FS bulk-IN under DWC2 on S3, and any
future throughput requirement needs a board change". That
overstates what is known. The 11 ms gap is measured but its cause
is not. Linux EHCI hosts the same Pico-class device at 677 KiB/s
on the same FS bus per `r25-direct-usb-baseline.md`, which proves
the throughput is not a silicon limit; the IDF host stack or its
DWC2 register configuration differs from Linux's in a way that
costs 60x. The "accept as final" step is removed below; the
investigation is open and the next concrete steps are listed in
`r25-isr-instrumentation.md` (corrected version).

## Verdict

**Inconclusive on the NAK question due to firmware crash.** The
continuous-fill bench triggered an `LoadProhibited` panic in
`esp_netif_free_rx_buffer` / `pbuf_free` after ~5 seconds of
streaming. Before the crash, three `dwc_isr` summary windows fired
showing `avg_gap=13-15 ms` (slightly **higher** than the 11 ms
baseline, not lower as the NAK hypothesis would predict). But these
numbers are contaminated by the raw-REPL handshake and the impending
crash so they cannot be taken as a clean refutation either.

**The dispatch's stated stop condition (harness/script crashes,
report and stop, don't debug) applies. We do not have a clean
result on the NAK hypothesis.**

## Headline numbers (contaminated, partial)

Three summary windows emitted before the panic, n=100 to n=300 on
the bulk-IN channel:

| Metric | n=100 | n=200 | n=300 | baseline (R25 stage B) |
|---|---|---|---|---|
| `avg_proc` | 18 us | 18 us | 18 us | 14-16 us |
| `min_proc` | 2 us | 2 us | 2 us | 1-3 us |
| `max_proc` | 43 us | 45 us | 45 us | 33-60 us |
| `avg_gap` | 13332 us | 15556 us | 14238 us | 10906-11159 us |
| `min_gap` | 35 us | 35 us | 35 us | 19-42 us |
| `max_gap` | 415 ms | 415 ms | 415 ms | 49-101 ms |
| `lost` | 0 | 0 | 0 | 0 |

`max_gap = 415 ms` is the raw-REPL handshake idle period (the channel
sat idle while the harness was negotiating the paste-mode entry).
The contamination from this large outlier inflates `avg_gap` by
roughly the constant 415 ms / 300 fires = 1.4 ms, which accounts for
the apparent rise from 11 ms baseline to ~14 ms here. Steady-state
gap is therefore not materially different from baseline.

Host-side throughput observed during the active streaming window
(t=1s to t=5s before the crash):

```
  t= 1.00s read=      7936 B  rate=     7.7 KiB/s
  t= 2.00s read=     20224 B  rate=     9.9 KiB/s
  t= 3.01s read=     28416 B  rate=     9.2 KiB/s
  t= 4.01s read=     36608 B  rate=     8.9 KiB/s
  t= 5.01s read=     44416 B  rate=     8.7 KiB/s
  t= 6.01s read=     44416 B  rate=     7.2 KiB/s   <- ESP32 crashed here
  ...
  t=10.02s read=     44416 B  rate=     4.3 KiB/s
SerialException: device reports readiness to read but returned no data
```

Throughput before the crash was ~9 KiB/s, **lower** than the 11.2 KiB/s
baseline from the read_test bench. Inconsistent with the NAK
hypothesis (which would predict an INCREASE under continuous fill).

## What crashed and where

ESP32 panic backtrace (decoded via xtensa-esp-elf-addr2line on the
firmware ELF):

```
Guru Meditation Error: Core 1 panic'ed (LoadProhibited)
EXCVADDR: 0x02692169          # invalid pointer dereference

esp_netif_free_rx_buffer at components/esp_netif/lwip/esp_netif_lwip.c:1320
esp_pbuf_free            at components/esp_netif/lwip/netif/esp_pbuf_ref.c:36
pbuf_free                at components/lwip/lwip/src/core/pbuf.c:770
pbuf_free                at components/lwip/lwip/src/core/pbuf.c:733
lwip_recv_tcp            at components/lwip/lwip/src/api/sockets.c:1049
lwip_recvfrom            at components/lwip/lwip/src/api/sockets.c:1257
lwip_recv                at components/lwip/lwip/src/api/sockets.c:1322
recv                     at usbip_server.c
read_exact               at usbip_server.c
handle_urb_stream        at usbip_server.c
handle_import_request    at usbip_server.c
handle_client            at usbip_server.c
client_task              at usbip_server.c
```

This is a heap/use-after-free in lwIP's TCP receive path during a
`pbuf_free`. Surfaced inside `lwip_recv` called from our
`usbip_server.c read_exact`. Our code is reading the next CMD_SUBMIT
from the kernel-side vhci_hcd (i.e., processing inbound USB/IP TCP).

This crash is independent of the NAK question. It is a latent bug
that does not surface under the chunked `cdc_throughput.py read_test`
bench (which has 50-200 ms quiescent gaps between bufsize iterations
where the lwip RX side fully drains). A truly continuous bulk-IN
stream presses on the lwip RX-buffer recycling path more aggressively
and tickles the bug.

The ELF SHA at the time of crash: `2a7eb0fed`. PC: `0x4214b9eb`.

## Why throughput went DOWN, not UP

Even before the crash, the harness measured ~9 KiB/s, lower than
the 11.2 KiB/s baseline. Two possible reasons:

1. **The streaming script's tight loop on the Pico is fed via
   MicroPython's `sys.stdout.buffer.write` which itself NAKs more
   frequently when it pauses to refill internal buffers.** A 4096 B
   write on RP2040 takes some milliseconds to fully push into the
   USB endpoint ringbuffer; while it's still pushing, the IN
   endpoint may NAK because the buffer hasn't yet been validated as
   "data ready". This is the OPPOSITE of what we wanted.

2. **The harness reads in 65 KiB chunks with a 200 ms timeout per
   read.** The test_read script chunks at 256-16384 B, much smaller
   than what the harness asks for. The kernel's cdc-acm reassembles
   into the harness's read buffer; if there's any stall in URB
   completion the harness sees a `ser.read()` return less data than
   asked. At 9 KiB/s the 65 KiB chunks each take 7+ s to fill, but
   the timeout is 200 ms. So `ser.read()` returns whatever is in
   the kernel's cdc-acm RX buffer after 200 ms. Throughput is
   correctly measured by total-bytes/total-time which is independent
   of chunk size.

Reason 1 is the more interesting effect. It suggests our continuous-
fill harness on this MicroPython device may not actually achieve the
"never NAK" condition we wanted. The test isn't quite the test we
intended to run.

## Implications

- **NAK hypothesis: still unconfirmed and still unrefuted.** The
  experiment as designed didn't produce the data we wanted. The
  Pico-side script may not actually achieve continuous fill under
  MicroPython's `sys.stdout.buffer.write` semantics. The crash
  prevented running long enough to even rule out instrument-side
  effects.
- **A new latent bug exists in our usbip_server.c receive path** that
  manifests under sustained bulk-IN load. Not in the immediate scope
  of the throughput investigation but worth flagging. Filed below.
- **Even if we fixed the firmware crash and the Pico-side fill
  mechanism**, the conclusion from stage B (HW re-arm matches the
  observed gap precisely) remains the strongest finding. The 60x
  gap to direct-USB is real and DWC2-as-host-on-S3 is the slow path.

## Files

Pico-side streaming script (sent via raw-REPL `exec_raw_no_follow`):

```python
import sys
b = bytearray(4096)
for i in range(len(b)):
    b[i] = i & 0xff
w = sys.stdout.buffer.write
while True:
    w(b)
```

Host-side harness: `/tmp/r25-nonak-harness.py`. Reproduced verbatim:

```python
#!/usr/bin/env python3
"""R25 stage B follow-up: continuous-fill bench to test the NAK-retry
hypothesis.

Usage: python3 /tmp/r25-nonak-harness.py <tty> <duration_sec>
"""

import os
import sys
import time

sys.path.insert(0, "/home/corona/mpy-pod/src/micropython/tools")

import serial
import pyboard


PICO_SCRIPT = """\
import sys
b = bytearray(4096)
for i in range(len(b)):
    b[i] = i & 0xff
w = sys.stdout.buffer.write
while True:
    w(b)
"""


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    tty = sys.argv[1]
    dur = float(sys.argv[2])

    ser = serial.Serial(tty, baudrate=115200, timeout=0.5)
    print(f"opened {tty}, scheduling {dur} s of continuous read")

    pyb = pyboard.Pyboard.__new__(pyboard.Pyboard)
    pyb.serial = ser
    pyb.in_raw_repl = False
    pyb.use_raw_paste = True
    pyb.enter_raw_repl(soft_reset=False)
    print("raw REPL entered; sending streaming script")
    pyb.exec_raw_no_follow(PICO_SCRIPT)

    n = 0
    start = time.monotonic()
    deadline = start + dur
    last_print = start
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        budget = max(0.0, deadline - now)
        ser.timeout = min(0.2, budget)
        chunk = ser.read(65536)
        if chunk:
            n += len(chunk)
        if now - last_print >= 1.0:
            elapsed = now - start
            rate = n / elapsed if elapsed > 0 else 0
            print(f"  t={elapsed:5.2f}s read={n:10d} B  rate={rate / 1024:8.1f} KiB/s")
            last_print = now

    elapsed = time.monotonic() - start
    rate = n / elapsed if elapsed > 0 else 0
    print()
    print(f"=== summary: read {n} bytes in {elapsed:.3f} s = {rate:.0f} B/s = {rate / 1024:.1f} KiB/s ===")
    ser.close()


if __name__ == "__main__":
    main()
```

Bench output: `/tmp/r25-nonak-bench.log`
UART log: `/tmp/r25-nonak-uart.log`

## Suggested next steps (NOT executed; for user decision)

1. **Address the lwip pbuf-free crash first.** It's a latent
   correctness bug that may also be perturbing prior bench numbers
   in subtle ways. Need to examine the usbip_server.c read_exact
   path to see if there's a double-free or use-after-free condition
   under sustained load. Out of scope for this dispatch.

2. **Redesign the never-NAK test for a different device.** Two
   options:
   - A Pico running pre-compiled C-level USB CDC firmware (TinyUSB
     directly, no MicroPython) that statically holds the IN ringbuffer
     full from a DMA loop. This guarantees no NAK from the device
     side regardless of host scheduling.
   - A more aggressive MicroPython script using `_thread` to run
     the fill loop alongside a separate task that ensures the
     ringbuffer never empties. Risky on RP2040 GIL semantics.

3. **Read DWC OTG databook directly** to determine if the FS-bulk-IN
   NAK retry interval is hardware-fixed or software-tunable (e.g.,
   via HCFG, HCCHAR, or some retry counter register). If software-
   tunable, the experiment can be run by lowering the retry interval
   on the host side instead of trying to make the device never NAK.

4. **Continue the investigation.** Stage B measured `avg_gap=11 ms`
   between bulk-IN ISR fires; what causes the gap is not yet
   established. The "accept as final" framing in an earlier draft
   was wrong: Linux EHCI achieves 677 KiB/s through this same
   Pico-class device on the same FS bus, so the gap is a software
   or DWC2-config difference, not silicon. See
   `r25-isr-instrumentation.md` for the prioritised list of next
   experiments.

## Project status after this experiment

- IDF tree: still patched with the stage B ISR instrumentation
  (commit at `fcae3288` + working-tree diff per
  `r25-isr-instrumentation.md`).
- Project main: latest commit is the stage B findings doc
  (`99403a5` / `08ec9a9`). No code changes in this experiment.
- Hardware: ESP32-S3 was rebooted by the panic; current state is
  whatever the most-recently-flashed firmware boots into. No
  re-flash needed for normal operation.
- USB/IP: detached cleanly post-experiment.
