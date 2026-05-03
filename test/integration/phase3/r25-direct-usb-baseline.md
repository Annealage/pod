# R25 direct-USB baseline: 677 KiB/s

## Result

`cdc_throughput.py read_test` against a directly-connected MicroPython
Pico (no usbip, no ESP32 in the path) reaches **677 KiB/s steady-state**
at bufsize=256, scaling to ~695 KiB/s at bufsize=4096.

| bufsize | nbuf | rate (B/s) | KiB/s |
|---|---|---|---|
| 256   | 128 | 693201 | 677.0 |
| 512   | 128 | 701869 | 685.4 |
| 1024  | 128 | 705840 | 689.3 |
| 2048  | 128 | 698075 | 681.7 |
| 4096  | 128 | 711922 | 695.2 |
| 8192  | 128 | 697819 | 681.5 |
| 16384 | 85  | 687092 | 671.0 |

ESP32 IDF reference (commit `ae9afc3`, R25 step 4 with HZ=1000):
**11.2 KiB/s steady-state across all bufsizes.**

**Ratio: ~60x slowdown via the ESP32 USB/IP path.** The headline number
for the rest of R25.

## Methodology

Same `serial_test.read_test` driver, same wrapper script, same
invocation pattern, same Linux host:

```
python3 test/integration/phase3/cdc_throughput.py <tty>
```

Direct test target:
- mpy-dev label `pico2-w` (RP2350)
- serial number `d83acddef8d410eb`
- tty `/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_d83acddef8d410eb-if00`
- USB FS CDC, plugged directly into the Linux host

ESP32 path target (R23 deep-dive and earlier R25 windows):
- mpy-dev label `esp32-s3` running mpy-pod firmware
- USB host on ESP32 enumerates a Pico W (RP2040, serial
  `a5a2229740635c53`) on remote 192.168.0.166
- USB/IP from 192.168.0.166 over Wi-Fi to the local host's vhci_hcd
- Same `cdc_throughput.py` driver against the resulting `/dev/ttyACMn`

## What this rules out

The 60x gap exceeds any plausible single-layer explanation that lives
outside the ESP32 firmware:

- **Bench command/response overhead.** Same script against a different
  USB target hits 677 KiB/s. The `pyboard.exec_raw_no_follow` setup +
  `serial_test.read_test` polling loop are not the gate.
- **Pico stdout pipeline limit.** The Pico can produce data at 677
  KiB/s when the consumer reads it directly. (The MicroPython stdout
  ringbuffer was a hypothesised cap in earlier R-rounds; this baseline
  refutes that as the binding constraint.)
- **USB Full-Speed (12 Mbps) protocol limits.** 677 KiB/s = 5.5 Mbps
  payload, well within FS bus capacity. Wire is not the limit.
- **Linux cdc-acm / vhci_hcd reassembly overhead.** Direct cdc-acm
  consumption hits 677 KiB/s; cdc-acm is not the gate. Whether
  vhci_hcd contributes overhead in the ESP32 path is a separate
  question the next investigation step probes.

What is NOT ruled out and lives in the ESP32 path:

- usbhost.c IDF callback handling
- usbip_server.c worker -> lane signalling
- usbip_server.c tx_ret_submit TCP send
- lwIP socket / pbuf path
- ESP32 Wi-Fi tx scheduling
- vhci_hcd RX loop on the Linux side

## Caveat

The two tests use different Pico boards: RP2040 W (firmware Pico W) on
the usbip-attached side, RP2350 W (Pico 2 W) directly. Both run
MicroPython FS-mode CDC. The MicroPython stdout-write path on RP2350
is somewhat faster than RP2040 (clock difference, USB interrupt cost),
but the difference is at most ~30%, not 60x. The comparison stands.

## Leading hypothesis and next instrumentation

The 60x gap is dominated by ESP32-side processing, not by USB hardware
or by Pico-side production rate. Within the ESP32 path the most
expensive per-URB operation we can identify from prior rounds is the
TCP `RET_SUBMIT` send: every URB completion produces a ~176-byte
RET_SUBMIT message that goes via `lwip_writev` to the kernel-side
vhci_hcd. R21 added `lwip_writev` to coalesce the 8-byte header with
the URB header and payload into a single send call (vs three previously),
but the per-URB TCP send cost itself is unmeasured.

If `lwip_writev` is averaging ~10 ms per URB, that alone explains the
~88 URB/s ceiling and is the dominant bottleneck. R25 next step
(separate doc) instruments `tx_ret_submit` with the same
`esp_timer_get_time()` pattern used in R23 deep-dive, with two anchor
points:

1. Inside `tx_ret_submit`: avg / min / max around the `lwip_writev`.
2. From IDF completion callback to `tx_ret_submit` entry: signalling
   overhead between `usbhost.c` and the responder/lane path.

Three branches the data can land in:

- `avg_writev ~ 10 ms`: TCP send is the gate. Look at Nagle,
  TCP_NODELAY, lwIP send window, Wi-Fi tx pacing.
- `avg_writev < 1 ms` but `avg_cb2tx ~ 10 ms`: worker -> lane chain
  is the gate. Look at queue / semaphore signalling.
- both `< 1 ms`: gate is downstream of `lwip_writev`. Need pbuf or
  socket-level instrumentation next.

This baseline doc is the reference point. All future R25-and-later
throughput investigations compare against the 677 KiB/s figure as the
direct-USB ceiling.
