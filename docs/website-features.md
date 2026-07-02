# Annealage Pod - feature reference (website source-of-truth)

This file is the verified capability reference for Annealage Pod, written from the
RP2350 codebase, docs, and git history (branch `main`, 2026-06-25). It is the
durable source-of-truth the marketing `/pod` page and the `/docs/pod` Starlight pages
should be checked against, so the site cannot drift from the code again.

Conventions used here:

- **Target:** only the RP2350 (Raspberry Pi Pico 2 W) variant is described. The earlier
  ESP32-S3 design is not the current product and is getting no new features; treat any
  ESP32-S3 detail on the existing site (synthetic CMSIS-DAP-v2 probe, ESP-IDF, on-board
  relays, on-board INA228) as superseded. See "Corrections" at the end.
- **Status tags per capability:** `[validated]` = exercised on real hardware (an
  nRF52840 DUT) with a recorded result; `[landing]` = implemented, partial or
  validation in progress; `[carrier]` = needs the upcoming purpose-built carrier board,
  not available on a bare Pico; `[planned]` = not yet built.
- Capability altitude only. Exact API signatures live in the repo docs under
  `docs/pod/` and `src/host/README.md`; this file names tools/commands/ports but is
  not an API dump.

---

## What Annealage Pod is

Annealage Pod is a Wi-Fi hardware-in-the-loop test rig. It flashes, debugs, resets, and
exercises a device-under-test (DUT) over the network, with the debugger running on the
pod itself, so there is no wired host PC and no separate debug probe in the loop. A host
`pod` CLI, Python client, and MCP server are the control plane; an AI agent or an
engineer drives the same iteration loop over Wi-Fi.

Suggested one-line blurb (rewritten from the locked CLAUDE.md line, which led on USB/IP -
the least-finished part - and is better led by the on-pod debugger, the actual
differentiator):

> Wi-Fi hardware-in-the-loop test rig: flash, debug, and exercise a device-under-test
> over the network, with the debugger running on the pod itself. No wired host, no
> separate probe.

Keep or edit; flagged because it changes emphasis from the current line.

It replaces a multi-board, wired-USB probe carrier (the dual-RP2040 Octoprobe Tentacle)
with a single network-attached board.

Target users: professional embedded and PCB engineers, plus a hobbyist free tier for
non-commercial use.

---

## Hardware target

Runs on an off-the-shelf **Raspberry Pi Pico 2 W** (RP2350, CYW43 Wi-Fi). Today the dev
setup is a bare Pico 2 W plus jumper wires to the DUT. The native USB controller is the
DUT host port; pod management rides Wi-Fi.

The defining architecture point: instead of the pod *being* a USB debug probe that a host
PC drives, the pod *runs the debugger itself* in MicroPython (PIO-driven SWD, an ADIv5
DP/AP/MEM-AP stack, a Cortex-M control layer, a flash loader, and a GDB server), and
exposes flash/debug/reset/capture operations over Wi-Fi.

---

## Readiness at a glance

Annealage Pod is a shipping, first-class product (alongside Annealage Canvas). The core
debug-and-test workflow is validated on hardware today; the DUT UART-over-TCP bridge is
planned, and per-rail power telemetry plus opto-relays arrive with the carrier board.

| Subsystem | Status | One-line |
|---|---|---|
| On-pod SWD debugger (DP/AP/MEM-AP, Cortex-M) | `[validated]` | Drives SWD locally; no external probe |
| Flash a DUT over Wi-Fi | `[validated]` | nRF52 native-NVM + generic CMSIS-FLM loader |
| GDB through the pod | `[validated]` | Real `arm-none-eabi-gdb` over Wi-Fi |
| Logic analyser | `[validated]` | PIO capture, streamed to host, decoded to VCD |
| DUT peripherals (I2C target, GPIO, ADC) | `[validated]` | Pod acts as I2C target / drives GPIO / reads ADC |
| Networking + discovery (mDNS, IPv6-first) | `[validated]` | Browsable `_annealage-pod._tcp`, dual-stack |
| Management REPL over Wi-Fi | `[validated]` | Socket REPL, persistent auto-reconnect session |
| USB/IP DUT export - enumerate + attach | `[validated]` | Host sees + binds the DUT |
| USB/IP DUT export - forwarded DUT REPL | `[validated]` | CDC REPL over the forward; reliable under sustained attach/detach churn |
| DUT UART-over-TCP bridge | `[planned]` | Advertised in mDNS, not yet bound on RP2350 |
| INA228 power telemetry, opto-relays, power switching | `[carrier]` | Needs the upcoming carrier board |

