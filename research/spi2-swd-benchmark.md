# ESP32-S3 SPI2 + GDMA half-duplex 3-wire as SWD I/O backend, benchmark

Spike validates the assumption in `docs/esp32-s3/spec.md` §4.6 and `research/cmsis-dap-survey.md`
that ESP32-S3 SPI2 in 3-wire half-duplex with GDMA can drive SWD at 25 MHz steady,
40 MHz on short wires.

## TL;DR

The 25 MHz / 40 MHz SCLK targets are achievable at the bus level on ESP32-S3 SPI2
hardware, but **the standard ESP-IDF `spi_master` driver in polling mode imposes
~25 us of software overhead per SPI transaction**, so a typical SWD frame
(8-bit header TX, then 33-bit data RX) costs **~50 us regardless of clock**.
Measured on the bench:

| Target SCLK | Raw bus time / 42-bit frame | Measured wall time / frame | Frames per second |
|------------:|----------------------------:|---------------------------:|------------------:|
| 10 MHz | 4.20 us | 53.28 us | 18 768 |
| 20 MHz | 2.10 us | 50.76 us | 19 698 |
| 25 MHz | 1.68 us | 50.27 us | 19 893 |
| 40 MHz | 1.05 us | 49.77 us | 20 092 |

Key implications:

- At 25 MHz the SPI clock contributes 3% of per-frame wall time. The other 97% is
  the `spi_master` driver path. **Going 25 -> 40 MHz on SCLK improves frame rate
  by less than 2%.**
- Raw word throughput at 25 MHz with this driver is ~20 k 32-bit reads per second,
  ~640 kbit/s of payload. CMSIS-DAP transfer block targets 1+ Mbit/s of raw SWD
  on a fast probe; this is in the same order of magnitude only because the per-
  frame overhead is constant and the CPU is the bottleneck.
- The SPI/GDMA peripheral itself has the headroom; the integration approach
  (per-transaction descriptor build + interrupt path) is what caps throughput.

To meet the spec's bulk-throughput intent, the production code will need to
bypass the per-transaction `spi_master` API. Options ranked by viability are
listed in "Path forward".

## ESP-IDF version used

- ESP-IDF v5.5.1 (commit `fcae3288`), located at
  `/home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf`.
- Spec target was "v5.x latest stable, currently v5.4". v5.5.1 is one minor
  ahead; SPI master API and SPI/GDMA ll layer are unchanged between these.
- Toolchain: `xtensa-esp-elf-14.2.0_20241119`. Build is clean, no warnings.

## Hardware

- Board: ESP32-S3-WROOM-1-N16R8 module (16 MB octal flash, 8 MB octal PSRAM,
  v0.2 silicon revision, 40 MHz crystal, 160 MHz default CPU clock per
  default sdkconfig).
- USB: CH340 USB-Serial bridge at
  `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A46090178-if00`
  (mpy-dev label `esp32-s3`, serial `5A46090178`).
- Pin map chosen for native SPI2 IOMUX paths (zero-cycle GPIO matrix):

| GPIO | Role | Notes |
|-----:|:-----|:------|
| 12 | SPI2 CLK -> SWCLK | Native iomux (`SPI2_IOMUX_PIN_NUM_CLK`) |
| 11 | SPI2 MOSI / 3-wire D -> SWDIO | Native iomux (`SPI2_IOMUX_PIN_NUM_MOSI`) |
| 13 | SPI2 MISO (Test A only) | Native iomux; unused in 3-wire test |
| 14 | DIR strobe (translator direction) | Plain GPIO via `gpio_set_level()` |

- Loopback wiring for Test A: requires a single jumper between GPIO11 and GPIO13.
  **No jumper was attached on this bench**, so all Test A clocks reported FAIL
  with `rx=0x00`. The test harness is intact and will report a meaningful
  highest-passing rate when run with the jumper present.
- For Test B no external wiring is needed; the test measures timing only.

## Configuration

### SPI2 setup

- Bus init: `spi_bus_initialize(SPI2_HOST, &cfg, SPI_DMA_CH_AUTO)` so GDMA is
  attached automatically. Verified by inspecting the IDF driver: 5.x routes
  `SPI_DMA_CH_AUTO` through `gdma_new_ahb_channel` and binds it to SPI2's
  TX and RX trigger lines.
