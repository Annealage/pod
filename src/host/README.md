# `pod` host tooling

Host-side library, CLI, and MCP server for driving an Annealage Pod (RP2350)
over Wi-Fi: discover pods, run code, mount directories, and flash / reset / read
a DUT through the pod's on-pod debug stack. It is the network-native sibling of
`mpy-dev`, and the agent-facing frontend for the hardware iteration loop.

Transport is `ampremote` (the `socket://` mpremote fork) for the REPL, plus
direct TCP sockets for the binary flash/read streams (ports 3333 / 3334) and
the GDB debug-command server (port 3335). The on-pod side is
`annealage_pod.debug` (see `../../docs/pod/debug-stack.md`).

## Install

```bash
cd src/host
pip install -e .            # console scripts: `pod`, `pod-mcp`
pip install -e .[dev]       # + zeroconf, mcp, pytest
```

Requirements:
- `ampremote` (Andrew's async `mpremote` fork - improved TCP + raw-REPL
  support). It is NOT on PyPI; `pip install -e .` pulls it straight from GitHub
  at the commit pinned in `pyproject.toml` (the source of truth for the exact
  revision), installing both the `ampremote` console script (the REPL transport)
  and the `mpremote` import (used by the persistent `pod repl` session). This
  build has no `resume` subcommand; `connect socket://HOST:PORT exec ...` does
  not soft-reset by default, which is what the client relies on.
- `zeroconf` (optional) for mDNS discovery; without it, discovery shells out to
  `avahi-browse`.
- `mcp` (optional) for the MCP server.
- The pod must have `annealage_pod.debug` resident at `/lib` for the DUT
  flash/reset/read verbs (deploy steps in `../../docs/pod/debug-stack.md`).

You can also run without installing: `cd src/host && python -m pod.cli ...`.

## CLI

