# SWD I/O and SWO capture engine (WS-D)

This document captures the implementation decisions for the dapprobe `io/`
subsystem. Source lives at `src/c_modules/dapprobe/io/{swd,swo}.{c,h}`.

The engine is a private surface inside the dapprobe module; the only caller
is the WS-C CMSIS-DAP protocol layer (`dap_core.c` and the vendored ARM
sources). Hardware flashing of a real CMSIS-DAP host is Phase 3.

## 1. Decision: SCT mode vs HAL-direct

Phase 0.3 spike (`research/spi2-swd-benchmark.md`) established that the
standard ESP-IDF `spi_master` polling driver imposes ~25 us of software
overhead per transaction. A typical SWD frame is two phases (header out,
data in across a turnaround), so under `spi_master` each frame takes ~50 us
regardless of SCLK rate. Production code therefore had to use one of:

- SPI2 SCT (Segmented Configure Transfer) mode using `SOC_SPI_SCT_SUPPORTED`.
- Direct HAL or register-level programming, matching `windowsair/wireless-esp8266-dap`.

I picked **direct HAL programming** via `spi_ll.h` and direct `spi_dev_t`
register writes, with `spi_bus_initialize()` from `spi_master` used only
to claim the peripheral, allocate a GDMA channel, and bind the device-list
mutex. The hot path (`swd_transfer`, `swd_send_bits`, `swd_recv_bits`,
`swd_phase_header_ack`) is all inline register manipulation.

### 1.1 Why not SCT

ESP-IDF v5.5.1 exposes SCT mode behind a private header
(`esp_private/spi_master_internal.h`): callers must `#include` an
"esp_private" path, which the IDF documents as "we don't provide backward
compatibility, and safety on these APIs either". That alone is not
disqualifying, but two technical reasons settled the decision:

1. **DIR strobe synchronisation.** The SWDIO direction-controlled
   translator (74LVC1T45 family) needs its DIR pin toggled between SWD
   frame phases (TX header, then 1-bit Trn, then RX ACK + data, then
   1-bit Trn back). SCT mode lets you queue multi-segment SPI transactions
   from a single CPU trigger, but the CS line is the only per-segment
   signal SCT can drive synchronously. Our DIR is a separate GPIO. There
   is no clean SCT primitive to "pulse GPIO N between segments K and K+1"
   without falling back to per-segment CPU intervention, which defeats the
   point.
2. **Frame shape.** SWD's combined header-out + ACK-in shape (8 MOSI bits
   + 4-5 MISO bits across one continuous SCLK) is what windowsair encodes
   directly via `user.usr_mosi=1, user.usr_miso=1, user.sio=1` in a single
   transaction. Reproducing that shape in SCT requires two segments with
   a CS gap, which adds idle SCLK cycles between phases and breaks SWD
   timing.

The `windowsair/wireless-esp8266-dap` reference confirms this: their
ESP32 path bypasses `spi_master` entirely and writes the registers, the
exact pattern adopted here. ESP32-S3 register layout differs from plain
ESP32 (the `mosi_dlen` and `miso_dlen` registers are split, and DMA is
GDMA rather than the legacy DMA controller), so the implementation cannot
be a literal copy; it is reimplemented using `spi_ll_*` HAL helpers where
they cover the field, with direct `spi_dev_t` member writes for fields
the HAL does not expose (`hw->user.sio`, `hw->ctrl.wr_bit_order`, etc).

### 1.2 What the spike said vs what we did

The Phase 0.3 spike "Path forward" §1 recommended trying SCT first and
falling back to direct HAL only if SCT could not accommodate the per-segment
DIR strobe. The DIR-strobe constraint above is exactly the failure case the
spike anticipated; we go directly to HAL-direct without burning effort on
an SCT path that the spike already flagged as risky. This is documented as
the deliberate decision in the workstream brief; we are not papering over a
failed SCT attempt.

If a future workstream wants to revisit SCT for the `DAP_TransferBlock` bulk
path (where many same-shape frames run back to back without DIR changes),
the option is still available; that would only optimise the bulk path and
would not replace the per-frame engine here.

## 2. SPI2 setup

### 2.1 Pin binding

Per `docs/esp32-s3/spec-appendix-A-pinmap.md` §A.5.1:

| GPIO | Role          | Routing                                |
|-----:|:--------------|:---------------------------------------|
|   10 | SWCLK         | GPIO matrix (SPI2 sclk_out)            |
|   11 | SWDIO (data)  | IO_MUX direct to `SPI2_IOMUX_PIN_NUM_MOSI` |
|   12 | SWDIO_DIR     | dedic_gpio bundle channel 0            |
|   14 | nRST          | GPIO matrix open-drain                 |

GPIO11 is the native ESP32-S3 IO_MUX pin for SPI2 D (the windowsair
"data" pin in 3-wire half-duplex). Using IO_MUX direct gives single-cycle
input/output without the GPIO matrix's 1-cycle skew, which keeps the
empirical 40 MHz ceiling reachable on short wiring.

GPIO10 is not an IO_MUX pin for SPI2; SCLK is routed via the GPIO matrix.
The matrix adds 1 APB cycle of skew, which is acceptable for SCLK because
the DUT samples on rising edge and the timing budget at 40 MHz is 12.5 ns
per half-cycle.

GPIO12 is the SWDIO_DIR translator strobe. It uses a single-pin dedic_gpio
bundle so `dedic_gpio_bundle_write()` lowers per-toggle cost from ~335 ns
(measured for `gpio_set_level()` in the spike) to a single CPU cycle at
240 MHz (~4 ns). At 25 MHz SCLK one bit cycle is 40 ns; the spike showed
`gpio_set_level()` consumes ~8 SWD bit cycles per single transition,
which is far longer than one Trn bit.

### 2.2 Clock divider

Source clock: 80 MHz APB (matches the spike). Realised SCLK is the integer
divider `80 / N` MHz: 80, 40, 26.67, 20, 16, 13.33, 11.43, 10, 8.89, 8,
... The HAL function `spi_ll_master_set_clock(hw, 80e6, hz, 50)` picks
the closest divider <= the requested rate and returns the actual realised
frequency. We cache that value and expose it via `swd_get_clock_hz()`.

### 2.3 Frame phases

The 74LVC1T45 translator's DIR pin must follow the SPI peripheral's bus
direction synchronously. A single `usr_mosi=1, usr_miso=1` transaction
flips the SPI peripheral's MOSI driver mid-frame, but the translator's
DIR pin is on a separate GPIO and cannot be driven from inside the SPI
peripheral. The engine therefore splits each SWD frame into discrete
single-direction transactions, with `dedic_gpio_bundle_write()` strobing
DIR between them.

For an SWD read (`is_read = true`):

1. DIR=out, send 8-bit header (`usr_mosi=1, usr_miso=0, mosi_bitlen=8`).
2. DIR=in, receive Trn(1) + ACK(3) + Trn(1) (`usr_mosi=0, usr_miso=1,
   miso_bitlen=5`). Bit 0 is junk Trn, bits 1..3 are the ACK code.
3. If ACK = OK: DIR stays=in, receive 33 bits (data + parity) via
   `usr_miso=1, miso_bitlen=33`.
4. DIR=out, send 8 idle clocks.
5. Host parity check on the 32-bit value; mismatch returns
   `SWD_STATUS_PARITY_ERR`.

For an SWD write (`is_read = false`):

1. DIR=out, send 8-bit header (`usr_mosi=1, miso=0, mosi_bitlen=8`).
2. DIR=in, receive Trn(1) + ACK(3) (`miso_bitlen=4`).
3. If ACK = OK: DIR=out, send 33 bits (data + parity) via
   `usr_mosi=1, mosi_bitlen=33`.
4. Send 8 idle clocks.

The DIR strobe between phases is one `dedic_gpio_bundle_write()` call,
which executes in a single CPU cycle (~4 ns at 240 MHz). At 25 MHz SCLK
one bit cycle is 40 ns; the strobe fits inside one bit cycle and does
not delay the SPI transaction's start. The natural Trn bit at the SWD
phase boundary absorbs any residual skew.

Idle clocks after each frame are conservative; CMSIS-DAP common practice
is at least 8. They flush any in-flight bit on the wire before the next
DIR change.

### 2.4 Notes on register fields used

Fields touched in the hot path:

- `user.usr_mosi`, `user.usr_miso`: enable each phase.
- `user.sio`: 3-wire half-duplex routing (MOSI doubles as MISO).
- `user.usr_command`, `user.usr_addr`, `user.usr_dummy`: forced 0;
  SWD has no command/address/dummy phases.
