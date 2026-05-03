# R26: lwIP `pbuf_free` corruption under sustained bulk-IN load

## Context

A separate correctness issue surfaced during R25 stage B follow-up
testing (`r25-nonak-attempt.md`). The investigation was looking at
USB throughput; the crash itself is independent of that question.
Filed as R26 so it is not lost in the R25 throughput history and
can be picked up on its own.

This bug has been latent under the current firmware for some time.
The `cdc_throughput.py read_test` bench used in R20 through R25
runs in chunked iterations (bufsize=256, 512, ..., 16384), each
iteration reading 16-32 KB then stopping briefly between bufsize
changes. Those quiescent gaps (50-200 ms) let the lwIP TCP receive
queue fully drain, which avoids the conditions that trigger this
bug. A continuous-stream test that keeps the receive path under
sustained pressure ran into the bug after about 5 seconds.

## Symptom

`Guru Meditation Error: Core 1 panic'ed (LoadProhibited)`. The CPU
load attempted to access an invalid pointer; `EXCVADDR` was
`0x02692169`, which is not a valid memory region on ESP32-S3. The
firmware reboots immediately. From the host side, the kernel-side
cdc-acm device sees its USB/IP backend disappear; mpremote sessions
hang.

The crash always lands in lwIP's `pbuf_free` chain on the receive
side. It does not happen on the transmit side.

## Reproduction

Tested on commit `99403a5` (R25 stage B firmware: priority-20 worker,
HZ=1000, ISR-trace ring buffer, TCP timing instrumentation enabled).
Crash also expected on prior commits since the bug is in code
unrelated to those R25 changes.

### Hardware

- ESP32-S3 dev board, `mpy-dev` label `esp32-s3`.
- A Pico-class MicroPython device exposed via USB/IP from
  192.168.0.166 (busid 1-1).

### Steps

1. Flash the firmware: `bash src/tools/build.sh && bash src/tools/flash.sh esp32-s3`
2. Cycle: `mpy-dev cycle esp32-s3 ; sleep 18`
3. Attach: `sudo usbip attach -r 192.168.0.166 -b $(sudo usbip list -r 192.168.0.166 | grep -oE "1-[0-9]+" | head -1)` ; `sleep 3` ; identify the resulting `/dev/ttyACM*` (look in `/sys/bus/usb/devices/5-1/5-1:*/tty/`)
4. Capture UART: `cat /dev/serial/by-id/usb-1a86_USB_Single_Serial_5A45040839-if00 > /tmp/r26-uart.log &`
5. Run the harness saved at `/tmp/r25-nonak-harness.py` (full source in `r25-nonak-attempt.md`):

   ```bash
   python3 /tmp/r25-nonak-harness.py /dev/ttyACM<N> 15
   ```

   The harness sends a tight-loop streaming script to the Pico via
   raw REPL (`while True: sys.stdout.buffer.write(b)` where `b` is
   a 4096-byte buffer), then reads bytes from the tty for 15 s.
6. Observe: the ESP32 panics around t=5 s. UART log captures the
   `Guru Meditation Error` plus a register dump and a backtrace.

The Pico-side script and host-side harness are reproduced in
`r25-nonak-attempt.md`. They are out-of-tree (`/tmp/`) and stable
enough to drop into the test/ tree if this becomes a recurring
debug aid.

## Decoded panic backtrace

From `/tmp/r25-nonak-uart.log`, decoded against
`build-ESP32_S3_ANNEALAGE_POD/micropython.elf` (stage B build, ELF SHA
`2a7eb0fed`) using
`/home/corona/.espressif/tools/xtensa-esp-elf/esp-14.2.0_20241119/xtensa-esp-elf/bin/xtensa-esp-elf-addr2line`:

```
Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.
Core  1 register dump:
PC      : 0x4214b9eb  PS      : 0x00060730  A0      : 0x820d09dd  A1      : 0x3c1c2850
A2      : 0x02692135  A3      : 0x00000000  A4      : 0x3fcee8d4  A5      : 0x00000000
A6      : 0x00000000  A7      : 0x3fcee910  A8      : 0x80388110  A9      : 0x3c1c2810
A10     : 0x3fcee910  A11     : 0x00000000  A12     : 0x00000000  A13     : 0x00000000
A14     : 0x00000001  A15     : 0x3fcee8c4  SAR     : 0x00000010  EXCCAUSE: 0x0000001c
EXCVADDR: 0x02692169  LBEG    : 0x40056fc5  LEND    : 0x40056fe7  LCOUNT  : 0x00000000

Backtrace (decoded):
  esp_netif_free_rx_buffer  components/esp_netif/lwip/esp_netif_lwip.c:1320
  esp_pbuf_free             components/esp_netif/lwip/netif/esp_pbuf_ref.c:36
  pbuf_free                 components/lwip/lwip/src/core/pbuf.c:770
  pbuf_free                 components/lwip/lwip/src/core/pbuf.c:733
  lwip_recv_tcp             components/lwip/lwip/src/api/sockets.c:1049
  lwip_recvfrom             components/lwip/lwip/src/api/sockets.c:1257
  lwip_recv                 components/lwip/lwip/src/api/sockets.c:1322
  recv                      src/c_modules/usbip/usbip_server.c       (read_exact's recv at line 214)
  read_exact                src/c_modules/usbip/usbip_server.c:209
  handle_urb_stream         src/c_modules/usbip/usbip_server.c
  handle_import_request     src/c_modules/usbip/usbip_server.c
  handle_client             src/c_modules/usbip/usbip_server.c
  client_task               src/c_modules/usbip/usbip_server.c
```

EXCCAUSE 0x1c is "LoadProhibited" — the loaded address (`A2 =
0x02692135`, similar to `EXCVADDR = 0x02692169`) is in
non-cacheable / non-mapped memory. That looks like a corrupted
pointer being followed, not a stale-but-valid one.

## What is known

- The crash is on the receive path, in `pbuf_free` deep inside
  lwIP. Our code is just calling `recv()` on a TCP socket; lwIP
  is freeing internal pbufs as part of normal recv-side
  bookkeeping.
- The two `pbuf.c` frames at lines 770 and 733 indicate `pbuf_free`
  is calling itself recursively (or `pbuf_free` is being called
  from within itself via the per-chain walk).
- `0x02692169` is not a valid heap address on ESP32-S3. Heap is
  in the `0x3c00_0000` (DRAM) and `0x3d00_0000` (PSRAM) ranges.
  An invalid pointer that magnitude looks like either a freelist
  link that has been overwritten with data, or a "next" pointer
  that has been reused.
- The crash is reproducible: the same harness running for ~5 s
  reliably triggers it. We did not run statistics on the timing,
  but the crash window is small enough that running the harness
  to completion is unlikely.
- Our `read_exact` (`src/c_modules/usbip/usbip_server.c:209`) is
  trivial: just a recv-loop. No buffer handoff to lwIP from our
  side. We are not touching pbufs directly.
- The bug does NOT manifest on the chunked `cdc_throughput.py
  read_test` because that bench has 50-200 ms quiescent gaps
  between bufsize iterations during which lwIP fully drains its
  RX queues. Continuous streaming with no quiescent gap exposes
  it.
- Bench numbers from R20 through R25 all ran with this latent
  bug present. Whether any of those numbers are perturbed by
  almost-but-not-quite-corruption events that the bench survives
  is open. None of those benches showed obvious symptoms.

## What is unknown

- The exact code-path inside lwIP that produces the bad pointer.
  The `pbuf_free` recursion plus invalid pointer magnitude
  strongly suggests **double-free** or **use-after-free** of a
  pbuf somewhere in the lwIP RX flow, but we have not localised
  it.
- Whether the corruption happens on our (handle_urb_stream) RX
  path specifically or somewhere else (e.g., TCP RX from
  vhci_hcd elsewhere). The backtrace shows the crash on our
  read_exact, but lwIP RX is a shared resource: a corruption
  caused by a different code path could surface here.
- Whether the bug is in our usbip_server.c usage pattern of
  `recv()` (e.g., wrong flags, wrong socket-level option), or
  in lwIP itself, or in the IDF lwIP integration shim
  (`esp_netif_free_rx_buffer`).
- Whether `CONFIG_LWIP_TCP_*`/`CONFIG_LWIP_NUM_PBUFS` defaults
  matter. Heap-poisoning detection options (`CONFIG_HEAP_POISONING_LIGHT`
  / `_COMPREHENSIVE`) might surface the bug at a clearer point.

## Suggested investigation directions

In rough priority order:

1. **Enable comprehensive heap poisoning.** Set
   `CONFIG_HEAP_POISONING_COMPREHENSIVE=y` in
   `src/boards/ESP32_S3_ANNEALAGE_POD/sdkconfig.board`. Rebuild, re-run
   the reproducer. Comprehensive poisoning catches double-free at
   the moment of the second free instead of crashing later when
   the corrupted pointer is followed. The closer-to-the-cause
   crash gives a clean backtrace of the actual offending free.

2. **Enable lwIP pbuf-stats and assertions.** Several
   `CONFIG_LWIP_*` options enable runtime checks in pbuf
   management. Examine:
   - `CONFIG_LWIP_DEBUG`
   - `CONFIG_LWIP_PBUF_DEBUG` (if exists in our IDF version)
   - `MEMP_OVERFLOW_CHECK` (lwipopts.h)
   These add per-allocation guards that abort closer to the
   offending free.

3. **Check our usbip_server.c `recv()` usage.** Look at:
   - Whether `MSG_PEEK` or any other flag is passed (it shouldn't
     be — `recv(fd, ptr, len, 0)`).
   - Whether the same fd is being recv'd by two different threads
     concurrently. The `client_task` is the only RX path on the
     usbip socket per inspection but worth re-verifying.
   - Whether we ever call `close()` or `shutdown()` on the fd while
     a recv is in progress. The shutdown path on lwIP can free
     pbufs out from under a concurrent recv.

4. **Search Espressif issue tracker** for "lwIP pbuf_free crash"
   or "esp_netif_free_rx_buffer LoadProhibited" reports. Filed
   bugs and fix-attempts upstream are direct prior art.

5. **Try a minimal repro outside our tree.** A hello-world
   IDF app that opens a TCP socket and calls `recv()` in a tight
   loop while another peer sends 100 KiB/s+ continuous data. If
   the bug reproduces there, it is a lwIP/IDF issue and we
   should file upstream. If it does not, our code is doing
   something unusual and the issue is in usbip_server.c.

6. **Bisect IDF commits.** The bug may have been introduced by an
   IDF version bump. Currently pinned at v5.5.1 (`fcae3288`); the
   previous pin was likely an earlier v5.x. If this bug surfaces
   only above a specific IDF version, that constrains the cause.

## Hardware required

Same as R25:
- ESP32-S3 dev board on `mpy-dev` label `esp32-s3`.
- Any USB/IP-attached device with a high-rate CDC bulk-IN endpoint.
  The MicroPython Pico W on remote 192.168.0.166 (busid 1-1) used
  in the original repro is suitable; any other CDC device that can
  generate sustained ~10 KiB/s+ outbound stream works.
- The harness at `/tmp/r25-nonak-harness.py` (Pico script + host
  read loop) is the simplest reproducer.

## Out of scope

- The R25 throughput investigation. The crash is independent of
  what causes the 11 ms bulk-IN gap. Fixing this bug is necessary
  for any future bench that streams without iteration gaps, and
  necessary for production safety, but does not directly bear on
  why the IDF bulk-IN throughput is 60x lower than Linux EHCI.
- The R24 TinyUSB pivot's D-state hang. Different code path
  (TinyUSB host on r24-wip vs IDF host on main); separate failure
  mode (kernel D-state on the Linux host vs ESP32 panic on the
  device).

## Relationship to R25

R25's `cdc_throughput.py read_test` benchmarks ran from R20 onwards
without triggering this crash because the bench is structured as
short bursts with quiescent gaps. R25 stage B's continuous-fill
attempt was the first time we put the firmware under sustained RX
pressure, and the bug surfaced. Numbers reported in R20-R25
findings docs are from runs that completed normally; the crash is
not retroactively suspected of having distorted those numbers.

R25 stage B left the IDF tree patched with a working-tree diff
(`r25-isr-instrumentation.md`). That diff is not implicated in
this bug; the crash is in IDF lwIP code, not in the patched
`hcd_dwc.c`.

## File of interest

- `src/c_modules/usbip/usbip_server.c:209-222` — `read_exact`,
  the only TCP RX call in our path.
- IDF: `components/esp_netif/lwip/esp_netif_lwip.c:1320` and
  `components/lwip/lwip/src/core/pbuf.c:733-770` — where the
  invalid pointer load happens.

## Status

Open. Backlog. R25 throughput investigation continues independently
on the assumption that this bug does not perturb the chunked-bench
numbers.