How to frame this on the page: lead with the validated core (debug, flash, GDB, logic
analyser, peripherals, networking). Present USB/IP as "enumerate and attach a DUT's USB
to your host, with a full forwarded DUT REPL session validated over the attach and
reliable under sustained attach/detach churn." Do not list UART-over-TCP, opto-relays, or
INA228 telemetry as present features on the bare-Pico product; UART is planned and the
power/relay features belong to the carrier tease below.

---

## Subsystems (capabilities)

### On-pod SWD debugger `[validated]`
The pod speaks SWD over a PIO state machine (pod GP14 = SWDIO, GP15 = SWCLK) at
9.375 MHz, with an ADIv5 DebugPort / MEM-AP stack and a Cortex-M control layer (halt,
resume, reset-and-halt, single-step, core-register and memory access, fault-cause
readout), FPB hardware breakpoints, and DWT data watchpoints (write/read/access,
reachable over GDB). Validated against an nRF52840 (DPIDR
`0x2BA01477`, CPUID read, 100/100 clean transfers).

### Flashing a DUT over Wi-Fi `[validated]`
A DUT image is streamed straight into pod RAM over TCP and programmed chunk-by-chunk
over SWD, with verify; it never touches a pod filesystem. Two flash paths: a native
nRF52 NVMC path (the validated one), and a generic CMSIS-FLM runner that executes a
standard CMSIS pack flash algorithm in the target's own SRAM (so any chip with a CMSIS
pack is reachable; validated end-to-end on nRF52840).

### GDB through the pod `[validated]`
A host-side translator presents a standard GDB remote-serial endpoint and bridges to a
small binary DAP RPC server on the pod. A stock `arm-none-eabi-gdb` connects over Wi-Fi
and gets reset-halt, register and memory read/write, a hardware breakpoint hit,
backtrace, single-step, continue and re-hit, and a clean detach. The pod holds no GDB
protocol state; the RSP semantics live host-side.

### Direct register / memory access `[validated]`
Single-shot SWD peek-poke without a full GDB session: read/write core registers (core
must be halted) and read/write target memory (live MEM-AP). Flash and the Cortex-M code
region are write-protected by policy. Validated on an nRF52840 (halt, read pc/sp/xpsr,
RAM read plus write/read-back/restore).

### Logic analyser `[validated]`
A PIO state machine samples up to 32 contiguous DUT GPIOs into a DMA RAM ring with
optional edge/level trigger, streams the capture to the host, and decodes to VCD.
Validated end-to-end including the live Wi-Fi round-trip and a two-channel 50/25 kHz
decode. Runs on PIO0; coexists with SWD (PIO1) and Wi-Fi (PIO2) simultaneously.

### DUT-facing peripherals `[validated]`
The pod can present hardware peripherals to a DUT-as-controller: a hardware I2C target
(a register-file responder on I2C1, address-match/ACK/clock-stretch in silicon), plus
GPIO drive/read and ADC sampling. Validated against an nRF52840 controller. This is a
thin pass-through over MicroPython's `machine` module, not a gated API.

### USB/IP DUT export `[validated]`
The pod's native USB port hosts the DUT, and an in-tree C USB/IP server (port 3240)
exports it over Wi-Fi so a host can `usbip attach` and bind the DUT with a normal class
driver. Device enumeration, attach, and a full forwarded CDC REPL session (`usbip
attach` then `mpremote connect` over the attached tty) are validated on an nRF52840,
across repeated attach / round-trip / detach cycles, including sustained attach/detach
churn (100 cycles of attach -> forwarded round-trip -> detach at 0% Wi-Fi packet loss).

### Networking and discovery `[validated]`
The pod advertises a browsable mDNS service `_annealage-pod._tcp` (`annealage-pod.local`)
with TXT records carrying its ports and identity. All pod TCP listeners are dual-stack
(IPv4 + IPv6). The host resolves a pod IPv6-first (self-authenticating via EUI-64), falls
back to a fingerprint-checked IPv4, then re-resolves via mDNS; it refuses to act on a
fingerprint mismatch, so it cannot flash or reset the wrong board after a DHCP change.

### Management REPL over Wi-Fi `[validated]`
The only management channel on a deployed pod is a Wi-Fi socket REPL (one client at a
time; a second connect is refused without evicting the active one). The host tooling adds
a persistent, auto-reconnecting streaming session: it tees DUT/pod stdout to a log file
and the console, accepts async stdin, survives drops and reboots with backoff, and can
chain setup (soft-reset, file copy, exec, mount) before connecting. A backup UART REPL on
GP0/GP1 survives the native-USB switch to host mode.

---