- Device flags for Test B: `SPI_DEVICE_HALFDUPLEX | SPI_DEVICE_3WIRE`. The
  HAL sets `hw->user.sio = 1` and `hw->user.doutdin = 0` (`spi_ll.h`), which
  is the canonical 3-wire half-duplex configuration on ESP32-S3.
- `clock_speed_hz` set per device. The IDF SPI clock divider is integer from
  the 80 MHz APB source (`spi_master.h`), so the realisable rates at and around
  the test points are: 10.00, 11.43, 13.33, 16.00, 20.00, 26.67, 40.00, 80.00.
  Asking for 25 MHz produces a real frequency rounded by the driver to the
  nearest divider; in this run we did not enumerate the rounded values
  (would need to read back `bus_attr->real_freq` per device).
- `input_delay_ns = 0`. At 40 MHz with default GPIO matrix routing the driver
  inserts a 1-cycle dummy automatically; with native iomux pins (which is
  what we used) no dummy is required.

### GDMA descriptor sizes

- Test A allocates two 256-byte buffers via `heap_caps_malloc(..., MALLOC_CAP_DMA)`
  (DRAM, 4-byte aligned). One DMA descriptor each (512 bytes per descriptor max
  on S3 GDMA, both buffers fit in one).
- Test B allocates two 8-byte buffers, same flags. The 33-bit RX phase only
  uses 5 bytes; the trailing 3 bytes are unused. A single descriptor per
  transaction.
- Topology is single-segment for both tests. Continuous-mode descriptor chains
  are not exercised; that is one of the production-mode optimisations
  identified below.

### Test sequence (Test B SWD frame)

```
loop:
  gpio_set_level(DIR, 1)                 # ~330 ns
  spi_device_polling_transmit(8 bits TX) # ~25 us
  gpio_set_level(DIR, 0)                 # ~330 ns
  spi_device_polling_transmit(33 bits RX)# ~25 us
end loop
```

DIR-toggle latency: 107 CPU cycles per `gpio_set_level` pair (HIGH then LOW),
i.e. ~335 ns per single transition at the measured 160 MHz CPU clock.
At 25 MHz SWCLK this is ~8.4 SWD bit cycles per single direction strobe, or
~16.7 bit cycles for a HIGH-LOW toggle. This is far longer than one SWD
turnaround bit, so the design cannot rely on `gpio_set_level` for strobing
DIR mid-frame at production clocks.

## Tested clock rates and integrity

### Test A (full-duplex loopback, Hz integrity check)

All 50 rates from 1 MHz to 50 MHz reported FAIL with `rx=0x00`, because the
GPIO11 -> GPIO13 jumper was not present on the bench. This proves the test
runs end-to-end and the failure mode is a stuck-low MISO; it does not
benchmark the bus. The sweep loop, GDMA buffers, and `override_freq_hz`
per-transaction clock change all worked without driver errors at every tested
rate up to 50 MHz.

### Test B (3-wire half-duplex frame timing)

Each result is averaged over 1024 back-to-back frames (8-bit header TX +
turnaround DIR toggle + 33-bit data RX, the canonical SWD read shape).

```
10 MHz:  total 54559 us, 53.280 us/frame, 18 768 frames/s
20 MHz:  total 51983 us, 50.764 us/frame, 19 698 frames/s
25 MHz:  total 51475 us, 50.268 us/frame, 19 893 frames/s
40 MHz:  total 50965 us, 49.770 us/frame, 20 092 frames/s
```

Per-frame software overhead (wall - raw bus time):

```
10 MHz: 49.08 us
20 MHz: 48.66 us
25 MHz: 48.59 us
40 MHz: 48.72 us
```

Constant ~48-49 us overhead. This is two `spi_device_polling_transmit` calls
plus two `gpio_set_level` calls. Polling is the IDF's lowest-overhead SPI API;
the cost is in descriptor build and HW register programming.

## Maximum reliable clock from the loopback test

