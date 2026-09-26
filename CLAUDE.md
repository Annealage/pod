# Annealage Pod

Hardware-in-the-loop test rig. See `../CLAUDE.md` for the Annealage brand context
and naming conventions.

## Targets

- **RP2350** (canonical, current target): the pod runs the debugger
  itself in MicroPython (PIO SWD + DP/AP/MEM-AP + CMSIS FLM flash loader + an
  on-pod GDB RSP server) instead of exporting a synthetic CMSIS-DAP probe over
  USB/IP. The native USB controller is the DUT host port; pod management (REPL,
  flashing, mount) rides Wi-Fi over a socket REPL. Two interchangeable boards
  run this target, sharing one DUT-facing pin map and all frozen Python
  (`src/boards/common/`):
  - `ANNEALAGE_POD_RP2350` - Raspberry Pi Pico 2 W (RP2350A, 4 MB flash),
    flashed over SWD by a wired pico-probe.
  - `ANNEALAGE_POD_RP2350B` - Waveshare RP2350B-Plus-W (RP2350B, 16 MB flash,
    optional 8 MB QSPI PSRAM, radio on GPIO36-39), flashed over USB BOOTSEL.
    See `src/boards/ANNEALAGE_POD_RP2350B/README.md` for the hardware deltas.
- **ESP32-S3** (prior design, removed): the code and its docs are no longer in
  the tree. They're at tag `v1.6.0-native-flash-default`, eg.
  `git show v1.6.0-native-flash-default:docs/esp32-s3/spec.md`. Its
  `usbip_server.c` was adapted from adafruit/esp-usbip-bridge, which has no
  licence, so don't restore it.

## RP2350 docs (read these before working on the RP2350 target)

- `docs/pod/hardware-setup.md`: the single DUT-to-pod wiring reference (SWD,
  USB host, UART, I2C, SPI, GPIO/ADC, logic-analyser taps, power/ground), written
  to be followable by a hobbyist. Marks each interface VERIFIED vs SUGGESTED
  (untested), and lists the open hardware decisions (DUT UART/SPI/nRST pins,
  USB-host cabling/power) still needed. The other RP2350 docs link here for "what
  wires where".
- `docs/pod/dev-notes.md`: development gotchas and recipes that cost real
  debugging time. Notably: `mpremote resume` caches imported modules (re-copy AND
  `sys.modules.pop` after edits); `probe-rs download --binary-format uf2`
  mis-flashes the multi-section RP2350 UF2 (flatten to a program bin and flash as
  `--binary-format bin --base-address 0x10000000`); the SWD bit-bang turnaround
  framing.
- `docs/pod/spike-findings.md`: hardware-validated bring-up results (on-pod SWD
  DP/AP/MEM-AP, Wi-Fi dupterm TCP REPL + `ampremote` socket transport + mount) and
  the settled architecture points (native USB host, on-pod probe, mDNS service
  discovery).
- `docs/pod/debug-stack.md`: usage of the on-pod debug stack
  (`annealage_pod.debug`) - the layered SWD / DAP / nRF52-flash modules, the
  high-level `ops` entry points, hardware wiring, deployment, and the
  no-filesystem streaming flash/read.
- `docs/pod/peripherals.md`: the pod's DUT-facing peripherals
  (`annealage_pod.peripherals`) - the thin machine-passthrough philosophy and
  the curated helpers (hardware I2C target / GPIO / ADC), distinct from the SWD
  debug stack.
- `docs/pod/logic-analyser.md`: the PIO logic analyser
  (`annealage_pod.debug.logic_analyser`) + the PIO arbiter. Validated end-to-end
  including the live Wi-Fi capture round-trip (LA on PIO0; the earlier hang was
  the LA colliding with CYW43 Wi-Fi on PIO2). Includes DUT wiring + usage and the
  authoritative PIO block map (`pio_arbiter.PIO_MAP`).
- `src/host/README.md`: usage of the host `pod` tooling - the CLI, the `Pod`
  Python client, the MCP server, and the discover/register/flash/reset/read
  workflow over Wi-Fi.
- `docs/pod/troubleshooting.md`: consumer-facing recovery for a silent / stuck
  forwarded DUT REPL - the `recover_dut_repl` (Ctrl-C/Ctrl-B) un-stick, the
  ModemManager `pod install-udev` fix, and the reset/power-cycle escalation
  ladder. The two ecosystem gotchas (host ModemManager toggling DTR; DUT latched
  in RAW repl) that make a healthy DUT look dead.
- `docs/pod/plan/`: the phased development plan. Start at `overview.md` (goals,
  architecture deltas, workstreams, phase map + gates, host `pod` CLI/MCP design,
  risk register), then `phase-1-foundation.md` .. `phase-7-integration-hardening.md`.
  Cross-cutting standalone plans (not numbered phases) sit alongside and are
  indexed from `overview.md`: `conflict-legibility.md` (multi-agent holder
  attribution + anti-bump gate; the one to build first), `bench-lease.md`
  (multi-agent DUT checkout, sequenced after it), `mcp-surface.md` (MCP tool
  reorg: pod_/dut_/bench_ namespaces, session-first DUT access),
  `carrier-hardware.md` (custom-carrier feature spec), and
  `resume-after-power-cycle.md` (brick-recovery runbook).
  Dynamic plan: each phase ends at a hardware-validated gate, then the remainder is
  re-cut. Development happens on the `main` branch (the `rp2350-pivot` branch was
  promoted to `main`).

## Conventions

- Address serial devices by `/dev/serial/by-id/...` and probes by
  `VID:PID:Serial`, never by `/dev/ttyACMx` (multiple boards and CMSIS-DAP probes
  are attached at once). Use `mpy-dev list` for the registry.
- Drive MicroPython devices with `mpremote connect <by-id> resume ...`; see the
  `resume` caching caveat in `docs/pod/dev-notes.md`.
- `prototypes/` is throwaway spike code, not built into firmware. Firmware images
  (`*.uf2`, `*.bin`) are gitignored.