```
pod discover [--timeout S]            browse mDNS (_annealage-pod._tcp) for live pods
pod register <label> --address IP [--repl-port 8266]
pod list                              show registered pods
pod info <label>                      show a registered pod's details
pod mount <label> <dir>               mount a host directory on the pod
pod exec <label> "<code>"             run MicroPython on the pod, print stdout
pod cp <label> <src> <dst>            copy a file (':path' = pod side)
pod flash <label> <image> [--addr 0xADDR] [--target T]
pod reset <label> [--mode sysreset|halt]
pod gdb <label> [--listen-port N] [--gdb-port 3335] [--no-reset-halt] [--resume-window-ms 200]
                                      (GDB path supports DWT data watchpoints: Z2/Z3/Z4 = write/read/access, plus FPB breakpoints)
pod halt <label>                      halt the DUT core over SWD (hold; no auto-resume)
pod resume <label>                    resume the DUT core over SWD
pod read-reg <label> <reg>            read a core register over SWD (core halted; reg 0..18 or pc/sp/lr/..)
pod write-reg <label> <reg> <value>   write a core register over SWD (core halted)
pod read-mem <label> <addr> <len>     read DUT memory over SWD, print hex (<= 4096 bytes)
pod write-mem <label> <addr> <hex>    write DUT memory over SWD (RAM/peripherals; flash refused)
pod repl <label> [--raw] [--log FILE] [--device DEV] [--mount DIR] [--exec CODE] [--cp SRC DST] [--soft-reset] [--no-reconnect]
                                      persistent REPL: stream stdout to console+file, type lines to stdin
                                      (auto-reconnects across drops; --raw = full raw terminal;
                                       --mount/--exec/--cp/--soft-reset chain setup first)
pod pins <label> [--cached]           show the pod's own DUT-facing pin assignments
pod dut <label> [--dut-* ...] [--adopt]   show / set / verify the wired DUT (identity, wiring, usb, repl)
pod usb <label>                       list the DUT USB devices the pod exports (live VID:PID)
pod attach <label> [--no-ensure]      attach the pod's DUT USB over USB/IP; prints the DUT tty
pod detach <label> --port N           detach a USB/IP vhci port (N from `usbip port`)
pod i2c-target <label> [--addr 0x42] [--regs 0xAB 0xCD ...] [--bus 1] [--scl 11] [--sda 10]
pod i2c-regs <label> [--off 0] [--length N] [--write 0xAB ...]   read/write the target register file
pod gpio <label> <pin> [--value 0|1] [--pull up|down]            read or drive a pod GPIO
pod adc <label> <pin>                                            sample a pod ADC channel
pod release <label> [--name '*']                                 release pod peripheral instance(s)
pod la <label> [--pins 16-23] [--rate 1e6] [--depth N] [--trigger 16:rise] [--out cap.vcd]
pod-mcp                               start the MCP stdio server (separate console script)
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
`../../docs/pod/peripherals.md`).

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

# SWD register/memory peek-poke (single-shot; registers need a halted core):
p.halt_dut(); p.resume_dut()                   # hold / release the core over SWD
p.read_reg("pc")                               # -> {'value': ...}; reg 0..18 or pc/sp/lr/..
p.write_reg("sp", 0x20004000)
p.read_mem(0x20000000, 16)                     # -> {'hex': '...'}; live MEM-AP, <= 4096 bytes
p.write_mem(0x20000000, b"\xde\xad\xbe\xef")   # RAM/peripherals; flash + code region refused

# Persistent streaming REPL session (built on ampremote; pod socket REPL by default):
s = p.open_session(log_path="pod.log", mount="./fw")  # connect; mount ./fw for the session
s.send("import os; print(os.uname())")         # inject a REPL command line
print(s.read_since()["text"])                  # tail buffered output (pass cursor for only-new)
s.interrupt(); s.close()                        # Ctrl-C the target; close (it keeps running)
# chain stateless setup too: open_session(pre_exec=[...], pre_cp=[("a.py",":a.py")], soft_reset=True)

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

The `pod-mcp` console script starts an MCP stdio server over the same library, so
an agent drives the hardware loop with the same verbs:

| Tool | Action |
|---|---|
| `discover_pods` | browse mDNS for live pods |
| `pod_info` | registry info for a label |
| `dut_exec` | run MicroPython on a pod |
| `mount_dir` | mount a host directory on a pod |
| `flash_dut` | flash a DUT image (streamed into pod RAM, no pod FS) |
| `reset_dut` | reset the DUT (`sysreset` / `halt`) |
| `read_dut` | read DUT memory to a host file (streamed) |
| `gdb_dut` | start a local GDB RSP server to the DUT and return its endpoint (supports DWT data watchpoints via gdb Z2/Z3/Z4 = write/read/access, plus FPB hardware breakpoints) |
| `dut_halt` / `dut_resume` | halt/resume the DUT core over SWD (no auto-resume; halt freezes the DUT) |
| `dut_read_reg` / `dut_write_reg` | read/write a core register over SWD (core must be halted) |
| `dut_read_mem` / `dut_write_mem` | read/write DUT memory over SWD, inline hex (live MEM-AP; flash refused) |
| `repl_open` / `repl_close` | open/close a persistent, auto-reconnecting streaming REPL session (stdout -> log + tail buffer; chain mount/exec/cp/soft_reset first) |
| `repl_read` | tail the session's buffered stdout by cursor |
| `repl_send` | inject a REPL command line to stdin and read back its output |
| `repl_interrupt` | send Ctrl-C to the session |
| `repl_list` | list open REPL sessions |
| `i2c_target` | pod acts as a hardware I2C target backing a register file |
| `i2c_target_regs` | read/write that register file from the host |
| `gpio` | read or drive a pod GPIO |
| `adc` | sample a pod ADC channel |
| `peripheral_release` | release a named pod peripheral instance, or all |
| `logic_analyse` | PIO-capture DUT pins on PIO0 (coexists with a live SWD session) and write a VCD file |

The loop an agent runs: edit DUT firmware -> `flash_dut` -> `reset_dut` ->
observe (`dut_exec`, or have the pod present an `i2c_target` / `gpio` the DUT
exercises) -> repeat.

### Persistent REPL session

`pod repl` / the `repl_*` tools hold a long-lived connection to the pod's socket
REPL (built on ampremote's transport), where its asyncio app + aiorepl run. The
target's stdout streams to a log file (the lossless record) and an in-memory
tail buffer; injected lines go to its stdin, so you watch output and run REPL
commands on the same live session. `--device` (CLI) / `device` (MCP) points the
same session at any mpremote device instead, e.g. a DUT CDC tty. A session holds
the pod's single socket-REPL slot for its lifetime, so while it is open use
`repl_send` (not `pod_exec`, which would contend for the slot) to run code.

Chain mpremote-style setup before the connect (like `mpremote mount ./fw exec
"..." repl`): `--mount DIR` / `--exec CODE` / `--cp SRC DST` / `--soft-reset`
(MCP: `mount`/`exec`/`cp`/`soft_reset`). The stateless steps run as ordinary
one-shot verbs first (so `--exec` code must RETURN - a bare loop hangs that
one-shot; start long-running work via `repl_send` after connecting); `mount` is
kept on the session's own connection (the fs hook RPCs back over it, so it
cannot live in a throwaway process), and is the reason to use `repl_open` over
the one-shot `mount_dir`. Mounting briefly enters the raw REPL to install the
hook (like `mpremote mount`), which interrupts a running app's foreground - and
it is re-applied once per reconnect, so on a flapping link with `--mount` expect
one interrupt per restored connection (a reconnect that cannot re-mount keeps
streaming unmounted rather than retrying). `repl_open` reports the real
`mounted` state.

The session is stateful and **auto-reconnects** (like ampremote): the reader
tells a dropped link (the read raises) from an idle gap (a read timeout returns
nothing) and re-establishes the connection with backoff, re-applying the mount,
so long-running logging survives Wi-Fi blips and target reboots. Reconnect
boundaries are marked inline in the stream/log (`[pod-repl: connection dropped
...]` / `[pod-repl: reconnected ...]`), the cursor/log are continuous across
them, and `repl_send` mid-reconnect waits briefly then reports if still down.
`--no-reconnect` (CLI) / `reconnect: false` (MCP) opts out.

`pod repl` is line-oriented (Ctrl-C interrupts the target, Ctrl-D exits, leaving
it running); add `--raw` for a full raw terminal (arrow keys, history, paste) -
which is a one-shot passthrough and cannot chain setup.

**DUT unresponsive / suspected wedged?** First try `reset_dut` (`pod reset
<label>`): a SWD system reset re-inits the target's core *and* peripherals
(including USB), so a DUT whose USB/serial hung (e.g. after a `soft_reset`)
re-enumerates cleanly with no physical replug or power-cycle. Use `--mode halt`
to catch the reset vector. Only resort to a physical power-cycle if the reset
itself errors (SWD not connected). See `../../docs/pod/debug-stack.md`.

## Tests

```bash
cd src/host && python -m pytest -q     # offline; no hardware or network needed
```

Tests cover discovery parsing, the registry, CLI argument handling, and the
client's command construction with an injected runner. The live flash/reset/read
paths are validated against hardware, not in the unit tests.

## USB/IP DUT access

When the DUT's native USB is wired to the **pod's** USB host port, the pod exports
it over USB/IP (TCP 3240) and the host attaches it as a local device. Device
enumeration and attach are validated. A full forwarded DUT REPL over that link has
been demonstrated on hardware (2026-06-18) but is not yet reliable: it succeeds
intermittently and often fails (attach errors, no CDC tty, or a forwarder/Wi-Fi
wedge). Treat the forwarded REPL as landing:

```bash
pod usb lab1                 # list exported devices (live VID:PID + busid)
pod attach lab1              # bring the pod USB host + usbip server up, attach, print the DUT tty
mpremote connect <tty>       # the printed /dev/serial/by-id path -> the DUT REPL (when forwarding succeeds)
pod detach lab1 --port N     # release (N from `usbip port`)
```

`pod attach` returns `{busid, vid, pid, tty}`; the MCP equivalents are `dut_usb`
(list) and `attach_dut`. If the DUT USB instead goes straight to this host
(declared `agent-direct` in the DUT block, `pod dut --dut-usb VID:PID/agent-direct`),
skip USB/IP and connect the by-id tty directly.

> Activating the pod USB host (`pod attach` does this unless `--no-ensure`) drives
> the native USB controller into host mode. It coexists with the Wi-Fi REPL on the
> current firmware, but it is a deliberate action - the REPL is the pod's only
> management channel.

### Host prerequisites

`pod attach`/`detach` use the standard `usbip` client (usbip-utils) and the
`vhci_hcd` kernel module:

```bash
sudo apt install usbip                                       # /usr/bin/usbip
sudo modprobe vhci-hcd                                       # load now
echo vhci-hcd | sudo tee /etc/modules-load.d/vhci-hcd.conf   # and at every boot
```

### Running attach/detach without sudo

`usbip attach`/`detach` need root only to write the vhci sysfs
(`/sys/devices/platform/vhci_hcd.0/attach`). `pod attach` calls them as
`sudo -n usbip ...`, so it runs unattended once one of these once-off configs is
in place. Pick one:

**Option A - scoped passwordless sudo (recommended, most reliable).** A sudoers
drop-in lets `sudo -n usbip` run without a prompt, and grants nothing else:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/bin/usbip" | sudo tee /etc/sudoers.d/pod-usbip
sudo chmod 0440 /etc/sudoers.d/pod-usbip
sudo visudo -c                                               # validate syntax
```