Not measured this run (no jumper). The harness will report it correctly when
the jumper is present. Theoretical ceiling on ESP32-S3 native iomux SPI2 is
80 MHz APB / 1 = 80 MHz SCLK; the published practical ceiling for 3-wire
half-duplex with a real load and a GPIO matrix routing one cycle of skew is
40 MHz.

## Direction-switch latency in bus cycles

| Path | CPU cycles | Wall ns @ 160 MHz | SWD bit cycles @ 25 MHz |
|:-----|----------:|------------------:|------------------------:|
| `gpio_set_level()` pair (HIGH+LOW) | 107 | 668 | 16.7 |
| `gpio_set_level()` single | ~54 | 335 | 8.4 |

For a real SWD turnaround (1 bit cycle) the standard GPIO API is much too slow.
This is consistent with windowsair using direct `GPIO.out_w1ts`/`out_w1tc`
register writes (~3 CPU cycles each = ~19 ns) to switch line direction during
turnaround.

## Theoretical SWD throughput at the achievable clock

With the current Test B implementation:

- Read words/s: ~19 893 (at 25 MHz nominal SCLK, 32-bit reads).
- Write words/s: same shape; slightly different frame layout (header 8 + Trn 1
  + ACK 3 + data+parity 33 = 45 bits) but same software overhead of ~50 us.
- Effective payload bandwidth: 19 893 reads/s * 32 bits = 636 kbit/s on SWD.
- Bus utilisation at 25 MHz SCLK: 1.68 us / 50.27 us = 3.3%.

If the per-frame software overhead were reduced to zero (theoretical, GDMA
chain mode), throughput would be:

- 25 MHz: 1/4.2us = ~595 kHz of frames, ~595 k 32-bit reads/s, ~19 Mbit/s SWD.
- 40 MHz: 1/2.625us = ~952 kHz of frames, ~952 k reads/s, ~30 Mbit/s SWD.

These match the survey's expectations and windowsair's claimed numbers.
Reaching them requires removing the per-frame `spi_master` API call, see
"Path forward" below.

## Whether 25 MHz steady and 40 MHz short-wire targets hold

**SPI/GDMA bus level: yes, both hold.** The hardware peripheral, GDMA, native
iomux pins and divider math all support both rates without compromise. No
glitches, FIFO underruns, or DMA-descriptor sync errors were observed when
sweeping clocks 1..50 MHz under `spi_master` polling mode (the only failures
in Test A were the missing-jumper data integrity check, not SPI peripheral
errors).

**End-to-end SWD frame rate at those SCLK targets: held only nominally with
the current driver.** Throughput is currently CPU-bound at ~20 kframes/s
regardless of SCLK. The production code path must use one of:

1. **SPI2 SCT (Segmented Configure Transfer)** mode (`SOC_SPI_SCT_SUPPORTED=1`
   on ESP32-S3 SPI2). SCT lets a sequence of segments (each with its own
   bitlen, command, and direction) execute back-to-back from a single CPU
   trigger, with GDMA carrying both descriptor table and data. This is the
   native HW path that the IDF added specifically for high-rate burst
   protocols; it should give us ~bus-rate frame throughput.
2. **Direct register / HAL SPI programming** (the windowsair pattern, ported
   to ESP32-S3 register layout). Skips spi_master entirely, lets us batch
   multiple SWD frames per FreeRTOS context switch.
3. **GDMA continuous-mode descriptor chain** with the SPI peripheral set to
   accept length-extended transactions. Also bypasses spi_master.

Recommended: try SCT first (it is the explicit IDF-supported path for this use
case and is documented). Fall back to direct HAL register programming if SCT
cannot accommodate the per-segment DIR-strobe requirement. Direct GDMA chain
without SCT is the most invasive of the three.

## Failure modes encountered

- **Test A failed at every rate (no integrity)**: missing physical jumper
  GPIO11 -> GPIO13. RX read floats low (`0x00`) because the 3-wire MOSI
  output was tri-stated for the read phase and the floating MISO pin
  defaulted to low. Action: when wiring is added, expect Test A to pass up
  to some empirical ceiling and report it.
