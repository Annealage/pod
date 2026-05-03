# R25 bufsize sweep: IDF bulk-IN timing vs bench read buffer size

Date: 2026-05-03
Branch: main
Commit: ae9afc3 (R25 step 4, CONFIG_FREERTOS_HZ=1000 in place)
Firmware built: 2026-05-03 09:59

## Setup

- ESP32-S3 on `mpy-dev` label `esp32-s3` (CH340N tty: usb-1a86_USB_Single_Serial_5A45040839-if00)
- Pico DUT at 192.168.0.166 busid 1-1, attached via usbip to
  usb-MicroPython_Board_in_FS_mode_a5a2229740635c53-if00 (ttyACM11)
- Pico CDC bulk IN endpoint: EP 0x82 IN, wMaxPacketSize=64 bytes (FS)
- IDF idf_timing instrumentation: emits every 100 URBs (cumulative avg)

## What "bufsize" means in this sweep

`cdc_throughput.py` bench `bufsize` parameter controls how many bytes the
Pico's MicroPython script writes per `wr(b)` call. It does NOT directly
control the USB bulk URB size. The IDF host stack receives 128-byte URBs
from the Linux cdc-acm driver (2 x 64-byte FS bulk packets per IDF
`usb_host_transfer_submit` call). The bench bufsize affects how frequently
and how long the Pico's CDC TX FIFO is occupied, which in turn determines
how long IDF IN URBs sit NAKing before the Pico can supply data.

Confirmed: `lsusb -s 5:7 -v` shows Pico EP 0x82 IN wMaxPacketSize=64 B.
From URB rate * throughput: 90 IDF-URBs/s * 128 B/IDF-URB = 11520 B/s =
11.2 KiB/s at bufsize=256. IDF transfer size = 128 B (2 FS packets).

## Sweep results

### Throughput vs bufsize (single sequential run, cumulative idf_timing)

Total data per size = 32768 bytes (except bufsize=16384 which used 65536).

| bufsize | nbuf | total bytes | time (ms) | throughput KiB/s |
|---------|------|-------------|-----------|-----------------|
| 64      | 512  | 32768       | 2842      | 11.3            |
| 128     | 256  | 32768       | 2844      | 11.3            |
| 256     | 128  | 32768       | 2847      | 11.2            |
| 512     | 64   | 32768       | 2854      | 11.2            |
| 1024    | 32   | 32768       | 2881      | 11.1            |
| 4096    | 8    | 32768       | 3325      | 9.6             |
| 16384   | 4    | 65536       | 8736      | 7.3             |

Note: the throughput drop at bufsize>=4096 was reproduced in the clean
per-bufsize runs below.

### idf_timing: per-bufsize clean runs (board cycled between each)

Board cycled with `mpy-dev cycle esp32-s3`, 18s wait, Pico reattached via
usbip, Python uart_monitor.py on ESP32 UART0. Counters reset to zero at
boot.

#### bufsize=256 (131072 bytes, nbuf=512)

```
n=100  I(35097) avg_submit=46us   avg_round=116060us  min=109us  max=204245us
n=200  I(36209) avg_submit=48us   avg_round=143846us  min=109us  max=204245us
n=300  I(37320) avg_submit=50us   avg_round=153324us  min=109us  max=204245us
n=400  I(38431) avg_submit=50us   avg_round=158037us  min=109us  max=204245us
n=500  I(39543) avg_submit=51us   avg_round=160739us  min=109us  max=204245us
n=600  I(40654) avg_submit=51us   avg_round=162584us  min=109us  max=204245us
n=700  I(41765) avg_submit=51us   avg_round=163851us  min=109us  max=204245us
n=800  I(42876) avg_submit=51us   avg_round=164762us  min=109us  max=204245us
n=900  I(43988) avg_submit=52us   avg_round=165456us  min=109us  max=204245us
n=1000 I(45099) avg_submit=53us   avg_round=166045us  min=109us  max=204245us
```

Per-direction at n=900 (cumulative):
- OUT: n=11 avg=284us  min=109us max=1035us
- IN:  n=889 avg=167500us min=191us max=204245us

