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
pod i2c-target <label> [--addr 0x42] [--regs 0xAB 0xCD ...] [--bus 1] [--scl 11] [--sda 10]
pod i2c-regs <label> [--off 0] [--length N] [--write 0xAB ...]   read/write the target register file
pod gpio <label> <pin> [--value 0|1] [--pull up|down]            read or drive a pod GPIO
pod adc <label> <pin>                                            sample a pod ADC channel
pod release <label> [--name '*']                                 release pod peripheral instance(s)
pod la <label> [--pins 16-23] [--rate 1e6] [--depth N] [--trigger 16:rise] [--out cap.vcd]
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

## DUT-facing peripherals

Beyond debugging the DUT, the pod can *present* peripherals to it: act as an I2C
target the DUT's controller talks to, drive/read GPIO, sample an ADC. The design
is thin passthrough plus a few curated helpers, not a heavy abstraction:

- For anything MicroPython's `machine` module exposes, just `pod exec` (or the
  `dut_exec` MCP tool) the code, e.g. `pod exec lab1 "from machine import Pin;
  print(Pin(25, Pin.OUT).value(1))"`. That is the universal escape hatch.
- The lifecycle-bearing cases get a curated helper so they persist correctly.
  The I2C target services the bus autonomously and must outlive a single call,
  so `pod i2c-target` brings it up and it stays up until `pod release`.

These map to `annealage_pod.peripherals` on the pod (see
`../../docs/rp2350/peripherals.md`).

```bash
# Pod becomes an I2C device at 0x42 backing a register file [0xAB, 0xCD, ...].
# The DUT controller reads it with readfrom_mem(0x42, off, n); each side sees
# the other's writes. Bench wiring is I2C1, SCL=GP11, SDA=GP10.
pod i2c-target lab1 --addr 0x42 --regs 0xAB 0xCD
pod i2c-regs lab1 --off 0 --length 2            # inspect what the DUT wrote
pod i2c-regs lab1 --off 0 --write 0x11 0x22     # seed registers from the host
pod gpio lab1 25 --value 1                       # drive GP25 high
pod gpio lab1 16 --pull up                       # read GP16 with a pull-up
pod adc lab1 26                                  # -> {'u16': ..., 'volts': ...}
pod release lab1                                 # tear down all pod peripherals
```

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

# DUT-facing peripherals (annealage_pod.peripherals resident on the pod):
p.i2c_target(addr=0x42, regs=[0xAB, 0xCD])     # pod acts as I2C target (register file)
p.i2c_target_regs(off=0, length=2)             # -> {'regs': [...]} read the file
p.i2c_target_regs(off=0, write=[0x11, 0x22])   # seed the file from the host
p.gpio(25, value=1)                            # drive; p.gpio(16, pull='up') reads
p.adc(26)                                      # -> {'u16': ..., 'volts': ...}
p.peripheral_release()                         # release all (or name='i2c_target')
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
| `i2c_target` | pod acts as a hardware I2C target backing a register file |
| `i2c_target_regs` | read/write that register file from the host |
| `gpio` | read or drive a pod GPIO |
| `adc` | sample a pod ADC channel |
| `peripheral_release` | release a named pod peripheral instance, or all |
| `logic_analyse` | PIO-capture DUT pins (swaps SWD out) and write a VCD file |

The loop an agent runs: edit DUT firmware -> `flash_dut` -> `reset_dut` ->
observe (`dut_exec`, or have the pod present an `i2c_target` / `gpio` the DUT
exercises) -> repeat.

## Tests

```bash
cd src/host && python -m pytest -q     # offline; no hardware or network needed
```

Tests cover discovery parsing, the registry, CLI argument handling, and the
client's command construction with an injected runner. The live flash/reset/read
paths are validated against hardware, not in the unit tests.

## Status

`discover` / `list` / `register` / `info` / `repl` / `mount` / `exec` / `cp`,
`flash_dut` / `reset_dut` / `read_dut`, and `gdb` / `gdb_endpoint` (Phase 3) are
implemented and validated on an nRF52840 DUT. The DUT-facing peripherals
(`i2c_target` / `i2c_target_regs` / `gpio` / `adc` / `release`, Phase 5) are
implemented, host-unit-tested, and hardware-validated end-to-end on the pod + an
nRF52840 controller: the pod presents the register file via the helper, the DUT
reads it (`readfrom_mem`) and writes it (`writeto_mem`), and the host reads back
what the DUT wrote.
The PIO logic analyser (`logic_analyse` / `pod la`, Track 2) is implemented and
its pieces are hardware-validated (DMA capture at 1 MHz, the SWD<->LA swap, the
VCD decoder, non-wedging socket teardown); the live Wi-Fi capture round-trip is
pending on the pod's Wi-Fi reconnect reliability (see
`../../docs/rp2350/logic-analyser.md`).
`usbip_attach` (Phase 4) and `uart_stream` / `telemetry` (Phase 5) remain
stubbed and raise with the pending phase.