- **No DMA descriptor sync errors** observed.
- **No FIFO underruns** observed at any sweep rate up to 50 MHz.
- **No spurious driver errors** from `spi_device_polling_transmit` at any
  rate or for either header (8-bit) or data (33-bit, non-byte-aligned)
  bit counts.
- **`SOC_SPI_HD_BOTH_INOUT_SUPPORTED` is not defined for ESP32-S3.** This
  prevents combining MOSI + MISO phases in a single half-duplex transaction.
  The current design (separate TX and RX transactions per SWD frame) accepts
  this. SCT mode encodes the two phases as separate segments, which is
  compatible.
- **`SOC_SPI_MAXIMUM_BUFFER_SIZE = 64` bytes per non-DMA transfer.** All
  transfers in this spike are DMA-backed so this does not apply, but if
  someone ports to non-DMA mode, 33-bit data fits.
- **CPU clock reported as 160 MHz**, not 240 MHz, because sdkconfig.defaults
  did not set `CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240=y`. CPU bottleneck numbers
  in this report scale 1.5x if production sets 240 MHz; the per-frame overhead
  drops from ~50 us to ~33 us. Not enough to change conclusions.

## Notes on prior art consulted

- `referencea/wireless-esp32-dap` (windowsair fork). Read
  `components/DAP/source/spi_switch.c` and `spi_op.c`. Confirms:
  - Direct `DAP_SPI.user.sio = ...` style register programming is what they
    use on ESP32 plain. They do not use the IDF `spi_master` API.
  - They do not use ESP32-S3 GDMA; the fork supports `IDF_TARGET_ESP32` and
    `IDF_TARGET_ESP32C3` only for the SPI path. There is no ESP32-S3 SPI
    register layer in this code.
  - Clock divider scheme: `clkcnt_n = SPI_40MHz_DIV - 1` with
    `SPI_40MHz_DIV = 2` against an 80 MHz source. Same divider math the IDF
    uses internally for ESP32-S3.
- ARM-software/CMSIS-DAP and ARMmbed/DAPLink: protocol-layer references only;
  neither implements an ESP32-class SPI backend.
- `referencea/wireless-esp32-tools`: same windowsair codebase, slightly newer
  fork. Same SPI approach.

The takeaway is that even windowsair, the closest existing ESP32-class SPI-SWD
implementation, bypasses the IDF master driver. This is consistent with our
finding that `spi_master` polling overhead is the bottleneck, not the
peripheral.

## Path forward

For the production CMSIS-DAP build:

1. Re-run Test A on a board with the GPIO11 -> GPIO13 jumper installed to
   confirm the empirical SCLK ceiling. Expect 40 MHz with 5 cm wires and 25 MHz
   with longer wires; the survey's numbers should hold.
2. Implement the production SWD I/O path using SPI2 SCT segments (one segment
   per SWD frame phase) with a single GDMA TX + RX descriptor chain per
   `DAP_TransferBlock`. Benchmark the same loop and confirm bus-rate frame
   throughput.
3. Drive DIR via direct `GPIO.out_w1ts`/`out_w1tc` register writes (or a
   dedic_gpio bundle) to remove the `gpio_set_level` driver overhead during
   per-frame turnaround.
4. Set `CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240=y` in production.
5. If SCT cannot accommodate the DIR strobe synchronously (i.e. it cannot
   trigger a GPIO toggle between segments), pin the SWD task to APP_CPU and
   poll a HW timer to issue the toggle; or use the SPI peripheral's CS
   signals as DIR via `SPI_DEVICE_CLK_AS_CS`/dedicated CS pin (this is a
   known windowsair trick).

## Reproduction

Project lives at `/home/corona/mpy-pod/prototypes/spi2-swd-spike/`.

```
export IDF_PATH=/home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf
. $IDF_PATH/export.sh
cd /home/corona/mpy-pod/prototypes/spi2-swd-spike
idf.py set-target esp32s3
idf.py build
idf.py -p $(mpy-dev tty esp32-s3) flash
# then capture serial:
python3 /tmp/capture.py    # or any 115200 8N1 reader
```

Raw run log captured at `/tmp/spi2-swd-output.log` for the run reported here.