- `mosi_dlen`, `miso_dlen` (via `spi_ll_set_mosi_bitlen` /
  `spi_ll_set_miso_bitlen`): per-transaction bit lengths.
- `data_buf[0..1]`: TX/RX FIFO. SWD frames fit in 64 bits, so non-DMA
  CPU FIFO is sufficient and avoids GDMA descriptor build cost.
- `cmd.update`, `cmd.usr`, `dma_int_raw.trans_done`: standard apply +
  start + wait + clear sequence.

`ctrl.wr_bit_order = 1` and `ctrl.rd_bit_order = 1` set LSB-first on the
wire, which matches the SWD frame layout (start bit first, parity last).

## 3. Loopback performance target

Phase 2 exit criterion: `swd_transfer()` round-trips correctly at 25 MHz
under loopback (SWDIO data line shorted to itself). Bench measurements
will be filled in once Phase 3 hardware brings up; this section captures
the projected numbers.

Theoretical raw bus time per SWD read frame at 25 MHz SCLK:

- Header = 8 bits = 0.32 us
- Trn + ACK + Trn = 5 bits = 0.20 us
- Data + parity = 33 bits = 1.32 us
- Idle = 8 bits = 0.32 us
- DIR strobes (3 transitions across the frame): 3 * ~4 ns = 12 ns
- Total ~2.18 us per frame on the wire

Per-frame software overhead with the HAL-direct path (vs ~50 us under
`spi_master`) is dominated by the apply+start+wait loop, repeated four
times per read frame (header TX, ACK RX, data RX, idle TX):

- `spi_ll_apply_config()` write to `cmd.update` + spin: 2-3 us
- `spi_ll_user_start()` + spin to `trans_done`: 1-2 us
- 4 transactions per frame: 12-20 us of CPU time

Projected: 15-25 us per frame, or 40-65 kHz of frames, 1.3-2.0 Mbit/s
SWD payload. This is 2-3x the spike's `spi_master` ceiling. The
`DAP_TransferBlock` bulk path can amortise the 4-transaction shape by
chaining frames without the idle-clock break, getting closer to the
2-transaction shape (header+ACK, then data) used in standard SWD reads
on the wire.

The 40 MHz target halves the bus-time portion. The per-frame software
overhead dominates again at that clock; the marginal gain from 25 MHz to
40 MHz with this engine is ~5%, much smaller than the 7% gain the spike
saw with `spi_master`. Bench-validated numbers go in §4.4 once Phase 3
hardware runs.

## 4. SWO pipeline

### 4.1 Two-tier ring sizing

Per spec.md §4.7 and `research/cmsis-dap-survey.md` §c:

- **Tier-1 DRAM**: 64 KB total, split into two 32 KB ping-pong chunks.
  UHCI cannot DMA into PSRAM, so this tier is mandatory and must be
  internal RAM with `MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL`.
- **Tier-2 PSRAM**: 8 MB. PSRAM octal at 80 MHz DDR sustains ~84 MB/s,
  vastly more than 6 Mbps SWO needs (~0.75 MB/s). The ring's purpose is
  resilience to multi-second Wi-Fi stalls, not bandwidth.

Sizing math at 6 Mbps sustained SWO:

| Tier | Size  | Time at 6 Mbps |
|:-----|------:|---------------:|
| 1    | 64 KB | 85 ms          |
| 2    | 8 MB  | 10.7 s         |

The drain task only needs to keep up with the average rate (0.75 MB/s);
PSRAM contention can stall it for tens of milliseconds during Wi-Fi
bursts without tier-1 overflowing. The 10.7 s tier-2 depth absorbs even
prolonged host-side stalls (e.g. pyOCD GC pause, network reconnect).

