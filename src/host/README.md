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
pip install -e .[dev]       # + pytest, to run the suite
```

Requirements:
- `ampremote` (Andrew's async `mpremote` fork - improved TCP + raw-REPL
  support). It is NOT on PyPI; `pip install -e .` pulls it straight from GitHub
  at the commit pinned in `pyproject.toml` (the source of truth for the exact
  revision), installing both the `ampremote` console script (the REPL transport)
  and the `mpremote` import (used by the persistent `pod open` session). This
  build has no `resume` subcommand; `connect socket://HOST:PORT exec ...` does
  not soft-reset by default, which is what the client relies on.
- `zeroconf` for mDNS discovery and `mcp` for `pod-mcp`. `mcp` is pinned below
  2.0 as 2.x dropped the decorator API `build_server` uses.
- The pod must have `annealage_pod.debug` resident at `/lib` for the DUT
  flash/reset/read verbs (deploy steps in `../../docs/pod/debug-stack.md`).

You can also run without installing: `cd src/host && python -m pod.cli ...`.

## CLI

The tree mirrors the MCP surface: flat verbs address the pod itself, `pod dut <verb>`
addresses the device under test, and `pod bench <verb>` drives the pod's instruments. MCP
tool `dut_flash` is `pod dut flash`, `bench_la` is `pod bench la`.

The pod itself:

```
pod discover [--timeout S]            browse mDNS (_annealage-pod._tcp) for live pods
pod register <label> --address IP [--repl-port 8266]
pod unregister <label>                remove a pod from the registry
pod list                              show registered pods
pod info <label>                      show a registered pod's details
pod mount <label> <dir>               mount a host directory on the pod
pod exec <label> "<code>"             run MicroPython on the pod, print stdout
pod cp <label> <src> <dst>            copy a file (':path' = pod side)
pod pins <label> [--cached]           show the pod's own DUT-facing pin assignments
pod flm <label> [--force]             report or install the DUT's CMSIS flash algorithm
pod install-udev [--vid f055] [--print]  host setup: make ModemManager ignore forwarded DUTs (needs sudo)
pod open <label> [--log FILE] [--mount DIR] [--exec CODE] [--cp SRC DST] [--soft-reset] [--no-reconnect]
                                      persistent session on the pod's socket REPL: stream stdout to
                                      console+file, type lines to stdin (auto-reconnects across
                                      drops; --mount/--exec/--cp/--soft-reset chain setup first)
pod open-raw <label>                  full raw-terminal passthrough to the pod's socket REPL
pod-mcp                               start the MCP stdio server (separate console script)
```

The device under test:

```
pod dut identify <label> [--dut-* ...] [--adopt]   show / set / verify the wired DUT
pod dut open <label> [device] [--recover] [--log FILE] [--mount DIR] [--exec CODE] [--cp SRC DST]
                                      persistent session on the DUT's CDC tty. Omit the device to
                                      bring the USB/IP link up and use the tty it returns, which
                                      activates the pod USB host and can disturb its Wi-Fi.
                                      --recover un-sticks a DUT latched in raw mode (Ctrl-C +
                                      Ctrl-B) before connecting. Setup chained with --exec/--cp/
                                      --soft-reset runs on the DUT, not the pod
pod dut exec <label> "<code>"         run MicroPython on the DUT once
pod dut flash <label> <image> [--addr 0xADDR] [--target T] [--loader native|flm]
pod dut erase <label> [--loader native|flm]        erase the entire DUT flash
pod dut reset <label> [--mode sysreset|halt]
pod dut halt <label>                  halt the DUT core over SWD (hold; no auto-resume)
pod dut resume <label>                resume the DUT core over SWD
pod dut reg <label> <reg> [value]     read a core register over SWD, or write it when value is given
                                      (core halted; reg 0..18 or pc/sp/lr/..)
pod dut mem <label> <addr> [len] [--data HEX] [--out PATH]
                                      read DUT memory over SWD as hex (<= 4096 bytes inline, or
                                      unbounded to --out), or write it with --data (RAM/peripherals;
                                      flash refused)
pod dut gdb <label> [--listen-port N] [--gdb-port 3335] [--no-reset-halt] [--resume-window-ms 200]
                                      (GDB path supports DWT data watchpoints: Z2/Z3/Z4 = write/read/access, plus FPB breakpoints)
pod dut link <label> [status|up|down|reprobe] [--no-ensure] [--port N]
                                      status (default) lists what the pod exports and which vhci
                                      ports are attached, and starts nothing; up attaches and prints
                                      the DUT tty; down detaches (--port N from `usbip port`, else
                                      every port for this pod); reprobe re-seeds a stale export
```

