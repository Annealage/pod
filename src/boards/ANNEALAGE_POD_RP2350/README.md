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
  pod is reached over Wi-Fi.

Status: `machine.USBHost` is compiled in, but the device->host switch and host
enumeration are **not yet verified** on this hardware (no DUT has been on the USB
port). That is a Phase 4 deliverable; see `docs/rp2350/plan/phase-4-usb-host-usbip.md`.

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

## Known local patches

Getting this board to build currently requires changes in the `src/micropython`
submodule that are not yet on its branch (see `docs/rp2350/dev-notes.md` and the
project notes): a `shared/tinyusb/mp_usbh.h` forward-declaration fix, a
`-Werror` workaround in `lib/tinyusb` CDC host, and a pico-sdk pin aligned to
current master. These are tracked for upstreaming into the `machine-usbhost`
branch.