This matches what `pod attach` already does; leave `POD_USBIP_SUDO` unset.

**Option B - no sudo at all, via a udev rule.** The rule ships in this repo at
`src/host/udev/99-usbip.rules`; it makes the vhci attach/detach sysfs attributes
writable by a `usbip` group (the kernel's vhci store handlers do not check
capabilities, so that is sufficient):

```bash
sudo groupadd -f usbip
sudo usermod -aG usbip "$USER"                               # re-login to pick up the group
sudo install -m 0644 src/host/udev/99-usbip.rules /etc/udev/rules.d/99-usbip.rules
sudo udevadm control --reload
sudo modprobe -r vhci-hcd 2>/dev/null; sudo modprobe vhci-hcd   # or reboot, to re-fire the rule
ls -l /sys/devices/platform/vhci_hcd.0/attach                # expect group 'usbip', mode -rw-rw-r--
```

Then tell the pod tooling to skip the sudo prefix by exporting `POD_USBIP_SUDO=0`
in its environment (shell profile, or the MCP server's env). With that set,
`pod attach`/`detach` call the bare `usbip`. If a hardened kernel still rejects
the unprivileged attach, fall back to Option A.

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
hardware-validated end-to-end, including the live Wi-Fi capture round-trip
(capture -> stream -> VCD, reliable on PIO0; the earlier hang was the LA running
on PIO2, the CYW43 Wi-Fi block, now fixed). For DUT wiring and usage, see
"Using the logic analyser" in `../../docs/pod/logic-analyser.md`.
USB/IP DUT access (`pod usb` / `pod attach` / `pod detach`, MCP `dut_usb` /
`attach_dut`) is driven by the standard `usbip` client against the pod's existing
server (see "USB/IP DUT access" above). Device enumeration and attach are
validated; a full forwarded DUT REPL over the link was demonstrated on hardware
(2026-06-18) but is not yet reliable (intermittent - attach errors, no CDC tty, or
a forwarder/Wi-Fi wedge). The attach path is exercised when the DUT's USB is on the
pod host port; on a bench where the DUT enumerates straight to the host it is
`agent-direct` and not used.
`uart_stream` / `telemetry` (Phase 5) remain stubbed and raise with the pending
phase.