Throughput: 11.25 KiB/s (11381ms for 131072 bytes)
URB rate (from timestamps): 90 URBs/s (delta n=900->1000: 1111ms)
Per-window avg_round (n=801-900): ~170ms
Per-window avg_round (n=901-1000): ~171ms
Bytes per IDF URB: 11520 / 90 = 128 B

#### bufsize=4096 (131072 bytes, nbuf=32)

```
n=100  I(31078) avg_round=429665us  min=110us  max=29847443us
n=200  I(32465) avg_round=445018us  min=110us  max=30272221us
n=300  I(33774) avg_round=459187us  min=110us  max=32157274us
n=400  I(35156) avg_round=390637us  min=90us   max=32157274us
n=500  I(36423) avg_round=336251us  min=90us   max=32157274us
n=600  I(37841) avg_round=429381us  min=90us   max=36588374us
n=700  I(39314) avg_round=440900us  min=90us   max=37377437us
n=800  I(40538) avg_round=596581us  min=90us   max=39282382us
n=900  I(42117) avg_round=591704us  min=90us   max=40501378us
n=1000 I(43368) avg_round=590306us  min=90us   max=42131370us
```

Note: avg_submit is negative due to int32 overflow in the instrumentation
accumulator when large round-trips dominate (known issue from R23 doc).

Per-direction IN at n=889 (cumulative):
- IN: avg=599023us min=90us max=40501378us

Throughput: 9.16 KiB/s (13968ms for 131072 bytes)
URB rate (delta n=900->1000): 43368-42117=1251ms -> ~80 URBs/s
Per-window avg_round (n=801-900): 596,581us (cumulative n=800) and
591,704 (n=900): per-window = (900*591704 - 800*596581)/100 = ~548ms
max_round exceeds 30 seconds (single worst-case URB)

#### bufsize=16384 (bench timed out; partial data)

Ran nbuf=8 (131072 bytes target). Bench timed out at ~29056 bytes
(bench READ_TIMEOUT_S=2 triggered when Pico paused >2s mid-16384-byte write).

```
n=100  I(34290) avg_round=1277784us  min=114us  max=33390676us
n=200  I(36288) avg_round=1283919us  min=114us  max=35425387us
```

Throughput (extrapolated from prior sequential sweep): 7.3 KiB/s
avg_round at n=100: 1278ms, max=33s per URB.

## Derived table

IDF URB size = 128 B for all rows (2 x 64-byte FS bulk packets).
URBs per bench-read = bufsize / 128.

| bufsize | URBs/bench-read | URBs/s | avg_round (IN, window) | throughput KiB/s |
|---------|-----------------|--------|----------------------|-----------------|
| 64      | 0.5 (short URB) | ~90    | ~170ms (inferred)    | 11.3            |
| 128     | 1               | ~90    | ~170ms (inferred)    | 11.3            |
| 256     | 2               | 90     | 170ms (measured)     | 11.2            |
| 512     | 4               | ~90    | ~170ms (inferred)    | 11.2            |
| 1024    | 8               | ~88    | ~175ms (inferred)    | 11.1            |
| 4096    | 32              | ~80    | ~548ms (measured)    | 9.2             |
| 16384   | 128             | ~55    | ~1278ms (measured)   | 7.3             |

Note: at bufsize<128 the cdc-acm driver gets short-packet URB completions.

## Hypothesis determination

**H2 (NAK-bounded by Pico TX rate) confirmed for large bufsizes.**
**H1 (fixed IDF pacing) confirmed for small bufsizes (64-1024).**

The data shows two distinct regimes:

### Regime 1: bufsize <= 1024 (small writes)

avg_round stays flat at ~170ms. Throughput stays flat at ~11.2 KiB/s.
URBs/s stays at ~90. The Pico writes small buffers fast enough that the
IDF IN URB queue rarely drains completely - the Pico keeps the pipe busy.
The 170ms avg_round is IDF's fixed serialisation overhead between URB
completion and the next submit, not Pico TX stall. This is the same
finding as R23: the IDF host-task event loop adds ~10-12ms per URB of
scheduling overhead (165ms avg at 90 URBs/s with 16-URB pipeline depth).

