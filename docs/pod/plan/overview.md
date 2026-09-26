# RP2350 pod: development plan (overview)

Master plan for the RP2350 (Pico 2 W), the canonical, current Annealage Pod
target. This is a dynamic, risk-ordered plan: each phase ends at a decision gate
that is validated on hardware before the next phase commits, and the plan is
re-cut as spikes land. It supersedes the prior ESP32-S3 design captured in
`v1.6.0-native-flash-default:docs/esp32-s3/spec.md` and `v1.6.0-native-flash-default:docs/esp32-s3/architecture.md`.

Read first: `docs/pod/spike-findings.md` (what is already proven on hardware)
and `docs/pod/dev-notes.md` (gotchas and recipes).

## 1. Goal and shape

A single RP2350 (Pico 2 W) hosting MicroPython, reachable entirely over Wi-Fi,
that:

- hosts a DUT on the **native USB controller** and exports it to a host PC via
  USB/IP over Wi-Fi;
- acts as its own **SWD debug probe and flasher** in MicroPython (PIO SWD +
  DP/AP/MEM-AP + CMSIS FLM flash loader + an on-pod GDB RSP server), instead of
  exporting a synthetic CMSIS-DAP probe;
- forwards DUT UART over TCP;
- presents an I2C-target personality (hardware I2C; a SPI target is planned
  and cuttable for GA);
- on a custom carrier (deferred; current development is on a bare Pico 2 W):
  per-rail current/voltage telemetry (INA228);
- is discovered and driven by host-side `pod` tooling (a CLI and an MCP server)
  built on `ampremote`.

Hardware scope: current development targets a bare Pico 2 W with jumper wires to a
DUT. Carrier-hardware-dependent capabilities (INA228 telemetry, power-rail
switching, level translation) are gated on a future custom PCB and are called out
as such per phase. Opto-relays are out of scope for this design.

The RP2350 is the canonical target; the prior ESP32-S3 design is frozen and
receives no new features. Maximise shared code (MP package, USB/IP protocol
logic, RP_INFRA API mimicry, INA228, slave register-table model); accept
divergence in the silicon backends.

## 2. Why this shape (architecture deltas from ESP32-S3)

| Concern | ESP32-S3 | RP2350 | Rationale |
|---|---|---|---|
| Debug probe | synthetic CMSIS-DAP-v2 over USB/IP, host pyOCD drives | a debug-probe subset runs **on the pod**, drives SWD locally | removes the CMSIS-DAP-over-USB/IP latency stack-up the S3 spec flagged as a pivot trigger (§9.2) |
| SWD I/O | SPI2 + GDMA register-direct | PIO state machine | PIO is the native fit on RP2350; `pico_debug` proves the approach |
| USB host | USB-OTG (TinyUSB), full speed | native USB controller, host mode | native host is more reliable than Pico-PIO-USB and consumes no PIO |
| Pod management | console UART + Wi-Fi REPL | Wi-Fi socket REPL only | native USB is spoken for by the DUT host role |
| I2C/SPI target | ESP32 hardware peripheral (documented limits) | PIO | PIO target is more reliable than the S3 i2c-target |
| Discovery | mDNS hostname + one service record | browsable mDNS **service** with TXT (ports, identity) | every interaction is network-side; tooling enumerates pods by service type |
| Host tooling | ad hoc scripts | `pod` CLI + MCP server over `ampremote` | a registry-backed, Claude-drivable control plane |

PIO/core budget after these deltas: native USB host = 0 PIO; CYW43 Wi-Fi ≈ 1
PIO-SPI state machine; leaving the bulk of the 3 PIO blocks / 2 Cortex-M33 cores
for PIO SWD, SWO capture, and the I2C/SPI-target engines. Headroom is larger than
a Pico-PIO-USB design, but coexistence is still a phase-1 spike.

## 3. Workstreams

Three workstreams progress in parallel, gated by dependencies rather than run
strictly in series:

- **F (firmware foundation)**: board variant, native USB host, Wi-Fi + socket
  REPL, mDNS service, supervisor.
- **D (debug probe)**: PIO SWD, DP/AP/MEM-AP, FLM flash loader, target data, GDB
  server, SWO. The core differentiator and the deepest risk.
