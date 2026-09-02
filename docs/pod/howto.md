# Pod how-to: task recipes

Short recipes for the common Pod tasks, each with the real command(s) and a link
to the authoritative doc. Every recipe assumes the pod is registered (`pod
register <label> --address <ip>`) and the DUT is wired per
[hardware-setup.md](hardware-setup.md). For the full CLI / Python client / MCP
surface see [src/host/README.md](../../src/host/README.md).

## Flash a DUT image

```bash
pod flash lab1 firmware.bin --addr 0x0    # stream into pod RAM, program over SWD
pod reset lab1 --mode sysreset            # reset and run
```

The image streams straight into pod RAM and is programmed over SWD; nothing is
staged on the pod filesystem. The default `loader="native"` uses the validated
nRF NVMC path; for a non-nRF Cortex-M with a CMSIS pack, use the generic
CMSIS-FLM runner via `loader="flm"` (needs the target algo extracted and
deployed). See [debug-stack.md](debug-stack.md).

## Recover a hung DUT

```bash
pod reset lab1 --mode sysreset    # re-init core + peripherals (incl. USB), run
pod reset lab1 --mode halt        # same, but catch the reset vector
```

A SWD system reset re-cycles the DUT core and its peripherals, so a target whose
USB/serial hung (e.g. after a `soft_reset`) re-enumerates with no replug or
power-cycle. Only power-cycle if the reset itself errors (SWD not connected). See
[debug-stack.md](debug-stack.md).

## Debug over GDB

```bash
pod gdb lab1 --listen-port 5005   # starts pod dbgsrv + a local RSP listener
# in another shell:
arm-none-eabi-gdb -q firmware.elf \
    -ex 'target extended-remote 127.0.0.1:5005' \
    -ex 'hbreak main' -ex 'continue'
```

Hardware breakpoints use the Cortex-M FPB (`hbreak`). Data watchpoints use the
DWT via the gdb `Z2`/`Z3`/`Z4` packets - write / read / access respectively
(gdb's `watch` / `rwatch` / `awatch`). See [debug-stack.md](debug-stack.md).

## Capture DUT signals

```bash
pod la lab1 --pins 16-23 --rate 1e6 --trigger 16:rise --out cap.vcd
```

PIO logic analyser on PIO0; coexists with a live SWD session. Writes a VCD you
open in a waveform viewer. For DUT wiring and options see
[logic-analyser.md](logic-analyser.md).

## Use the pod as an I2C target

```bash
pod i2c-target lab1 --addr 0x42 --regs 0xAB 0xCD   # pod backs a register file
pod i2c-regs lab1 --off 0 --length 2               # read what the DUT wrote
pod i2c-regs lab1 --off 0 --write 0x11 0x22        # seed registers from the host
pod release lab1                                   # tear down
```

The DUT controller reads/writes the pod at `0x42` (`readfrom_mem` /
`writeto_mem`); the target stays up until `pod release`. See
[peripherals.md](peripherals.md).

## Talk to the DUT over USB/IP

```bash
pod usb lab1                 # list exported devices (live VID:PID + busid)
pod attach lab1              # bring up pod USB host + usbip, attach, print DUT tty
mpremote connect <tty>       # the printed by-id path -> the DUT REPL
pod detach lab1 --port N     # release (N from `usbip port`)
```

Enumerate, attach, and the forwarded DUT REPL are all validated, including under
sustained attach/detach churn (100 cycles, 0% Wi-Fi loss). Needs the `usbip`
client and vhci prerequisites below. See
[src/host/README.md](../../src/host/README.md).

Known limitation - CDC-validated only: the forwarder is class-agnostic by design
(it raw-forwards URBs, runs no class drivers), so bulk / MSC / HID devices should
forward, but only the CDC REPL path is hardware-validated. There is a latent
bulk-IN data-toggle desync on an in-flight abort (detaching while a bulk read is
mid-transfer): the rp2 HCD's `hcd_edpt_abort_xfer` does not roll back the
endpoint data toggle and the detach/reattach path does not re-open the endpoint,
so a re-attached bulk endpoint can be one toggle-step out of sync. USB toggle
mismatch self-heals within one packet, so a CDC REPL is unaffected (0 failures in
100 detach-mid-transfer cycles) - but a bulk/MSC transfer could drop a single
packet on the first post-reattach read. The fix (reset the host-side non-control
toggles when the forwarder relays a `SET_CONFIGURATION`) is deferred until a
non-REPL DUT actually exhibits it, since it needs a firmware reflash and cannot
be validated against a reproduction today.

## Prerequisites

**Host toolchain (to build pod firmware and debug DUTs):**
`arm-none-eabi-gcc` (and `arm-none-eabi-gdb` for the GDB recipe), `cmake`, and
`make` / `ninja`.

**Host `pod` tooling (pre-launch, from source):** the repo is private pre-launch,
so clone it and install from the checkout. Needs Python 3.10+, plus network + git
because the `ampremote` dependency is a git-only fork (not on PyPI, pulled at the
commit pinned in `pyproject.toml`):

```bash
cd src/host
uv tool install --editable ".[zeroconf,mcp]" --force   # console scripts: pod, pod-mcp
```

Install it **editable**. A plain (non-editable) install copies the package into
site-packages, so the `pod` on your PATH keeps running that copy while the
checkout moves on: new CLI options are rejected and new modules appear missing,
with nothing to indicate the code being run is stale. Keep the `mcp` extra rather
than installing `mcp` separately - it carries an `mcp<2` cap, and 2.x drops the
decorator API the MCP server is built on, so an uncapped resolve leaves `pod-mcp`
failing at startup with `AttributeError: 'Server' object has no attribute
'list_tools'`. `pip install -e ./src/host[mcp,zeroconf]` works the same way if you
prefer pip.

**USB/IP (Linux host, for the USB/IP recipe):** the standard `usbip` client and
the `vhci_hcd` kernel module, and passwordless `sudo` for `usbip` so `pod attach`
runs unattended:

```bash
sudo apt install usbip
sudo modprobe vhci-hcd
echo "$USER ALL=(root) NOPASSWD: /usr/bin/usbip" | sudo tee /etc/sudoers.d/pod-usbip
```

See the full setup (including the no-sudo udev alternative) in
[src/host/README.md](../../src/host/README.md).

USB/IP attach is the only Linux-bound pod path (Windows has a best-effort
third-party client, usbip-win, untested with the pod; macOS has no USB/IP
client). Every other recipe on this page is plain TCP/Python and
host-OS-agnostic; see the host OS table in
[getting-started.md](getting-started.md).

**Deploying the on-pod `annealage_pod` package:** Python changes deploy by
copying the files to the pod filesystem and reloading - no firmware reflash. See
[dev-notes.md](dev-notes.md).