### Regime 2: bufsize >= 4096 (large writes)

avg_round degrades dramatically: 548ms at 4096, 1278ms at 16384.
Throughput drops: 9.6 KiB/s at 4096, 7.3 KiB/s at 16384.
max_round reaches 30-40 seconds per URB - the Pico is paused mid-write
waiting for its own USB TX FIFO to drain, causing long NAK sequences.

At bufsize=16384, the Pico's MicroPython `wr(b)` call with a 16384-byte
buffer takes ~1.4 seconds to complete (16384 / 11468 B/s). During that
time, the 16 pipelined IDF IN URBs all NAK. The cdc-acm driver's 2-second
timeout triggers, causing the bench test to fail.

### The 170ms IDF floor (Regime 1 finding)

The 170ms per-window avg_round in Regime 1 is the IDF serialisation latency
with no Pico stall. 90 URBs/s * 128 B/URB = 11520 B/s. This is consistent
with R23 baseline and unchanged by CONFIG_FREERTOS_HZ=1000 (R25 step 4).
The floor is IDF's host-client event loop scheduling between URB completion
and next submit, not Pico TX rate.

### Verdict

**H1 (fixed-interval IDF pacing) holds for bufsizes 64-1024.**
**H2 (NAK-bounded by Pico TX FIFO stalls) dominates for bufsizes >= 4096.**

The 11.2 KiB/s ceiling for bufsizes 64-1024 is set entirely by IDF's
~170ms per-URB serialisation latency at 128 B/URB, not by Pico TX rate.
The bufsize sweep does NOT unlock throughput - H4 is ruled out.

## Implications for R25

The bufsize knob in the bench is irrelevant for improving IDF throughput -
it cannot change the IDF host-task scheduling latency. The 11.2 KiB/s
ceiling remains at 90 IDF-URBs/s * 128 B/URB.

The next levers to try (from r25-tune-idf-bulk-in-plan.md):
- Step 3: bump IDF host task priority (still untested)
- Step 6: hcd_* lower API to bypass the event-loop serialisation

The max_round=204ms at bufsize=256 is noteworthy: even in the best case,
the IDF takes up to 204ms per IN URB. The min_round=109-191µs (wire floor)
proves the hardware is capable of fast IN delivery, but IDF serialises
them at ~90/s.

## Raw bench output (excerpts)

### bufsize=256 bench result

```
DATA IN: bufsize=256, nbuf=512, read 131072 bytes in 11381.63 msec = 11.25 KiB/s
RESULT: bufsize=256 nbuf=512 rate=11516 bytes/s kib_s=11.2
```

### bufsize=4096 bench result

```
DATA IN: bufsize=4096, nbuf=32, read 131072 bytes in 13968.23 msec = 9.16 KiB/s
RESULT: bufsize=4096 nbuf=32 rate=9384 bytes/s kib_s=9.2
```

### bufsize=16384 bench result (failed)

```
ERROR: timeout waiting for data
RESULT: bufsize=16384 nbuf=8 rate=0 bytes/s kib_s=0.0
```

(Bench received ~29056 of 131072 bytes before 2-second timeout on read.)

### Sequential sweep summary (all sizes, non-reset counters)

```
=== summary (bufsize, nbuf, bytes/sec, kib/sec) ===
  bufsize=64     nbuf=512  rate=11531      kib_s=11.3
  bufsize=128    nbuf=256  rate=11522      kib_s=11.3
  bufsize=256    nbuf=128  rate=11512      kib_s=11.2
  bufsize=512    nbuf=64   rate=11480      kib_s=11.2
  bufsize=1024   nbuf=32   rate=11374      kib_s=11.1
  bufsize=4096   nbuf=8    rate=9855       kib_s=9.6
  bufsize=16384  nbuf=4    rate=7502       kib_s=7.3
```
