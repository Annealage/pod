# `pod` host tooling

Host-side library, CLI, and MCP server for driving an Annealage Pod (RP2350)
over Wi-Fi: discover pods, run code, mount directories, and flash / reset / read
a DUT through the pod's on-pod debug stack. It is the network-native sibling of
`mpy-dev`, and the agent-facing frontend for the hardware iteration loop.

Transport is `ampremote` (the `socket://` mpremote fork) for the REPL, plus
direct TCP sockets for the binary flash/read streams (ports 3333 / 3334) and
the GDB debug-command server (port 3335). The on-pod side is
`annealage_pod.debug` (see `../../docs/rp2350/debug-stack.md`).

## Install

```bash
cd src/host
pip install -e .            # console scripts: `pod`, `pod-mcp`
pip install -e .[dev]       # + zeroconf, mcp, pytest
```

Requirements:
- `ampremote` on `PATH` (the REPL transport). This build has no `resume`
  subcommand; `connect socket://HOST:PORT exec ...` does not soft-reset by
  default, which is what the client relies on.
- `zeroconf` (optional) for mDNS discovery; without it, discovery shells out to
  `avahi-browse`.
- `mcp` (optional) for the MCP server.
- The pod must have `annealage_pod.debug` resident at `/lib` for the DUT
  flash/reset/read verbs (deploy steps in `../../docs/rp2350/debug-stack.md`).

You can also run without installing: `cd src/host && python -m pod.cli ...`.

## CLI

```
pod discover [--timeout S]            browse mDNS (_annealage-pod._tcp) for live pods
pod register <label> --address IP [--repl-port 8266]
pod list                              show registered pods
pod info <label>                      show a registered pod's details
pod repl <label>                      attach an interactive REPL
pod mount <label> <dir>               mount a host directory on the pod
pod exec <label> "<code>"             run MicroPython on the pod, print stdout
pod cp <label> <src> <dst>            copy a file (':path' = pod side)
pod flash <label> <image> [--addr 0xADDR] [--target T]
pod reset <label> [--mode sysreset|halt]
pod gdb <label> [--listen-port N] [--gdb-port 3335] [--no-reset-halt] [--resume-window-ms 200]
pod mcp                               start the MCP stdio server (alias: pod-mcp)
```

The registry lives at `~/.config/pod/pods.json` (override with `POD_CONFIG_DIR`).

### End-to-end example

```bash
pod discover                                  # find the pod on the network
pod register lab1 --address 192.168.0.121     # label it
pod info lab1
pod flash lab1 firmware.bin --addr 0x0        # stream-flash the DUT over Wi-Fi
pod reset lab1 --mode sysreset                # reset and run
pod exec lab1 "import machine; print(machine.freq())"
```

`flash` streams the image straight into pod RAM and programs the DUT over SWD;
nothing is written to the pod filesystem. `--addr` accepts decimal or `0x` hex.

## Python library

```python
from pod.client import Pod
p = Pod("192.168.0.121")                       # or repl_port=8266

p.exec("print('hi')")                          # -> stdout str
p.cp("main.py", ":main.py")                    # ':' prefix = pod side
p.mount("/host/dir")                           # blocks until disconnected

# DUT operations (need annealage_pod.debug resident on the pod):
p.flash_dut("firmware.bin", addr=0x0)          # -> {'ok': True, 'bytes': N, ...}
p.reset_dut(mode="sysreset")                   # 'sysreset' (run) or 'halt'
p.read_dut(0x0, 4096, "dump.bin")              # explicit target read -> host file
p.gdb_endpoint(listen_port=0)                  # local GDB RSP server; blocks until detach
```

`flash_dut` / `read_dut` open a direct TCP connection to a pod receiver (ports
3333 / 3334) and stream the binary; the image is never staged on the pod
filesystem, and the prior DUT contents are only read by the explicit
`read_dut`. Discovery and registry helpers live in `pod.discovery` and
`pod.registry`.

## MCP server

`pod mcp` (or `pod-mcp`) starts an MCP stdio server over the same library, so an
agent drives the hardware loop with the same verbs:

| Tool | Action |
|---|---|
| `discover_pods` | browse mDNS for live pods |
| `pod_info` | registry info for a label |
| `dut_exec` | run MicroPython on a pod |
| `mount_dir` | mount a host directory on a pod |
| `flash_dut` | flash a DUT image (streamed into pod RAM, no pod FS) |
| `reset_dut` | reset the DUT (`sysreset` / `halt`) |
| `read_dut` | read DUT memory to a host file (streamed) |
| `gdb_dut` | start a local GDB RSP server to the DUT and return its endpoint |

The loop an agent runs: edit DUT firmware -> `flash_dut` -> `reset_dut` ->
observe (`dut_exec`) -> repeat.

## Tests

```bash
cd src/host && python -m pytest -q     # offline; no hardware or network needed
```

Tests cover discovery parsing, the registry, CLI argument handling, and the
client's command construction with an injected runner. The live flash/reset/read
paths are validated against hardware, not in the unit tests.

## Status

`discover` / `list` / `register` / `info` / `repl` / `mount` / `exec` / `cp` and
`flash_dut` / `reset_dut` / `read_dut` are implemented and validated on an
nRF52840 DUT. `gdb` / `gdb_endpoint` (Phase 3) is implemented on the host side
(RSP server, register map, breakpoint policy, run-control/interrupt, offline
unit tests); live-hardware validation is the Phase 3 gate against the on-pod
`dbgsrv`. `usbip_attach` (Phase 4) and `uart_stream` / `telemetry` (Phase 5)
remain stubbed and raise with the pending phase.