- **H (host tooling)**: the `pod` library, CLI, registry, and MCP server. Grows
  as firmware capabilities land; usable from the first transport milestone.

## 4. Phase map and gates

Phase 0 is done (`spike-findings.md`): on-pod SWD bit-bang link (DP/AP/MEM-AP) and
Wi-Fi dupterm transport + `ampremote` socket/mount, both proven on hardware.

| Phase | Title | Workstream | Exit gate (hardware-validated) |
|---|---|---|---|
| 1 | Foundation and remaining spikes | F + D | Custom board builds; PIO SWD reads DPIDR/MEM-AP at target speed; native-USB-host + CYW43 + PIO-SWD coexist; pod discoverable via mDNS and reachable over socket REPL with reconnect |
| 2 | Debug-probe stack | D | Flash + verify a real DUT image over the network via the on-pod FLM loader |
| 3 | GDB server and debug control | D | A host `gdb`/`pyocd`/`probe-rs` session halts, steps, sets breakpoints, reads memory on a DUT through the pod |
| 4 | DUT USB host + USB/IP | F + D | Host PC enumerates the DUT through `usbip attach` over Wi-Fi |
| 5 | Peripherals, reset, API | F | I2C target (landed, hardware I2C), SPI target (cuttable for GA, see phase 5), UART-over-TCP, `swd`/`nrst` reset, RP_INFRA API mimicry exercised on bare Pico 2 W; INA228 telemetry + `power` reset gated on custom carrier |
| 6 | Host tooling: `pod` CLI + MCP | H | Claude drives a pod+DUT (discover, flash, reset, telemetry, UART, USB/IP, gdb) through the MCP server |
| 7 | Integration, CI, self-update, hardening | all | testbed_micropython runs against a pod; reliability run passes; pod self-update path works |

Dependency notes:
- D2 depends on D1 (PIO SWD at speed) and F1.
- D3 depends on D2 (memory + flash control primitives).
- H starts at F1 (discovery + REPL + mount + telemetry) and gains DUT-flash,
  USB/IP, and gdb tools as D2/D3/Phase-4 land.
- Phase 4 (USB/IP) is independent of the debug-probe stack but is sequenced after
  Phases 2-3 (decision 3).

Dynamic-workflow rule: at each gate, record the measured result, then re-cut the
remaining phases. Spikes that fail their gate trigger a documented fallback (see
the risk register) rather than silent scope creep.

Cross-cutting plans (standalone docs in this dir, not a single numbered phase):
- `bench-lease.md` - a time-boxed, named lease over the DUT so multiple agents
  sharing one pod do not collide on the SWD singleton / usbip / peripherals
  (a host-tooling CLI/MCP gate over a small pod-side firmware authority).
  Conditional: build only if the phases before it leave a gap - assessed
  2026-09-03, deferred (see `conflict-legibility.md` § Relationship to
  bench-lease.md).
- `conflict-legibility.md` - naming the holder of each contended pod resource and
  refusing the host-side operations that displace an incumbent (caller identity, a
  pod-side holder record on a control port, an anti-bump gate, and an SWD
  re-entrancy guard). The layer beneath `bench-lease.md`.
- `mcp-surface.md` - reorganising the 40-tool MCP surface into three subject-first
  namespaces (`pod_` / `dut_` / `bench_`) at 27 tools (28 as of `dut_flm`,
  2026-09-04), with the persistent DUT session as the default path and the
  one-shot named as such. A single pre-announcement cutover with no aliases;
  the CLI nests onto the same taxonomy in the same pass.
- `carrier-hardware.md` - the desirable-features spec for a future custom carrier
  PCB (power switching + remote power-cycle recovery, INA228 telemetry, DUT-USB
  power control/measurement, level translation, connectors). Draft, gated on the
  software reaching GA; the counterpart to the deferred-capabilities list above.
- `resume-after-power-cycle.md` - the XIP-wedge brick-recovery runbook from the
  2026-06 incident. Resolved; kept as historical reference, not an active plan.
- `spi-la-concurrency.md` - making the SPI target and the logic analyser coexist on
  PIO0 (per-SM arbiter claims + per-program PIO teardown), so an agent can watch the
  bus the pod is driving. Phases A-D landed and hardware-validated (`4ff1b77`);
  Phase E's SWD-puppet byte-pattern cross-check is the one piece left to confirm.
