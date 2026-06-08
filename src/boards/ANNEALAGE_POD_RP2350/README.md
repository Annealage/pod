# Annealage Pod RP2350 board

MicroPython board variant for the RP2350 (Pico 2 W) pod. Raspberry Pi Pico 2 W
base: CYW43 Wi-Fi + Bluetooth, with the native USB controller usable as a host
for the DUT (`machine.USBHost`, host-on-demand). See `docs/rp2350/` for the
pivot plan and findings, and `docs/rp2350/dev-notes.md` for build/flash gotchas.

This board is for a bare Pico 2 W plus jumper wires to a DUT. Carrier-hardware
capabilities (INA228 power telemetry, power-rail switching, level translation)
are gated on a future custom PCB and are not present here.

## USB and reachability

The RP2350 has one USB controller, so it is device or host, not both. It is a
USB-CDC **device** (REPL) at boot, and is intended to switch to **host** mode
only when `machine.USBHost()` is activated. So:

- At boot/idle the pod has a normal USB-CDC REPL over USB.
- While actively hosting a DUT (USB in host mode) there is no USB-CDC REPL; the
  pod is reached over Wi-Fi (§ Reach the pod over Wi-Fi) and the backup UART REPL
  (§ Reach the pod over UART).

Status: the device->host switch is verified (2026-06-08): `machine.USBHost().active(True)`
drops the USB-CDC REPL, and both the Wi-Fi REPL and the UART REPL keep working
through it (`active(False)` does not re-enumerate the CDC; a `machine.reset()`
restores device mode). Host *enumeration* of a DUT on the port is still pending
(no DUT has been on the USB port yet); that is a Phase 4 deliverable, see
`docs/rp2350/plan/phase-4-usb-host-usbip.md`.

The frozen boot module (`netboot.py`, started by the frozen `main.py`) brings up
Wi-Fi and exposes the REPL on a TCP socket via `os.dupterm`, so the pod is
reachable over Wi-Fi regardless of USB mode.

## Configuration (`config.py`)

Credentials live in a `config.py` on the device filesystem. It is **not** frozen
into firmware and **not** committed (gitignored), so it changes without a
rebuild. Create one from the template:

```
cp config.example.py config.py     # then edit WIFI_SSID / WIFI_PASSWORD
```

Fields (`config.example.py`):

- `WIFI_SSID`, `WIFI_PASSWORD` - station credentials.
- `REPL_PORT` - TCP port for the dupterm REPL (default 8266).

`netboot.start()` reads `config.py` at boot; if it is missing it prints a notice
and skips Wi-Fi bring-up (leaving USB-CDC as the only transport).

## Build

Toolchain: `arm-none-eabi-gcc`, `cmake`, `ninja`/`make`. From the MicroPython
submodule's `ports/rp2`, with this repo's board directory passed as `BOARD_DIR`.

Initialise submodules (the SDK's picotool check needs the fetch flag; see
`docs/rp2350/dev-notes.md` for why):

```
BOARD_DIR=<repo>/src/boards/ANNEALAGE_POD_RP2350
PTCACHE=~/.cache/picotool-sdk
cmake -S . -B build/submodules -DUPDATE_SUBMODULES=1 \
    -DMICROPY_BOARD=ANNEALAGE_POD_RP2350 -DMICROPY_BOARD_DIR="$BOARD_DIR" \
    -DPICOTOOL_FORCE_FETCH_FROM_GIT=1 -DPICOTOOL_FETCH_FROM_GIT_PATH="$PTCACHE"
```

Configure and build:

```
cmake -S . -B build-ANNEALAGE_POD_RP2350 -DPICO_BUILD_DOCS=0 \
    -DMICROPY_BOARD=ANNEALAGE_POD_RP2350 -DMICROPY_BOARD_DIR="$BOARD_DIR" \
    -DPICOTOOL_FORCE_FETCH_FROM_GIT=1 -DPICOTOOL_FETCH_FROM_GIT_PATH="$PTCACHE"
make -C build-ANNEALAGE_POD_RP2350 -j"$(nproc)"
```

Output: `build-ANNEALAGE_POD_RP2350/firmware.{uf2,elf}`.

## Flash (via a wired CMSIS-DAP probe, by serial)

`probe-rs` mis-parses the multi-section RP2350 UF2, so flash the program region
as a flat bin (this preserves the filesystem, including `config.py`). Reference
the probe by serial. Flatten and flash:

```
python3 - <<'PY'
import struct
d=open("build-ANNEALAGE_POD_RP2350/firmware.uf2","rb").read()
FLASH,END=0x10000000,0x10400000; b={}
for i in range(len(d)//512):
    blk=d[i*512:(i+1)*512]; _,_,_,a,s,_,_,_=struct.unpack("<8I",blk[:32])
    if FLASH<=a<END: b[a]=blk[32:32+s]
lo=min(b); hi=max(a+len(b[a]) for a in b); buf=bytearray(b"\xff"*(hi-lo))
for a,x in b.items(): buf[a-lo:a-lo+len(x)]=x
open("/tmp/pod_prog.bin","wb").write(buf)
PY

PROBE=<vid:pid:serial>            # e.g. 2e8a:000c:<probe-serial>
probe-rs download --probe "$PROBE" --chip RP235x --binary-format bin \
    --base-address 0x10000000 /tmp/pod_prog.bin
probe-rs reset --probe "$PROBE" --chip RP235x
```

First-time provisioning can also use BOOTSEL + UF2. The filesystem (and
`config.py`) survives a program-region flash; a full chip erase does not.

The same CMSIS-DAP probe also carries the backup UART REPL (see § Reach the pod
over UART), so one probe over one USB cable gives both flashing (SWD) and an
out-of-band REPL.

## Reach the pod over Wi-Fi

After boot, `netboot` connects Wi-Fi and serves the REPL on `REPL_PORT`. Drive it
with `ampremote` (mpremote with the `socket://` transport):

```
ampremote connect socket://<pod-ip>:8266 repl
ampremote connect socket://<pod-ip>:8266 exec "import sys; print(sys.implementation._build)"
ampremote connect socket://<pod-ip>:8266 mount <local-dir> exec "..."
```

mDNS service discovery (so the IP need not be known) is a planned addition; see
`docs/rp2350/plan/phase-1-foundation.md`.

## Reach the pod over UART (backup REPL)

A serial REPL on **UART0 (GP0 = TX, GP1 = RX, 115200)** is built in
(`MICROPY_HW_ENABLE_UART_REPL` in `mpconfigboard.h`). It is a separate stdio path
from USB-CDC and the Wi-Fi `os.dupterm` slot, so all three give a REPL at once,
and - the point - **it keeps working when the native USB is in host mode** (no
USB-CDC). It is the out-of-band channel for developing/debugging the pod while it
hosts a DUT, or when Wi-Fi is down.

Reach it through the wired CMSIS-DAP probe's USB-UART bridge (the probe's second
CDC interface), cross-wired to the pod's UART0:

| Pod UART0 | wire | Probe bridge (debugprobe `uart1`) |
|---|---|---|
| GP0 (TX) | -> | GP5 (RX) |
| GP1 (RX) | <- | GP4 (TX) |
| GND | -- | GND (already common via the SWD link) |

```
# the probe's UART bridge enumerates as its second CDC interface (-if01)
UART=/dev/serial/by-id/usb-Raspberry_Pi_Debugprobe_*-if01
mpremote connect "$UART" resume repl
mpremote connect "$UART" resume exec "import os; print(os.uname().machine)"
```

Verified end-to-end (2026-06-08): a full REPL into the pod over this path, and it
survives `machine.USBHost().active(True)` (USB-CDC drops, UART REPL stays up).
GP0/GP1 are reserved for this REPL - not available as DUT/LA pins. The probe's
bridge pins are GP4/GP5 on stock debugprobe firmware; verify yours. See
`docs/rp2350/dev-notes.md` § 9.

## How to develop the pod firmware: the connections at a glance

| Channel | Wire | Use |
|---|---|---|
| **SWD** (program) | probe SWCLK/SWDIO -> pod SWD pads | flash/erase/reset the pod firmware (§ Flash); `probe-rs reset` recovers a hung pod |
| **USB-CDC REPL** | pod native USB -> host | primary dev REPL, but only in device mode (gone once the USB hosts a DUT) |
| **Wi-Fi REPL** | over the air (`socket://...:8266`) | management REPL, works in any USB mode (§ Reach the pod over Wi-Fi) |
| **UART REPL** | pod GP0/GP1 <-> probe GP5/GP4 | backup REPL, works in any USB mode incl. host (§ above) |

USB-CDC and Wi-Fi were the existing channels; the UART REPL adds an out-of-band
serial path so the pod stays reachable when the native USB is taken for the DUT.

## Known local patches

Getting this board to build currently requires changes in the `src/micropython`
submodule that are not yet on its branch (see `docs/rp2350/dev-notes.md` and the
project notes): a `shared/tinyusb/mp_usbh.h` forward-declaration fix, a
`-Werror` workaround in `lib/tinyusb` CDC host, and a pico-sdk pin aligned to
current master. These are tracked for upstreaming into the `machine-usbhost`
branch.
