# dapprobe (WS-C): CMSIS-DAP-v2 design notes

Companion to `src/c_modules/dapprobe/`. Phase 2 WS-C output: vendored
ARM-software CMSIS-DAP protocol layer wired to WS-D's SWD/SWO engine and
WS-A's USB/IP virtual-device registry.

## 1. Vendoring inventory

Vendored from <https://github.com/ARM-software/CMSIS-DAP> at revision
`6256803b7ac93731ec22e24e0ae8d91df3a7c953` (2026-04-29 import).
License: Apache-2.0; full text at
`src/c_modules/dapprobe/vendor/cmsis-dap/LICENSE`.

| File in tree | Upstream path | Built? | Notes |
|---|---|---|---|
| `vendor/cmsis-dap/Source/DAP.c` | `Firmware/Source/DAP.c` | yes | Wire-level CMSIS-DAP command dispatcher |
| `vendor/cmsis-dap/Source/DAP_vendor.c` | `Firmware/Source/DAP_vendor.c` | yes | Strong default for `DAP_ProcessVendorCommand`; overrides the `__WEAK` in DAP.c |
| `vendor/cmsis-dap/Source/SWO.c` | `Firmware/Source/SWO.c` | no | Targets the CMSIS USART driver; replaced by `port/swo_glue.c` |
| `vendor/cmsis-dap/Source/SW_DP.c` | `Firmware/Source/SW_DP.c` | no | GPIO bit-bang only; replaced by `port/swd_glue.c` (SPI2-DMA via WS-D) |
| `vendor/cmsis-dap/Source/JTAG_DP.c` | `Firmware/Source/JTAG_DP.c` | no | DAP_JTAG=0; never linked |
| `vendor/cmsis-dap/Source/UART.c` | `Firmware/Source/UART.c` | no | DAP_UART=0; target UART bridged via uartbridge |
| `vendor/cmsis-dap/Include/DAP.h` | `Firmware/Include/DAP.h` | header | Includes `cmsis_compiler.h` |
| `vendor/cmsis-dap/Config/DAP_config.h` | `Firmware/Config/DAP_config.h` | no | Reference template; the build copy is `port/DAP_config.h` |

License headers were preserved byte-for-byte. No vendored file was
modified. The Apache-2.0 obligation to ship a copy of the licence is
met by `vendor/cmsis-dap/LICENSE`.

## 2. Port glue

`port/` contains the local replacements and shims:

| File | Role |
|---|---|
| `port/DAP_config.h` | Active build copy. Replaces upstream `Firmware/Config/DAP_config.h` on the include path. Hooks `PIN_*`, `LED_*`, `DAP_GetVendorString` etc. to function pointers in `swd_glue.c`. |
| `port/cmsis_compiler.h` | Local 30-line shim mapping `__STATIC_INLINE` / `__STATIC_FORCEINLINE` / `__WEAK` / `__NOP` / `__ASM` to GCC equivalents. Avoids pulling the full CMSIS chain on xtensa GCC. |
| `port/swd_glue.c` | Replacement for `vendor/cmsis-dap/Source/SW_DP.c`. Exposes `SWD_Transfer`, `SWJ_Sequence`, `SWD_Sequence` in terms of WS-D's `swd_transfer()`. |
| `port/swo_glue.c` | Replacement for `vendor/cmsis-dap/Source/SWO.c`. Provides the seven CMSIS-DAP `SWO_*` entry points (Transport / Mode / Baudrate / Control / Status / ExtendedStatus / Data) wrapping WS-D's `swo_init` / `swo_start` / `swo_stop` / `swo_read`. |

## 3. DAP_config.h compile-time choices

| Macro | Value | Reasoning |
|---|---|---|
| `DAP_SWD` | 1 | rev1 in scope. |
| `DAP_JTAG` | 0 | Out of scope; saves protocol-layer code and avoids needing JTAG_DP / JTAG_Sequence. |
| `DAP_PACKET_SIZE` | 512 | Matches windowsair and probe-rs's expectation; the wire-level USB MaxPacket is still 64 (FullSpeed). The host fragments larger packets across multiple Bulk-OUT URBs. |
| `DAP_PACKET_COUNT` | 4 | Standard CMSIS-DAP default. |
| `SWO_UART` | 1 | rev1 SWO scope is UART-only. |
| `SWO_MANCHESTER` | 0 | No real host emits Manchester. |
| `SWO_BUFFER_SIZE` | 8 MiB (`8388608`) | Matches spec.md §4.7's tier-2 PSRAM ring sizing. Power-of-two required by `DAP_Info`. |
| `SWO_UART_MAX_BAUDRATE` | 6 Mbps | spec.md §4.7 design target. |
| `TIMESTAMP_CLOCK` | 0 | rev1 does not implement the timestamp register; pyOCD/probe-rs cope. |
| `DAP_UART` | 0 | DAP UART (DAP_UART_Transfer command set) is not the same as the target UART; we forward the latter via `uartbridge` instead. |

## 4. Synthetic device descriptor stack

Per `research/usbip-multiplexing-design.md` §2:

- `bcdUSB = 0x0210` so BOS exists (Windows MS-OS-2.0 hook).
- `bDeviceClass / bDeviceSubClass / bDeviceProtocol = 0xEF / 0x02 / 0x01`
  (composite-IAD-friendly triple).
- `idVendor = 0xC251` (Keil), `idProduct = 0xF00A` (windowsair-shared).
  Both informational; Linux match path keys on iInterface.