- `arepl-dupterm-upstream.md` - the socket-REPL park: its mechanism, why both
  local fork branches are superseded or on hold, and the open scope decision.
  Also carries the recompose gates for getting any upstream fix into the pod's
  firmware, and the separate by-design limitation that a busy device cannot be
  interrupted over the socket REPL at all.
- `cmsis-flash-completion.md` - finished the generic CMSIS-FLM flash path and
  deleted the hand-written native nRF52 driver it was a stand-in for. Covered the
  FLM resume bug that left the DUT parked on the algorithm's breakpoint, the
  unvalidated host pack-to-algo layer, the dropped FlashDevice timeouts, and the
  pack-versus-registry geometry question. The generic CMSIS-FLM runner is now the
  only flash backend; `flash_nrf52.py` and the `loader` parameter are gone.

### Agent-surface track (execution order)

`mcp-surface.md`, `conflict-legibility.md`, and `bench-lease.md` are one programme of
work on the agent-facing surface, executed in this order. The numbering is global to
the track and independent of the numbered project phases above; each phase's detail
and gate live in the doc named beside it.

| # | phase | doc |
| --- | --- | --- |
| 1 | MCP + CLI surface cutover: `pod_`/`dut_`/`bench_` namespaces, merges, plumbing collapsed, no aliases | `mcp-surface.md` |
| 2 | Session rework: session ids, concurrent pod + DUT sessions, `dut_open` absorption, `dut_exec` session reuse | `mcp-surface.md` |
| 3 | Doc sweep for phases 1-2 | `mcp-surface.md` |
| 4 | Caller identity + failure classification: busy is no longer indistinguishable from a dead pod | `conflict-legibility.md` |
| 5 | Pod-side holder record on the 8267 control port, surfaced through `pod_info` | `conflict-legibility.md` |
| 6 | Anti-bump gate: the host-side operations that displace an incumbent are refused, with `force` | `conflict-legibility.md` |
| 7 | SWD re-entrancy guard + the sticky-holder window for one-shot sequences | `conflict-legibility.md` |
| 8 | Bench lease (conditional): reservation ahead of use, only if 1-7 leave a gap - **assessed 2026-09-03: deferred, see `conflict-legibility.md` § Relationship to bench-lease.md** | `bench-lease.md` |

Phases 2 and 5-7 need the live pod; 1, 3, 4 and most of 6 are host-side. Phase 4 is
name-independent and can move earlier if the busy-versus-dead confusion bites before
the reorg lands.

## 5. Code-sharing strategy with the ESP32-S3 variant

The ESP32-S3 variant has since been removed from the tree, so this section only records the original plan.

Shared (single source, both variants):
- the `annealage_pod` MicroPython package surface and RP_INFRA API mimicry
  (`v1.6.0-native-flash-default:docs/esp32-s3/spec.md` §7, appendix B);
- USB/IP protocol logic (framing, OP_REQ/REP, CMD_SUBMIT validation) factored
  away from the ESP-IDF/lwIP specifics;
- INA228 driver, slave register-table model, reset abstractions.

Variant-specific:
- SWD/SWO engine (SPI2+GDMA vs PIO), USB host backend (USB-OTG vs native),
  transport bring-up (ESP-IDF Wi-Fi/mDNS vs CYW43/lwIP/mDNS), build system
  (ESP-IDF + board variant vs rp2 port + board variant).

Factoring target: the MP package depends on a thin platform-capability interface;
each variant provides its backend. Where the S3 implemented a capability in a C
module, the RP2350 may implement it in MicroPython (debug probe) or PIO.

## 6. Host tooling: `pod` CLI and MCP server

`mpy-dev` has been the high-value tool for local device work; `pod` is its
network-native sibling for pods, with an MCP frontend so Claude can drive
hardware directly.

