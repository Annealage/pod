# Phase 1: Foundation and remaining spikes

Workstreams F (firmware foundation) + D (debug probe, PIO SWD slice).

Goal: a buildable custom RP2350 board firmware, a productionised Wi-Fi management
transport with discovery, and the two remaining hardware feasibility spikes
retired (PIO SWD at speed, multi-engine PIO/core coexistence).

## Dependencies

- Phase 0 spike results (`docs/pod/spike-findings.md`).
- `src/micropython` submodule, branch `machine-usbhost` (currently uninitialised;
  init and confirm it carries rp2 native USB host).

## Tasks

### F1.1 Board variant and build
- Init the `machine-usbhost` submodule; confirm rp2 + native USB host support.
- Create a `RPI_PICO2_W`-derived board variant under the rp2 port (or a board dir
  in this repo wired via the port's board path), enabling: CYW43 Wi-Fi, native USB
  **host** mode, the flash/FS layout, frozen `annealage_pod` package.
- Reproducible build (document the toolchain; ephemeral-container build per the
  monorepo convention).
- Flash recipe by probe serial (see `dev-notes.md` §2): build artifact is a UF2;
  flatten to program bin for SWD flashing, or BOOTSEL for first provisioning.

### F1.2 Wi-Fi + socket REPL, productionised
- Promote the spike `netrepl.py` to a supervised service: Wi-Fi connect with
  auto-reconnect, `os.dupterm` TCP REPL with clean client teardown and re-accept,
  credentials from a config file (not hardcoded; mirror `credentials.example.json`).
- Cleanup hook on REPL disconnect (DUT-safe default), per S3 spec §5.4.

### F1.3 mDNS service discovery
- Advertise `_annealage-pod._tcp` with TXT records: `repl-port`, `usbip-port`,
  `uart-port`, `carrier-id`, `firmware-version`, `mp-version`.
- Verify browsable from the host (`avahi-browse`, and the `pod` discovery slice).

### D1.1 PIO SWD port
- Port the `pico_debug` `swd.pio` dispatch model (FIFO words as jump-table calls;
  in-state-machine ACK check) to `rp2.asm_pio`. Cover output/input/short-output
  and the conditional ACK-check routine.
- Re-derive turnaround timing for PIO (do not assume the bit-bang counts from
  `dev-notes.md` §3 carry over).
- Re-validate against the nRF52840 and an RP2040/RP2350 target: DPIDR, DP
  power-up, MEM-AP read of CPUID, matching the bit-bang results. Add RP multidrop
  (dormant + TARGETSEL) validation on an RP target.
- Measure SWD clock and per-frame time; target useful clock (>= 10 MHz, aim
  higher) with reliable ACK handling.

### D1.2 PIO/core coexistence spike
- Run native USB host (enumerate any device), CYW43 Wi-Fi (socket REPL active),
  and PIO SWD (continuous MEM-AP reads) simultaneously; confirm PIO state-machine
  and core budget hold, and SWD reliability is unaffected by Wi-Fi/USB activity.
- Record PIO block/SM allocation and which work runs on core1.

## Deliverables

- Custom board firmware that builds and boots, Wi-Fi up, discoverable, socket
  REPL with reconnect.
- `swd_pio.py` (or a small C/PIO module) reading DPIDR/MEM-AP at speed, validated
  on >= 2 target families.
- A short coexistence report appended to `spike-findings.md`.

## Exit gate

Custom board builds and boots; PIO SWD reads DPIDR and MEM-AP at target speed on
nRF52840 and an RP target; native-USB-host + CYW43 + PIO-SWD coexist without
degradation; the pod is discoverable via mDNS and reachable over socket REPL with
auto-reconnect.

## Risks

- R1 (PIO SWD speed/reliability), R2 (native USB host maturity), R3 (PIO/core
  budget). See overview risk register for fallbacks.

## References

- `github.com/essele/pico_debug` (`swd.pio`, `swd.c`)
- `v1.6.0-native-flash-default:docs/esp32-s3/design/swd-swo-engine.md` (SWD frame protocol, reusable at the protocol
  level), `docs/pod/dev-notes.md` §3
- `prototypes/rp2350-swd-spike/swd_bitbang.py`