The pod's instruments, pointed at the DUT:

```
pod bench gpio <label> <pin> [--value 0|1] [--pull up|down]   read or drive a pod GPIO
pod bench adc <label> <pin>                                   sample a pod ADC channel
pod bench la <label> [--pins 16-23] [--rate 1e6] [--depth N] [--trigger 16:rise] [--out cap.vcd]
pod bench device <label> [up|status|down] --bus i2c|spi
                          i2c: [--addr 0x42] [--regs 0xAB 0xCD ...] [--i2c-bus 1] [--scl 11] [--sda 10]
                          spi: [--mode 0..3] [--miso 16] [--mosi 19] [--sck 18] [--cs 17]
                               [--personality stream|regfile] [--table-size N]
                          down with --name '*' releases every instance
pod bench device-regs <label> --bus i2c|spi [--off 0] [--length N] [--write 0xAB ...] [--table read|write]
                                      read/write that device's register file
pod bench uart <label> [--port PORT] [--duration S] [--tx] [--out FILE]
                                      stream the DUT's UART over TCP
```

The registry lives at `~/.config/pod/pods.json` (override with `POD_CONFIG_DIR`).

### End-to-end example

```bash
pod discover                                  # find the pod on the network
pod register lab1 --address 192.168.0.121     # label it
pod info lab1
pod dut flash lab1 firmware.bin --addr 0x0        # stream-flash the DUT over Wi-Fi
pod dut reset lab1 --mode sysreset                # reset and run
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
  so `pod bench device --bus i2c` brings it up and it stays up until
  `pod bench device ... down`.

These map to `annealage_pod.peripherals` on the pod (see
`../../docs/pod/peripherals.md`).

```bash
# Pod becomes an I2C device at 0x42 backing a register file [0xAB, 0xCD, ...].
# The DUT controller reads it with readfrom_mem(0x42, off, n); each side sees
# the other's writes. Bench wiring is I2C1, SCL=GP11, SDA=GP10.
pod bench device lab1 up --bus i2c --addr 0x42 --regs 0xAB 0xCD
pod bench device-regs lab1 --bus i2c --off 0 --length 2   # inspect what the DUT wrote
pod bench device-regs lab1 --bus i2c --off 0 --write 0x11 0x22   # seed from the host
pod bench gpio lab1 25 --value 1                       # drive GP25 high
pod bench gpio lab1 16 --pull up                       # read GP16 with a pull-up
pod bench adc lab1 26                                  # -> {'u16': ..., 'volts': ...}
pod bench device lab1 down --name '*'            # tear down all pod peripherals
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

28 tools in the same three groups as the CLI.