## MCP surface

Transport is **stdio** via the `pod-mcp` console script that ships with the host tooling;
it is not an HTTP endpoint. The current page's `http://pod.local/mcp` is invented and
should be replaced. An agent adds it with, for example:

```
claude mcp add pod -- pod-mcp
```

The server wraps the `pod` client and exposes **33 tools**. Grouped:

- **Discovery / registry:** `discover_pods`, `pod_info`, `register_pod`, `dut` (probe and
  reconcile the wired DUT's SWD identity).
- **SWD debug (requires the DUT wired + powered for SWD):** `flash_dut`, `reset_dut`,
  `read_dut`, `gdb_dut`, `dut_halt`, `dut_resume`, `dut_read_reg`, `dut_write_reg`,
  `dut_read_mem`, `dut_write_mem`. The register tools additionally require a halted core.
- **USB/IP:** `dut_usb`, `attach_dut`, `detach_dut`, `ensure_dut_link`, `dut_exec`
  (run code on the DUT's own REPL over USB/IP).
- **Pod-side exec / files:** `pod_exec`, `mount_dir`.
- **Persistent REPL session:** `repl_open`, `repl_read`, `repl_send`, `repl_interrupt`,
  `repl_close`, `repl_list`.
- **Peripherals:** `i2c_target`, `i2c_target_regs`, `gpio`, `adc`, `peripheral_release`,
  `logic_analyse` (mutually exclusive with a live SWD session).

Read vs write: `discover_pods`, `pod_info`, `dut`, `dut_usb`, `read_dut`, `dut_read_reg`,
`dut_read_mem`, `adc`, `repl_read`, `repl_list`, `peripheral_release` are read/query;
`flash_dut`, `reset_dut`, `attach_dut`, `dut_write_reg`, `dut_write_mem`, `gpio` (drive),
`i2c_target*`, `mount_dir` mutate state. The SWD group is meaningful only when a DUT is
attached for debug; the register tools need the core halted first.

---

## Quickstart (real flow)

The real command names and ports (the current page invents `annealage-pod flash`,
`pod uart tail dut`, and an HTTP MCP URL; none of those exist):

1. **Flash the pod firmware** onto a Pico 2 W and set Wi-Fi credentials in `config.py`
   (template `config.example.py`). `make flash` programs it over an attached probe with
   both cores halted.
2. **Wire the DUT to the pod** - at minimum SWD: pod GP14 to SWDIO, GP15 to SWCLK, and a
   common ground. See the pinout below.
3. **Install the host tooling** (`pip install` the `src/host` package; it pulls the
   `ampremote` fork from git). 
4. **Find and register the pod:** `pod discover`, then `pod register <label>` (browses
   mDNS and stores the pod's handles + identity fingerprint).
5. **Drive the DUT:** `pod flash <label> firmware.bin`, `pod reset <label>`,
   `pod gdb <label>` (prints a `target extended-remote host:port` for your gdb),
   `pod la <label> --pins 16-19 --out cap.vcd`, `pod repl <label>` (live streaming REPL).
6. **For an agent:** `claude mcp add pod -- pod-mcp`, then the 33 tools above are
   available over Wi-Fi.

Ports in use: socket REPL `8266`, USB/IP `3240`, GDB/DAP RPC `3335`, flash-in `3333`,
memory-out `3334`, logic-analyser-out `3336`.

---

## Stack

- **Board:** Raspberry Pi Pico 2 W (RP2350, CYW43 Wi-Fi).
- **Firmware:** MicroPython (rp2 port), composed with `mbm` to add the in-tree
  `machine-usbhost`, `network-mdns`, and a no-Werror TinyUSB host. There is **no ESP-IDF**
  on this target; the page's "ESP-IDF 5.5" is ESP32-S3-only.
- **Debug stack:** pure MicroPython (`annealage_pod.debug`) - PIO SWD, ADIv5 DP/AP/MEM-AP,
  Cortex-M, FPB, an nRF52 NVM flash path plus a generic CMSIS-FLM runner, and a binary
  DAP RPC server with a host-side GDB RSP translator.
- **USB/IP:** an in-tree C module (`usbip`) implemented as an lwIP-RAW callback state
  machine with static buffer pools (no libc malloc on the network path), forwarding raw
  URBs; a TinyUSB host backend (`usbhost`) runs the DUT enumeration. The pod is a raw
  forwarder and deliberately runs no USB class drivers of its own.
- **Discovery:** native lwIP mDNS responder; service `_annealage-pod._tcp`,
  hostname `annealage-pod.local`.
- **Host tooling:** Python `pod` CLI + `Pod` client + `pod-mcp` MCP server, talking to the
  pod over `ampremote` (a git-pinned async fork of `mpremote`) and raw TCP for the binary
  streams.

---

## Pico 2 W wiring / pinout

3.3V logic only (RP2350 GPIOs are not 5V tolerant); a common ground on every connection is
mandatory; ADC reference is 3.3V. GP23/24/25/29 are internal CYW43 Wi-Fi pins and are not
on the header. GP0/GP1 are reserved for the backup UART REPL.

**Validated interfaces** (firmware-assigned and exercised on hardware):

| DUT connection | Pod side | DUT side |
|---|---|---|
| SWD debug / flash | GP14 SWDIO, GP15 SWCLK, GND | SWDIO, SWCLK, GND |
| I2C (pod = target) | GP10 SDA, GP11 SCL, GND | SDA, SCL, GND |
| GPIO functional | any free GP, GND | pin under test |
| ADC measure | GP26 / GP27 / GP28, GND | 0-3.3V analog node |
| Logic-analyser taps | GP16-GP21 (default block), GND | signals to observe |
| Backup pod console | GP0 TX, GP1 RX (to a probe, not the DUT) | n/a |

**Suggested interfaces** (no firmware pin assigned yet, or assigned-but-not-bench-tested -
do not present these as finished):

| DUT connection | Pod side (suggested) | Status |
|---|---|---|
| DUT reset (nRST) | GP13 | assigned in code, not yet hardware-tested |
| DUT UART bridge | GP4 TX, GP5 RX (UART1) | suggested; bridge not implemented |
| DUT SPI | GP18 SCK, GP19 MOSI, GP16 MISO, GP17 CS (SPI0) | suggested; overlaps the LA default block |
| USB host (USB/IP) | native USB connector | cabling / VBUS / current limit unspecified |

PIO block map (do not violate): PIO0 = logic analyser (free), PIO1 = SWD, PIO2 = CYW43
Wi-Fi (reserved; building a state machine there hard-wedges the chip). GP16-GP19 are
shared between the suggested SPI0 and the LA default block; use one at a time, or move the
LA to GP2-GP9.

The deep, hobbyist-followable wiring reference (with electrical rules, the full pin-budget
table, and collision warnings) lives at `docs/pod/hardware-setup.md`.

---

## Upcoming: off-the-shelf Pod hardware

Today Annealage Pod runs on an off-the-shelf Raspberry Pi Pico 2 W with jumper wires to
the DUT. A purpose-built Annealage Pod carrier board is in development: it adds per-rail
INA228 power telemetry, opto-isolated relays, and DUT power switching (the features the
bare Pico cannot provide), on a board that wires the DUT interfaces for you. This is the
right home for any "coming soon" tease on the site; do not present these power/relay
features as available on the current product.

---

## Corrections to the current /pod and /docs/pod pages

Every Pod fact on the current site came from the brand blurb, not the code, and describes
the older ESP32-S3 design. Specific fixes:

- **Wrong hardware and architecture.** The page describes an ESP32-S3 that exports a
  *synthetic CMSIS-DAP-v2 probe* over USB/IP. The current product is an RP2350 that *runs
  the debugger itself*; there is no synthetic probe. Rebuild the architecture section
  around the on-pod debugger.
- **"ESP-IDF 5.5"** - wrong for RP2350; it is MicroPython on the rp2 port, no ESP-IDF.
- **"Seven opto-isolated relays" and INA228 telemetry** - not on the bare Pico; these move
  to the upcoming carrier board. Reframe under the hardware tease, not as current features.
- **"UART-over-TCP"** - not implemented on RP2350 (the port is advertised in mDNS but
  nothing binds it yet). Remove from the present feature list; it is planned.
- **MCP endpoint** - the page's `http://pod.local/mcp` does not exist. The MCP server is
  stdio via the `pod-mcp` console script.
- **CLI names** - `annealage-pod flash` and `pod uart tail dut` are invented. The real CLI
  is `pod <verb>` (`pod flash`, `pod reset`, `pod gdb`, `pod repl`, `pod la`, ...). There is
  no UART tail command.
- **No MCP action list** - the page has none; use the 33-tool list above.
- **Missing the actual differentiators** - the on-pod debugger, GDB-through-pod, the PIO
  logic analyser, IPv6-first discovery, and the 33-tool MCP surface are the strongest,
  validated capabilities and are absent from the page.

Note on positioning: "shipping now" for Pod (alongside Canvas) is retained per the product
owner. Keep that framing; just lead with the validated core and mark USB/IP bulk + UART as
landing/planned rather than claiming them done.
