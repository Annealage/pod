# Phase 5: Peripherals, reset, API

Workstream F. The remaining DUT-facing capabilities, several reused from the
ESP32-S3 variant.

Goal: PIO I2C/SPI-target personalities, DUT UART over TCP, the `swd`/`nrst` reset
paths, and the RP_INFRA-compatible MP API on the bare Pico 2 W. INA228 telemetry
and the `power` reset path are gated on a future custom carrier.

## Dependencies

- F1 (transport); shares code with the S3 variant where possible.

## Tasks

### F5.1 PIO I2C/SPI target personalities
- Implement I2C-target and SPI-target responders on PIO with the S3 slaveio
  register-table model (`docs/design/slaveio.md`): two flat buffers per
  personality (read-table / write-table), ISR-free PIO path, MP-side read/write
  and on-write-range callbacks. PIO target is the reason the RP2350 is expected to
  be more reliable than the S3 i2c-target.
- I2C address / SPI mode / freq cap configurable from MP; personalities mutually
  exclusive on shared pins.

### F5.2 UART bridge over TCP
- DUT UART forwarded over a TCP socket (default port per mDNS TXT). Hardware UART
  or PIO UART depending on the Phase 1 pin/PIO allocation. Configurable
  baud/parity/bits.

### F5.3 Power telemetry (INA228) - custom carrier, deferred
- Not present on the bare Pico 2 W. When a custom carrier exists, reuse the S3
  INA228 driver (I2C, portable): per-rail current/voltage for VTARGET and DUT-USB
  VBUS; vbus-present detection. MP API per S3 spec §7.2.

### F5.4 Reset integration
- Integrate the reset paths (Phase 3 D3.3) into the unified
  `annealage_pod.dut.reset(mode=...)` API: `swd` and `nrst` on the bare Pico 2 W;
  `power` when custom carrier hardware is available.

### F5.5 RP_INFRA API mimicry
- Provide the RP_INFRA-equivalent surface (S3 spec §7.1, appendix B) so
  testbed_micropython needs only a transport adapter. Share the package with the
  S3 variant against the platform-capability interface (overview §5).

## Deliverables

- PIO I2C/SPI target modules with the register-table API.
- UART-over-TCP bridge.
- Unified reset API (`swd`/`nrst`; `power` on custom carrier). INA228 telemetry
  deferred to the custom carrier.
- The `annealage_pod` package presenting the RP_INFRA-compatible surface on
  RP2350.

## Exit gate

A DUT-as-master test drives the pod's I2C/SPI target; UART forwards over TCP; the
`swd` and `nrst` reset paths work; the RP_INFRA API surface is present. INA228
telemetry and the `power` reset path are validated when custom carrier hardware
exists.

## References

- `docs/design/slaveio.md`, `docs/design/uartbridge.md`
- `docs/design/annealage-pod-package.md`, S3 spec §7, appendix B
- `src/mpy/annealage_pod/` (existing package to share/factor)