| Tool | Action |
|---|---|
| `pod_discover` | browse mDNS for live pods |
| `pod_register` | register a pod under a label |
| `pod_info` | registry info for a label, plus this process's open sessions |
| `pod_exec` | run MicroPython on the pod's own interpreter |
| `pod_mount` | mount a host directory on a pod (one-shot; unmounts on return) |
| `pod_open` | persistent session on the pod's socket REPL; returns the session id the `session_*` verbs take |
| `dut_open` | persistent, auto-reconnecting session on the DUT's CDC tty, returning its session id. With no `device` it brings the USB/IP link up and uses that tty. `recover` un-sticks a raw-latched DUT first; `mount`/`exec`/`cp`/`soft_reset` chain setup on the DUT |
| `session_send` | write to a session's stdin (by session id) and read back its output, or send Ctrl-C / Ctrl-B / Ctrl-D via `control` |
| `session_read` | tail that session's buffered stdout by cursor |
| `session_close` | close it, leaving the target running |
| `dut_exec` | run MicroPython on the DUT once, over an open session when there is one and otherwise over a fresh attach; reports which as `via`, with `returncode` either way |
| `dut_identify` | show, set, or verify the wired DUT (declared vs live IDs) |
| `dut_halt` / `dut_resume` | halt/resume the DUT core over SWD (no auto-resume; halt freezes the DUT) |
| `dut_reg` | read a core register over SWD, or write it when `value` is given (core must be halted) |
| `dut_mem` | read DUT memory over SWD inline as hex or streamed to `out_path`, or write it when `data` is given (live MEM-AP; flash refused) |
| `dut_gdb` | start a local GDB RSP server to the DUT and return its endpoint (supports DWT data watchpoints via gdb Z2/Z3/Z4 = write/read/access, plus FPB hardware breakpoints) |
| `dut_flash` | flash a DUT image (streamed into pod RAM, no pod FS); refuses another caller's live USB/IP session unless `force` |
| `dut_erase` | erase the entire DUT flash; refuses another caller's live USB/IP session unless `force` |
| `dut_flm` | report the DUT's installed CMSIS flash algorithm, or resolve and install one (`device`/`pack`/`download`+`vendor`+`pack_name`/`force`) - the only way to fetch a pack from the vendor index; `dut_flash`/`dut_erase`'s `loader="flm"` only resolves from the local cache |
| `dut_reset` | reset the DUT (`sysreset` / `halt`); refuses another caller's live USB/IP session unless `force` |
| `dut_link` | `status` (pure read) lists exports and attached vhci ports; `up` attaches and returns the DUT tty; `down` detaches (this host's own ports only); `reprobe` re-seeds a stale export, refusing another host's live import unless `force` |
| `bench_gpio` | read or drive a pod GPIO |
| `bench_adc` | sample a pod ADC channel |
| `bench_la` | PIO-capture DUT pins on PIO0 (coexists with a live SWD session) and write a VCD file |
| `bench_device` | present the pod as an I2C or SPI device on the DUT's bus (`bus`), with `action` `up` / `status` / `down`; `down` with no name releases only instances this caller brought up, `force` sweeps every instance |
| `bench_device_regs` | read/write that device's register file from the host |
| `bench_uart` | stream the DUT's UART over TCP |

The loop an agent runs: edit DUT firmware -> `dut_flash` -> `dut_reset` ->
observe (`dut_open` + `session_send`, or have the pod present a `bench_device` /
`bench_gpio` the DUT exercises) -> repeat.

Only `dut_link(action="up")` can activate the pod's USB host, which has been
observed to disturb the pod's Wi-Fi link, its only management channel. The
`status` default starts nothing on the pod.

### Persistent REPL session

`pod open` / `pod_open` holds a long-lived connection to the pod's socket REPL
(built on ampremote's transport), where its asyncio app + aiorepl run. The
target's stdout streams to a log file (the lossless record) and an in-memory
tail buffer; injected lines go to its stdin, so you watch output and run REPL
commands on the same live session. `pod dut open` / `dut_open` points the same
machinery at a DUT CDC tty instead. Either way the session is then driven by the
same verbs: `session_send`, `session_read`, `session_close`.

A pod session holds the pod's single socket-REPL slot for its lifetime, so while
it is open use `session_send` rather than `pod_exec`, which would contend for the
slot. A DUT session rides the DUT's own tty, so a pod session and any number of
DUT sessions coexist; each is identified by the session id its open call returns,
derived from what it is attached to (`<label>:pod`, `<label>:dut:<tty>`), so
re-opening the same target hands back the session already held.

Two consequences worth knowing. Bringing the USB/IP link up runs code on the pod,
so `dut_open` with no `device` needs that same REPL slot: with a pod session open,
either pass a `device` from a link already up, or bring the link up first. And the
session store is per-process, so what a session listing shows is this process's
own sessions, not the pod's state; another agent's sessions are invisible here and
an empty list does not mean the pod is free.

Chain mpremote-style setup before the connect (like `mpremote mount ./fw exec
"..." repl`): `--mount DIR` / `--exec CODE` / `--cp SRC DST` / `--soft-reset`
(MCP: `mount`/`exec`/`cp`/`soft_reset`). The stateless steps run as ordinary
one-shot verbs first (so `--exec` code must RETURN - a bare loop hangs that
one-shot; start long-running work via `session_send` after connecting); `mount` is
kept on the session's own connection (the fs hook RPCs back over it, so it
cannot live in a throwaway process), and is the reason to use `pod_open` /
`dut_open` over the one-shot `pod_mount`. Mounting briefly enters the raw REPL to install the
hook (like `mpremote mount`), which interrupts a running app's foreground - and
it is re-applied once per reconnect, so on a flapping link with `--mount` expect
one interrupt per restored connection (a reconnect that cannot re-mount keeps
streaming unmounted rather than retrying). The open call reports the real
`mounted` state.

The session is stateful and **auto-reconnects** (like ampremote): the reader
tells a dropped link (the read raises) from an idle gap (a read timeout returns
nothing) and re-establishes the connection with backoff, re-applying the mount,
so long-running logging survives Wi-Fi blips and target reboots. Reconnect
boundaries are marked inline in the stream/log (`[pod-repl: connection dropped
...]` / `[pod-repl: reconnected ...]`), the cursor/log are continuous across
them, and `session_send` mid-reconnect waits briefly then reports if still down.
`--no-reconnect` (CLI) / `reconnect: false` (MCP) opts out.

`pod open` is line-oriented (Ctrl-C interrupts the target, Ctrl-D exits, leaving
it running); add `--raw` for a full raw terminal (arrow keys, history, paste) -
which is a one-shot passthrough and cannot chain setup.

**DUT REPL silent or won't enter raw repl?** First try `dut_open(recover=true)`
(`pod dut open <label> <tty> --recover`): it sends Ctrl-C then Ctrl-B over the DUT tty to
break a running program and leave a stuck RAW repl for the friendly one -
non-destructive, no reset. If the REPL is silent rather than mode-stuck, it is
usually ModemManager on the host probing the DUT tty and toggling its DTR off
(MicroPython gates stdout on DTR); install the ignore rule once with `sudo pod
install-udev` and re-attach so it applies at enumeration.

**DUT unresponsive / suspected wedged?** Then `reset_dut` (`pod dut reset <label>`):
a SWD system reset re-inits the target's core *and* peripherals (including USB),
so a DUT whose USB/serial hung (e.g. after a `soft_reset`) re-enumerates cleanly
with no physical replug or power-cycle. Use `--mode halt` to catch the reset
vector. Only resort to a physical power-cycle if the reset itself errors (SWD not
connected). Full recovery ladder + the ModemManager fix:
`../../docs/pod/troubleshooting.md`; SWD debug stack: `../../docs/pod/debug-stack.md`.

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
enumeration, attach, and the forwarded DUT REPL are validated, and stay reliable
under sustained attach/detach churn (the earlier Wi-Fi churn wedge is fixed). One
caveat: if the DUT re-enumerates (reset / replug / re-flash) the export slot can
go stale - a fresh attach then floods `0xff`-then-quiet, or the host logs
`string descriptor 0 read error: -19`. Run `pod dut link <label> reprobe` (or
`usbip.stop()/start()` on the pod) to refresh it; see
`docs/pod/troubleshooting.md`.

```bash
pod dut link lab1 status     # list exported devices (live VID:PID + busid); starts nothing
pod dut link lab1 up         # bring the pod USB host + usbip server up, attach, print the DUT tty
mpremote connect <tty>       # the printed /dev/serial/by-id path -> the DUT REPL
pod dut link lab1 down --port N   # release (N from `usbip port`)
```

`pod dut link <label> up` returns `{busid, vid, pid, tty}`; the MCP equivalent is
`dut_link` with `action="status"` to list and `action="up"` to attach. If the DUT USB instead goes straight to this host
(declared `agent-direct` in the DUT block, `pod dut --dut-usb VID:PID/agent-direct`),
skip USB/IP and connect the by-id tty directly.

> Activating the pod USB host (`pod dut link <label> up` does this unless `--no-ensure`) drives
> the native USB controller into host mode. It coexists with the Wi-Fi REPL on the
> current firmware, but it is a deliberate action - the REPL is the pod's only
> management channel.

### Host prerequisites

`pod dut link up`/`down` use the standard `usbip` client (usbip-utils) and the
`vhci_hcd` kernel module:

```bash
sudo apt install usbip                                       # /usr/bin/usbip
sudo modprobe vhci-hcd                                       # load now
echo vhci-hcd | sudo tee /etc/modules-load.d/vhci-hcd.conf   # and at every boot
```

### Running attach/detach without sudo

`usbip attach`/`detach` need root only to write the vhci sysfs
(`/sys/devices/platform/vhci_hcd.0/attach`). `pod dut link up` calls them as
`sudo -n usbip ...`, so it runs unattended once one of these once-off configs is
in place. Pick one:

**Option A - scoped passwordless sudo (recommended, most reliable).** A sudoers
drop-in lets `sudo -n usbip` run without a prompt, and grants nothing else:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/bin/usbip" | sudo tee /etc/sudoers.d/pod-usbip
sudo chmod 0440 /etc/sudoers.d/pod-usbip
sudo visudo -c                                               # validate syntax
```

This matches what `pod dut link up` already does; leave `POD_USBIP_SUDO` unset.

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
`pod dut link up`/`down` call the bare `usbip`. If a hardened kernel still rejects
the unprivileged attach, fall back to Option A.

## Status

`discover` / `list` / `register` / `info` / `open` / `mount` / `exec` / `cp`,
`flash_dut` / `reset_dut` / `read_dut`, and `gdb` / `gdb_endpoint` (Phase 3) are
implemented and validated on an nRF52840 DUT. The DUT-facing peripherals
(`i2c_target` / `i2c_target_regs` / `gpio` / `adc` / `peripheral_release`,
Phase 5) are
implemented, host-unit-tested, and hardware-validated end-to-end on the pod + an
nRF52840 controller: the pod presents the register file via the helper, the DUT
reads it (`readfrom_mem`) and writes it (`writeto_mem`), and the host reads back
what the DUT wrote.
The PIO logic analyser (`logic_analyse` / `pod bench la`, Track 2) is implemented and
hardware-validated end-to-end, including the live Wi-Fi capture round-trip
(capture -> stream -> VCD, reliable on PIO0; the earlier hang was the LA running
on PIO2, the CYW43 Wi-Fi block, now fixed). For DUT wiring and usage, see
"Using the logic analyser" in `../../docs/pod/logic-analyser.md`.
USB/IP DUT access (`pod dut link` with `status` / `up` / `down`, MCP `dut_link`)
is driven by the standard `usbip` client against the pod's existing
server (see "USB/IP DUT access" above). Device enumeration and attach are
validated, including the forwarded DUT REPL. One caveat: sustained USB-host
attach/detach churn can wedge the pod's Wi-Fi (a separate known issue) and needs a
reset. The attach path is exercised when the DUT's USB is on the pod host port; on a
bench where the DUT enumerates straight to the host it is `agent-direct` and not used.
`uart_stream` / `telemetry` (Phase 5) remain stubbed and raise with the pending
phase.
