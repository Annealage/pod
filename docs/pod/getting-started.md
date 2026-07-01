# Getting started with the Pod

Zero to a first DUT debug, in order. Each step is a few lines and a link to the
authoritative doc; follow the links for the detail, do not expect it repeated
here.

The Pod is a Raspberry Pi Pico 2 W running MicroPython. It debugs and flashes a
device-under-test (DUT) over SWD, forwards the DUT's USB and (planned) UART, and
presents I2C/GPIO/ADC to the DUT - all driven from your PC over Wi-Fi. You will
build its firmware, put it on Wi-Fi, install the host tooling, wire a DUT, and
run a first flash and debug.

## 1. Build and flash the Pod firmware

Build with the top-level Makefile, then put the firmware on a Pico 2 W:

```bash
make                # build firmware.uf2 + firmware.elf
```

- **Fresh Pico, no probe:** hold BOOTSEL while plugging in USB, then drag
  `firmware.uf2` (from the build dir) onto the mounted `RPI-RP2` drive.
- **With a wired CMSIS-DAP probe:** `make flash` programs over SWD with both
  cores halted, so it is safe to reflash a Pod that is already running.

Board details, pinout, and the probe wiring:
[../../src/boards/ANNEALAGE_POD_RP2350/README.md](../../src/boards/ANNEALAGE_POD_RP2350/README.md).

## 2. Set Wi-Fi credentials

Wi-Fi creds live in a `config.py` on the Pod filesystem (not frozen into
firmware, not committed). Copy the template and edit it:

```bash
cp src/boards/ANNEALAGE_POD_RP2350/config.example.py config.py   # edit WIFI_SSID / WIFI_PASSWORD
```

Copy `config.py` to the Pod (over the USB-CDC REPL or Wi-Fi) and reset. See the
"Configuration" section of
[../../src/boards/ANNEALAGE_POD_RP2350/README.md](../../src/boards/ANNEALAGE_POD_RP2350/README.md).

## 3. Install the host tooling

From source (the repo is private pre-launch; the `ampremote` dependency is a
git-only fork, so this needs network and git):

```bash
pip install ./src/host[mcp,zeroconf]
```

This installs the `pod` CLI and the `pod-mcp` server. Usage, the `Pod` Python
client, and the MCP tool list:
[../../src/host/README.md](../../src/host/README.md).

## 4. Discover and register the Pod

```bash
pod discover                        # browse mDNS for live Pods
pod register lab1 --address <ip>    # label it (use the discovered address)
pod info lab1
```

`lab1` is your chosen label; all later verbs take it. See
[../../src/host/README.md](../../src/host/README.md).

## 5. Wire the DUT to the Pod

At minimum, wire SWD so the Pod can flash and debug the DUT: Pod **GP14 (SWDIO)**
and **GP15 (SWCLK)** to the DUT's SWD pads, plus a common **GND**. Add USB, UART,
and functional-test pins as needed.

Full wiring reference (SWD, USB host, UART, I2C, SPI, GPIO/ADC, logic-analyser,
power/ground) and the Pico pinout:
[hardware-setup.md](hardware-setup.md) (SWD is section 5a).

## 6. Flash and reset the DUT

```bash
pod flash lab1 fw.bin           # stream the image into Pod RAM, program over SWD
pod reset lab1                  # reset and run (add --mode halt to catch the reset vector)
```

`flash` never stages the image on the Pod filesystem. The on-pod debug stack,
the FLM flash backends (`loader="native"` default, or `loader="flm"`), and
deployment of the `annealage_pod` package are in
[debug-stack.md](debug-stack.md).

## 7. First debug

Attach a debugger, or peek-poke the halted core:

```bash
pod gdb lab1                    # GDB RSP server to the DUT; supports DWT watchpoints (Z2/Z3/Z4) + FPB breakpoints
# or single-shot SWD:
pod halt lab1                   # hold the core
pod read-reg lab1 pc            # read a register (reg 0..18 or pc/sp/lr/..)
pod resume lab1                 # release the core
```

Register/memory peek-poke and the GDB path (including data watchpoints) are
covered in [debug-stack.md](debug-stack.md).

## Ports

The Pod exposes these TCP ports:

| Port | Service |
|---|---|
| 8266 | socket REPL (Wi-Fi management) |
| 3240 | USB/IP (DUT USB export) |
| 3335 | GDB / DAP RPC |
| 3333 | flash-in (DUT image stream) |
| 3334 | mem-out (DUT read stream) |
| 3336 | logic-analyser out |

## Next

- Task recipes and workflows: [howto.md](howto.md).
- Subsystem docs: [debug-stack.md](debug-stack.md) (SWD/GDB debug),
  [peripherals.md](peripherals.md) (I2C target / GPIO / ADC),
  [logic-analyser.md](logic-analyser.md) (PIO logic analyser),
  [hardware-setup.md](hardware-setup.md) (wiring).
- Host CLI, Python client, and MCP: [../../src/host/README.md](../../src/host/README.md).
- Capability and readiness status: [../website-features.md](../website-features.md).
- Development gotchas and the phased plan: [dev-notes.md](dev-notes.md),
  [plan/overview.md](plan/overview.md).
```
