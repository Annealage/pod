# Vendored ARM-software/CMSIS-DAP

Source: <https://github.com/ARM-software/CMSIS-DAP>
Vendored revision: `6256803b7ac93731ec22e24e0ae8d91df3a7c953` (2026-04-29 import)
License: Apache-2.0 (see `LICENSE`).

## Files in this directory

| Path | Upstream path | Built into firmware? |
|---|---|---|
| `Source/DAP.c` | `Firmware/Source/DAP.c` | Yes |
| `Source/DAP_vendor.c` | `Firmware/Source/DAP_vendor.c` | Yes (weak default `DAP_ProcessVendorCommand`) |
| `Source/SWO.c` | `Firmware/Source/SWO.c` | No (replaced by `port/swo_glue.c`) |
| `Source/SW_DP.c` | `Firmware/Source/SW_DP.c` | No (replaced by `port/swd_glue.c`) |
| `Source/JTAG_DP.c` | `Firmware/Source/JTAG_DP.c` | No (DAP_JTAG=0) |
| `Source/UART.c` | `Firmware/Source/UART.c` | No (DAP_UART=0) |
| `Include/DAP.h` | `Firmware/Include/DAP.h` | Yes (header) |
| `Config/DAP_config.h` | `Firmware/Config/DAP_config.h` | No (the active build copy is `port/DAP_config.h`) |
| `LICENSE` | `LICENSE` | n/a |

The reason for vendoring SWO.c and SW_DP.c despite not building them is that the
spec (§4.6 and §4.7) names these as the firmware base; keeping them in tree as
reference makes future audits trivial.

## Modifications to vendored sources

None. Files are byte-for-byte upstream copies. License headers are preserved.

## Local glue

Port-specific replacements live in `../../port/`:

- `port/DAP_config.h`: replaces upstream `Config/DAP_config.h` on the include
  path. Hooks `PIN_*`, `LED_*`, `DAP_GetVendorString` etc. to the WS-D engine
  and to compile-time strings.
- `port/swd_glue.c`: replaces `Source/SW_DP.c`. Provides `SWD_Transfer`,
  `SWJ_Sequence`, `SWD_Sequence` in terms of WS-D's `swd_transfer()`.
- `port/swo_glue.c`: replaces `Source/SWO.c`. Provides the seven `SWO_*`
  CMSIS-DAP entry points in terms of WS-D's `swo_init`/`swo_start`/
  `swo_stop`/`swo_read`.
- `port/jtag_stub.c`: stubs `JTAG_*` entry points so DAP.c links with
  DAP_JTAG=0 still referencing them through function-pointer paths.

See `docs/esp32-s3/design/cmsis-dap.md` for the full vendoring inventory and design
rationale.