Layering:
- **`pod` core library (Python)**: pod discovery (mDNS service browse), a local
  registry (`~/.config/pod/pods.json`, mirroring `mpy-dev`'s registry idea), and a
  control client built on `ampremote` (socket transport, mount, exec, file
  transfer). All higher functions are methods on this library.
- **`pod` CLI**: thin frontend. Subcommands (provisional): `discover`/`list`,
  `info`, `repl`, `mount`, `flash <dut-image> [--target ...]` (drives the on-pod
  FLM flasher), `reset [--mode swd|nrst|power]`, `telemetry`, `uart`
  (tail/bridge), `usbip` (attach helper), `gdb` (proxy to the on-pod GDB server),
  `exec`, `cp`. (`telemetry` and `--mode power` need custom carrier hardware.)
- **`pod` MCP server**: the `pod-mcp` console script starts an MCP (stdio) server
  over the same core library. Tool names and CLI verbs are derivable from each
  other: `pod_discover`, `pod_info`, `pod_mount`, `dut_flash`, `dut_reset`,
  `dut_exec`, `dut_link`, `dut_gdb`, `bench_uart`, `read_telemetry`. Leverages
  `ampremote` for all transport. The full surface is in `mcp-surface.md`.

Design constraints:
- discovery-first: never require a hardcoded IP; resolve pods by mDNS service +
  registry label.
- the MCP server is the contract by which an agent iterates on DUT firmware: edit
  -> `dut_flash` -> `dut_reset` -> observe (`bench_uart`, `read_telemetry`,
  `dut_open` + `session_send`) -> repeat.
- host tooling never reimplements transport; `ampremote` is the single transport
  dependency.

Detailed design lands in `phase-6-host-tooling-mcp.md`; an early discovery +
REPL + mount + telemetry slice is usable from Phase 1.

## 7. Decisions

Settled (2026-05-30):

1. **USB/IP server**: a C user module on the rp2 port, factoring the S3 `usbip`
   protocol logic, over native TinyUSB host. Reuse and performance over
   hackability.
2. **Flashing**: both a general CMSIS FLM loader and an RP-native bootrom fast
   path. Phase 2 leads with the already-wired nRF52840's native NVMC to prove the
   flash-control path, then the general FLM loader, then the RP-native fast path.
3. **Phase order**: the debug/flash stack (Phases 2-3) comes before DUT-USB /
   USB/IP (Phase 4). The on-pod debugger is the differentiator and SWD is proven.
4. **Discovery**: browsable `_annealage-pod._tcp` service + TXT records, plus the
   `pod` CLI's local registry. No central hub.

Deferred:

5. **Pod self-update mechanism**: decided at Phase 7 (rp2 has no esp-style OTA;
   candidates: A/B partitions + bootloader shim, update-over-mount,
   BOOTSEL-assisted recovery).

## 8. Risk register

| ID | Risk | Trigger / detection | Fallback |
|---|---|---|---|
| R1 | PIO SWD cannot hit useful clock with reliable ACK-in-PIO | Phase 1 PIO spike below ~10 MHz or unreliable | keep bit-bang for correctness, optimise hot paths; cap clock |
| R2 | native USB host immature on rp2 / machine-usbhost branch | Phase 1/4 enumeration failures | fall back to Pico-PIO-USB host (costs PIO + a core) |
| R3 | PIO/core budget overflows when everything coexists | Phase 1 coexistence spike | drop concurrent personalities; time-multiplex; move a function to the second core |
| R4 | FLM blob loader does not generalise across target families | Phase 2 multi-target flashing | RP-native fast path + per-family native-NVM (MCU_Flasher style) for the rest |
| R5 | GDB server too heavy for MicroPython RAM | Phase 3 | trim to the subset pico_debug implements; lazy features |
| R6 | Wi-Fi RTT jitter degrades USB/IP or gdb under load | Phase 4/7 reliability | the on-pod-probe design already removes SWD from the network hot path; for USB/IP, document lab-network requirement |

## 9. Pointers

- `docs/pod/spike-findings.md`, `docs/pod/dev-notes.md`
- ESP32-S3 baseline: `v1.6.0-native-flash-default:docs/esp32-s3/spec.md`, `v1.6.0-native-flash-default:docs/esp32-s3/architecture.md`, `v1.6.0-native-flash-default:docs/esp32-s3/design/`
- On-device probe reference: `github.com/essele/pico_debug`
- Spike code: `prototypes/rp2350-swd-spike/`
- Per-phase detail: `phase-1-foundation.md` ... `phase-7-integration-hardening.md`
