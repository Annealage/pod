# windowsair register pattern: what was missing in our SPI engine

## Pattern in the canonical reference

`referencea/wireless-esp32-dap/components/DAP/source/spi_op.c`,
`DAP_SPI_Send_Header` (C3 path) and `DAP_SPI_Read_Data`:

```c
// before each MISO-bearing transaction:
DAP_SPI.user.sio = true;
SET_MISO_BIT_LEN(...);
START_AND_WAIT_SPI_TRANSMISSION_DONE();
// after:
DAP_SPI.user.sio = false;
```

`DAP_SPI_WriteBits` does not touch `sio`; it stays cleared.

`spi_switch.c` `DAP_SPI_Init` (C3 path) configures the pad with:

```c
PIN_INPUT_ENABLE(IO_MUX_GPIOn_REG);
GPIO.func_out_sel_cfg[n].oen_sel = 0;       // OE from peripheral
GPIO.func_out_sel_cfg[n].oen_inv_sel = 0;   // not inverted
PIN_FUNC_SELECT(IO_MUX_GPIOn_REG, FUNC_...);
```

Note `oen_sel = 0` is set even when using IOMUX direct. This routes
the pad output enable through the peripheral's auto-tristate signal
so the half-duplex hardware can release the pad for MISO.

## What our code was doing pre-fix

- `user.sio = 1` set once at init, never toggled.
- IOMUX direct via `PIN_FUNC_SELECT` and `PIN_INPUT_ENABLE`, but
  `func_out_sel_cfg[].oen_sel` left at whatever `spi_bus_initialize()`
  set it to.
- IOMUX pull-up not explicitly disabled.

## What we changed

In `src/c_modules/dapprobe/io/swd.c`:

- `swd_recv_bits` and `swd_phase_header_ack` now wrap
  `swd_ll_apply_and_start(hw)` with `hw->user.sio = 1` before and
  `hw->user.sio = 0` after, mirroring the C3 windowsair pattern.
- `swd_send_bits` explicitly clears `hw->user.sio = 0`.
- Init no longer leaves `sio = 1`.
- `swd_bind_swdio_iomux` now adds `PIN_PULLUP_DIS`,
  `PIN_PULLDWN_DIS`, and explicit
  `GPIO.func_out_sel_cfg[gpio].oen_sel = 0`,
  `oen_inv_sel = 0`.

## Why the fix did not move the symptom on ESP32-S3

The windowsair codebase has paths for ESP8266, ESP32-plain, and
ESP32-C3, but no path for ESP32-S3. The C3 register layout is the
closest analog to S3, but S3's SPI2 has additional fields (some
documented only in the TRM erratum and in `soc/esp32s3/include/soc/spi_struct.h`)
that may govern half-duplex pad release differently. The sio toggle
plus explicit oen_sel routing handled the C3 case but ack_raw stayed
at 0x07 on S3, indicating the pad was still being driven during the
MISO sub-window.

The bit-bang fallback (path 2) confirmed that the wire and target
are good; the failure is isolated to S3 SPI2 half-duplex direction
control. Further investigation of S3-specific register fields is the
next session's job.
