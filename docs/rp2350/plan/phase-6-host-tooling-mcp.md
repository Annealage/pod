# Phase 6: Host tooling - `pod` CLI and MCP server

Workstream H. The network-native sibling of `mpy-dev`, plus an MCP frontend so an
agent can drive hardware directly. Develops in parallel from Phase 1; this phase
completes it.

Goal: Claude drives a pod + DUT (discover, flash, reset, telemetry, UART, USB/IP,
gdb) through the `pod` MCP server, all over `ampremote`.

## Dependencies

- F1 (discovery, socket REPL, mount) for the early slice.
- D2 (flash), D3 (gdb), Phase 4 (USB/IP), Phase 5 (UART; telemetry on custom carrier) for the
  full tool set. Tools light up as those capabilities land.

## Design

Three layers (overview §6):

### H6.1 `pod` core library (Python)
- **Discovery**: browse the `_annealage-pod._tcp` mDNS service; resolve label ->
  pod (address + ports + identity from TXT).
- **Registry**: `~/.config/pod/pods.json`, mirroring `mpy-dev`'s registry model
  (labels, last-seen address, carrier-id, notes, links). Reconcile registry with
  live discovery.
- **Control client**: built on `ampremote` (socket transport, exec, file
  transfer, mount). One client object per pod; all functions are methods. No
  transport reimplementation.
- Functions: `discover()`, `info()`, `exec()`, `mount()`, `cp()`,
  `flash_dut(image, target=None)`, `reset_dut(mode)`, `telemetry()`,
  `uart_stream()`, `usbip_attach()`, `gdb_endpoint()`.

### H6.2 `pod` CLI
Thin frontend over the core library. Provisional subcommands:
- `pod discover` / `pod list` - live mDNS browse + registry status.
- `pod register <label>` / `pod info <label>` - registry management (mpy-dev-like).
- `pod repl <label>` - attach REPL via `ampremote socket://`.
- `pod mount <label> <dir>` - mount host dir on the pod.
- `pod flash <label> <image> [--target ...]` - drive the on-pod FLM/RP-native
  flasher.
- `pod reset <label> [--mode swd|nrst|power]`.
- `pod telemetry <label>` - INA228 readings (custom carrier hardware).
- `pod uart <label>` - tail/bridge DUT UART.
- `pod usbip <label> attach|detach` - host-side `usbip` helper.
- `pod gdb <label>` - proxy/launch against the on-pod GDB server.
- `pod exec <label> <code>` / `pod cp ...`.

### H6.3 `pod` MCP server
- `pod mcp` starts an MCP (stdio) server over the same core library, so the CLI
  and the agent share one implementation.
- Tools (map to CLI verbs): `discover_pods`, `pod_info`, `flash_dut`,
  `reset_dut`, `read_telemetry`, `dut_exec`, `mount_dir`,
  `tail_uart`, `attach_usbip`, `gdb_attach`/`gdb_*`.
- The MCP server is the contract for an agent's hardware iteration loop:
  edit DUT firmware -> `flash_dut` -> `reset_dut` -> observe (`tail_uart`,
  `read_telemetry`, `dut_exec`) -> repeat.
- Transport is `ampremote` throughout; discovery-first (resolve by label/service,
  never a hardcoded IP).

## Tasks

- Build the core library (discovery + registry + ampremote client) and the early
  slice tools (info, repl, mount, exec, telemetry) once F1 lands.
- Add `flash_dut`, `reset_dut` after D2/D3; `attach_usbip` after Phase 4;
  `tail_uart` after Phase 5 (`read_telemetry` once custom carrier hardware exists).
- Implement the MCP server over the library; validate each tool against a live
  pod.
- Package the CLI and MCP entry points (`pod`, `pod mcp`).

## Deliverables

- `pod` core library, CLI, and MCP server.
- A documented MCP tool set Claude can use to iterate on DUT firmware.

## Exit gate

Claude, through the `pod` MCP server, discovers a pod, flashes a DUT image,
resets it, tails its UART, and reads telemetry, all over `ampremote`.

## References

- `mpy-dev` (registry/labelling model to mirror)
- `ampremote` (`github.com/andrewleech/ampremote`) - sole transport
- MCP server build patterns (the `build-mcp-server` skill family)