If the host is consuming faster than the trace is generated, both tiers
drain; if slower, tier-2 fills first, then on tier-2 overflow we drop
oldest-data and set `DAP_SWO_BUFFER_OVERRUN`. CMSIS-DAP semantics: never
inject asterisks (the loathed DAPLink #179 behaviour); just keep
streaming and surface the flag.

### 4.2 UHCI configuration notes

UHCI is the ESP-IDF v5.5+ peripheral driver at `driver/uhci.h`. Key
configuration:

```c
uhci_controller_config_t uhci_cfg = {
    .uart_port = UART_NUM_1,
    .max_receive_internal_mem = 32 KB,   // size of one ping-pong chunk
    .max_packet_receive       = 32 KB,   // length-EOF threshold
    .dma_burst_size           = 32,
    .rx_eof_flags = {
        .idle_eof = 1,    // EOF on UART idle (so partial fills surface)
        .length_eof = 1,  // EOF when chunk is full
    },
};
```

UART1 RX is on GPIO13 per Appendix A; we route it via `uart_set_pin()`
through the GPIO matrix (the IOMUX default is GPIO18, which we don't use
because GPIO13 is the spec-assigned SWO carrier pin).

### 4.3 ISR -> drain task contract

The UHCI RX completion callback runs in ISR context and only does:

1. Identify which tier-1 chunk just completed (pointer compare against
   `s_state.tier1_chunk[]`).
2. Push a `(chunk_idx, length)` event onto the completion queue with
   `xQueueSendFromISR`.

The drain task on APP_CPU pops events and:

1. Takes the tier-2 lock.
2. memcpy's the chunk into the tier-2 PSRAM ring at `tier2_head`.
3. If insufficient free space, drops the oldest bytes (advances `tier2_tail`)
   and bumps `overruns_total + overrun_latched`.
4. Re-mounts the chunk with `uhci_receive()` for the next round.

This keeps the ISR short (no PSRAM access), and the drain task absorbs
PSRAM write latency at task priority 7 on APP_CPU, off the critical USB/IP
RX path.

### 4.4 Open items measured during Phase 3

- Empirical loopback peak SCLK (bench rig with GPIO11 jumpered).
- Per-frame wall time at 10 / 20 / 25 / 40 MHz. Target: < 10 us/frame.
- SWO sustained rate at 4 Mbps with host draining at 0.5 MB/s; expected
  zero overruns over 60 s.
- Wi-Fi-burst impact on tier-2 PSRAM write latency.

These get appended to this document when WS-D moves into Phase 3
integration.

## 5. Public API summary

See `swd.h` and `swo.h` for the canonical declarations. The contract that
WS-C consumes:

```c
// SWD
esp_err_t   swd_init(const swd_config_t *config);
esp_err_t   swd_set_clock_hz(uint32_t hz);
swd_status_t swd_transfer(uint8_t header, const uint32_t *data_in, uint32_t *data_out);
esp_err_t   swd_line_reset(void);

// SWO
esp_err_t   swo_init(uint32_t baud, swo_mode_t mode);
ssize_t     swo_read(uint8_t *buf, size_t max_len);
uint32_t    swo_overruns_total(void);
size_t      swo_bytes_buffered(void);
```

`swd_status_t` returns the CMSIS-DAP-shaped ack codes (OK/WAIT/FAULT/
PARITY_ERR/PROTOCOL). The CMSIS-DAP protocol layer maps these directly
into the on-the-wire ACK byte for `DAP_Transfer` / `DAP_TransferBlock`.

## 6. Build wiring

The io/ subdir builds via `io/io.cmake`, which is a fragment for WS-C to
include from `src/c_modules/dapprobe/micropython.cmake`:

```cmake
include(${CMAKE_CURRENT_LIST_DIR}/io/io.cmake)
```

Until WS-C wires that include in, the io/ sources are still build-checked
by the standalone unit-test app at `test/unit/dapprobe-io/`. That app
links the io/ sources directly via an EXTRA_COMPONENT_DIRS pointer and
runs a smoke loopback on real hardware.

## 7. References

- `docs/esp32-s3/spec.md` §4.6 (SWD backend), §4.7 (SWO pipeline)
- `docs/esp32-s3/architecture.md` §3 (concurrency), §4.3 (SWO data flow)
- `docs/esp32-s3/spec-appendix-A-pinmap.md` §A.5.1 (S3 GPIO assignment)
- `research/cmsis-dap-survey.md` §2 (SPI as SWD), §3 (SWO buffering)
- `research/spi2-swd-benchmark.md` (Phase 0.3 spike, the ~25 us/transaction finding)
- `prototypes/spi2-swd-spike/main/spi2_swd_spike.c` (working SPI2+GDMA setup)
- `referencea/wireless-esp32-dap/components/DAP/source/spi_op.c` (windowsair register-direct pattern)
- IDF v5.5.1 `components/hal/esp32s3/include/hal/spi_ll.h`
- IDF v5.5.1 `components/esp_driver_uart/include/driver/uhci.h`
- IDF v5.5.1 `components/esp_driver_gpio/include/driver/dedic_gpio.h`
