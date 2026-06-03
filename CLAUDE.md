# Annealage Pod

Hardware-in-the-loop test rig. See `../CLAUDE.md` for the Annealage brand context
and naming conventions.

## Targets

- **ESP32-S3** (original): single ESP32-S3 + ESP-IDF + MicroPython, C user modules
  for USB/IP, synthetic CMSIS-DAP-v2, UART bridge, I2C/SPI slave. The design of
  record is `docs/spec.md` and `docs/architecture.md`, with per-workstream notes
  in `docs/design/`.
- **RP2350 (Pico 2 W)**: a parallel variant in bring-up. Inverts the debug-probe
  decision, the pod runs the debugger itself in MicroPython (PIO SWD + DP/AP/MEM-AP
  + CMSIS FLM flash loader + an on-pod GDB RSP server) instead of exporting a
  synthetic CMSIS-DAP probe over USB/IP. The native USB controller is the DUT host
  port; pod management (REPL, flashing, mount) rides Wi-Fi over a socket REPL.

## RP2350 docs (read these before working on the RP2350 target)

- `docs/rp2350/dev-notes.md`: development gotchas and recipes that cost real
  debugging time. Notably: `mpremote resume` caches imported modules (re-copy AND
  `sys.modules.pop` after edits); `probe-rs download --binary-format uf2`
  mis-flashes the multi-section RP2350 UF2 (flatten to a program bin and flash as
  `--binary-format bin --base-address 0x10000000`); the SWD bit-bang turnaround
  framing.
- `docs/rp2350/spike-findings.md`: hardware-validated bring-up results (on-pod SWD
  DP/AP/MEM-AP, Wi-Fi dupterm TCP REPL + `ampremote` socket transport + mount) and
  the settled architecture points (native USB host, on-pod probe, mDNS service
  discovery).
- `docs/rp2350/debug-stack.md`: usage of the on-pod debug stack
  (`annealage_pod.debug`) - the layered SWD / DAP / nRF52-flash modules, the
  high-level `ops` entry points, hardware wiring, deployment, and the
  no-filesystem streaming flash/read.
- `docs/rp2350/peripherals.md`: the pod's DUT-facing peripherals
  (`annealage_pod.peripherals`) - the thin machine-passthrough philosophy and
  the curated helpers (hardware I2C target / GPIO / ADC), distinct from the SWD
  debug stack.
- `src/host/README.md`: usage of the host `pod` tooling - the CLI, the `Pod`
  Python client, the MCP server, and the discover/register/flash/reset/read
  workflow over Wi-Fi.
- `docs/rp2350/plan/`: the phased development plan. Start at `overview.md` (goals,
  architecture deltas, workstreams, phase map + gates, host `pod` CLI/MCP design,
  risk register), then `phase-1-foundation.md` .. `phase-7-integration-hardening.md`.
  Dynamic plan: each phase ends at a hardware-validated gate, then the remainder is
  re-cut. Development happens on the `rp2350-pivot` branch.

## Conventions

- Address serial devices by `/dev/serial/by-id/...` and probes by
  `VID:PID:Serial`, never by `/dev/ttyACMx` (multiple boards and CMSIS-DAP probes
  are attached at once). Use `mpy-dev list` for the registry.
- Drive MicroPython devices with `mpremote connect <by-id> resume ...`; see the
  `resume` caching caveat in `docs/rp2350/dev-notes.md`.
- `prototypes/` is throwaway spike code, not built into firmware. Firmware images
  (`*.uf2`, `*.bin`) are gitignored.
