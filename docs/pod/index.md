# Annealage Pod documentation

The Pod is a Wi-Fi hardware-in-the-loop test rig: it flashes, debugs, resets, and
exercises a device-under-test (DUT) over the network, with the debugger running on the
Pod itself, so there is no wired host PC and no separate debug probe in the loop. A host
`pod` CLI, Python client, and MCP server are the control plane. For the full capability
list and what is shipping versus landing, see
[website-features.md](../website-features.md).

## Start here

New to the Pod? Follow the **[getting-started guide](getting-started.md)** - it takes you
zero-to-working (build and flash the Pod firmware, set Wi-Fi, install the host tooling,
discover and register the Pod, wire a DUT, then flash and debug it), linking to the
detailed docs at each step. After that:

1. **[howto.md](howto.md)** - task recipes (flash a DUT, recover a hung DUT, GDB debug,
   capture signals, I2C target, USB/IP) for once you are up and running.
2. **Go deep on a subsystem** - the debug stack, logic analyser, and peripheral docs
   below, as your task needs them.

The detailed references the guide links into: the
[board README](../../src/boards/ANNEALAGE_POD_RP2350/README.md) (firmware build, flash,
Wi-Fi), [hardware-setup.md](hardware-setup.md) (DUT wiring + pinout), and
[src/host/README.md](../../src/host/README.md) (the `pod` CLI, Python client, and MCP).

## Goal to doc map

| I want to... | Read |
|---|---|
| Get from zero to a working DUT session | [getting-started.md](getting-started.md) |
| Do a specific task (flash, recover, GDB, capture, I2C, USB/IP) | [howto.md](howto.md) |
| Set up the Pod firmware (build, flash, Wi-Fi config) | [board README](../../src/boards/ANNEALAGE_POD_RP2350/README.md) |
| Wire a DUT to the Pod (pinout, SWD/I2C/GPIO/ADC/LA taps, power/ground) | [hardware-setup.md](hardware-setup.md) |
| Use the Pod CLI, Python client, or MCP server | [src/host/README.md](../../src/host/README.md) |
| Flash, debug, GDB, or set data watchpoints on a DUT | [debug-stack.md](debug-stack.md) |
| Capture signals with the logic analyser | [logic-analyser.md](logic-analyser.md) |
| Use the Pod as an I2C target, or drive GPIO / read ADC | [peripherals.md](peripherals.md) |
| Drive a dev board with a built-in programmer (Nucleo, CMSIS-DAP) with no pod | [howto.md](howto.md#drive-a-dev-board-with-no-pod) |
| See the capability list and what is shipping vs landing | [website-features.md](../website-features.md) |

Notes on the debug row: [debug-stack.md](debug-stack.md) covers flashing (both the
native and generic CMSIS-FLM loader backends), the GDB path, and GDB data watchpoints
(write/read/access). The host verbs that exercise it (`pod dut flash`, `pod dut gdb`,
`pod dut halt`/`resume`, `pod dut reg`/`write-reg`, `pod dut mem`/`write-mem`) are
documented in [src/host/README.md](../../src/host/README.md).

## For developers

The documents below are for contributors working on the Pod itself, not for using a
Pod. They cover bring-up gotchas, spike results, and the roadmap; a Pod user does not
need them.

- [dev-notes.md](dev-notes.md) - development gotchas and recipes (module-cache caveats,
  flash/UF2 pitfalls, SWD framing) that cost real debugging time.
- [spike-findings.md](spike-findings.md) - hardware-validated bring-up results and the
  settled architecture points.
- [plan/](plan/) - the phased development plan; start at
  [plan/overview.md](plan/overview.md).