- One configuration, one interface, three Bulk endpoints in canonical
  order: EP1 OUT (0x01), EP2 IN (0x82), EP3 IN (0x83). All with
  wMaxPacketSize=64 (FullSpeed).
- iInterface = "CMSIS-DAP" exactly. Both pyOCD's
  `_match_cmsis_dap_v2_interface` and probe-rs's `is_cmsis_dap` check
  for this substring; the device is invisible to them otherwise.
- iManufacturer = "mpy-pod", iProduct = "mpy-pod synthetic CMSIS-DAP",
  iSerial = lower 12 hex of `esp_efuse_mac_get_default()`.

BOS / MS-OS-2.0 in Phase 2 is a 5-byte stub (no platform capabilities).
Linux hosts ignore that cleanly. A future commit can lift the
windowsair MS-OS-2.0 blob for Windows WinUSB auto-binding.

## 5. URB dispatch

| Endpoint | Handler |
|---|---|
| EP0 OUT/IN | `synthetic_device.c::control_transfer`: GET_DESCRIPTOR (device, config, string, BOS), SET/GET_CONFIGURATION, SET/GET_INTERFACE, GET_STATUS, CLEAR/SET_FEATURE, SET_ADDRESS. STALL (-EPIPE) on unknown vendor requests. |
| EP1 OUT (0x01) | `handle_ep1_out`: copy bytes into `s_dap_cmd_buf`, call `dap_core_process` synchronously, park the response in `s_dap_resp_buf`. |
| EP2 IN (0x82) | `handle_ep2_in`: drain `s_dap_resp_buf` into the URB's IN buffer. Returns 0 bytes if no response is queued; the host re-submits. |
| EP3 IN (0x83) | `handle_ep3_in`: `dap_core_swo_read` capped at 60 bytes per completion (probe-rs #448 ZLP discipline). |

The CMSIS-DAP-v2 spec says EP3 is the third bulk-IN on the same
interface, not a separate interface. Confirmed via
`vendor/cmsis-dap/...` and the windowsair reference; both list three
endpoints under interface 0.

### 5.1 ZLP / probe-rs #448 handling

At FullSpeed `wMaxPacketSize=64`, every SWO Bulk-IN completion that
ends on an exact 64-byte boundary stalls some hosts (probe-rs #448).
We cap each EP3 completion at 60 bytes; the host's next Bulk-IN
drains whatever is left. The cost of the cap is one extra round trip
per ~60 bytes of trace; at 6 Mbps SWO that's about 12 k extra round
trips per second, negligible against the 100 k+ URB rate the kernel
issues anyway.

## 6. Threading and buffer ownership

`dap_core_process` is invoked from the per-USB/IP-connection
`client_task` (APP_CPU, priority 5) via the dapprobe `data_transfer`
op. It runs synchronously to completion: a CMSIS-DAP command typically
takes microseconds (DAP_Info), or up to ~1 ms for a worst-case
DAP_TransferBlock at the rev1 25 MHz SWD clock.

The cmd / response buffers (`s_dap_cmd_buf`, `s_dap_resp_buf`) are
single-owner: only the same `client_task` writes and reads them. The
USB/IP server already serialises per-busid via the `attachment_acquire`
table; only one connection ever owns busid 2-1 at a time. No mutex
needed.

The SWO ring is the producer-consumer between WS-D's `swo_drain_task`
(producer) and the dapprobe data_transfer EP3 path (consumer). That
synchronisation lives inside `swo_read` in WS-D's swo.c; dapprobe
treats `swo_read` as thread-safe by contract.

## 7. MP API

```python
import dapprobe
dapprobe.attach()                # registers with usbip server's bus 2
dapprobe.is_attached()           # bool
dapprobe.swd_clock_hz()          # current realised SWD clock
dapprobe.swo_overruns()          # monotonic tier-1->tier-2 overrun count
dapprobe.transfers_total()       # monotonic SWD transfer count
dapprobe.swo_bytes_buffered()    # tier-1 + tier-2 unread bytes
dapprobe.detach()                # soft detach
```

`dapprobe.detach()` is a flag-flip only; the current usbip
virtual-device registry is append-only, so the descriptor stays
registered until firmware reboot. Phase 3 may add a real unregister
entry to the registry if the use case warrants it.

## 8. Open issues for Phase 3

1. SWJ_Sequence / SWD_Sequence currently route to swd_line_reset() or
   are stubbed. WS-D's swd.h does not expose a "send N raw bits"
   primitive; extending it is the right fix once WS-D opens. pyOCD
   and probe-rs only emit canonical line-reset bit patterns on ARM
   targets, so the gap is invisible in practice.
2. MS-OS-2.0 descriptor blob is a stub. Lift the windowsair
   `bosDescriptor[33]` and `msOs20DescriptorSetHeader[162]` for
   Windows WinUSB auto-binding when Windows hosts enter scope.
3. `dap_core_detach()` is soft. WS-A's virtual_device.h has no
   unregister entry; either add one (small change in WS-A) or
   accept "until reboot" for rev1.
4. EP1 OUT is treated as one CMSIS-DAP command per Bulk-OUT packet
   (`research/usbip-multiplexing-design.md` §3 confirms CMSIS-DAP-v2
   does not span commands across packets). The buffer cap is 1 KiB to
   match `DAP_PACKET_SIZE` plus slack; if a future host pushes a
   larger packet the EP1 handler returns -EPIPE.
5. Timestamp support (TIMESTAMP_CLOCK > 0) requires a free-running
   counter the SWD path can read in nanoseconds. Phase 3 task.
