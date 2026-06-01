# RP2350 on-pod debug stack (`annealage_pod.debug`)

The pod debugs a DUT over SWD entirely in MicroPython: a PIO bit transport, an
ADIv5 DP/AP/MEM-AP layer, a per-family flash loader, and a thin high-level
`ops` module that the host `pod` tool drives over the REPL. This is the RP2350
target's replacement for exporting a synthetic CMSIS-DAP probe.

For the host-side `pod` CLI / library / MCP that calls into this, see
`src/host/README.md`. For development gotchas (USB-CDC dev transport, `resume`
+ PIO instruction memory, the nRF52 flash map), see `dev-notes.md`. For the
hardware-validated results, see `spike-findings.md` section 6.

## Hardware

- Pod `GP14` = SWDIO, `GP15` = SWCLK, common GND to the target.
- Default SWCLK is `clkdiv=8` = 9.375 MHz (validated 100/100 clean on an
  nRF52840; 12.5 MHz fails, the PIO input-sampling phase is the limit).
- PIO allocation: CYW43 Wi-Fi uses PIO0, the SWD transport uses PIO1 SM4, the
  optional write-streamer uses PIO2.
- Validated target: nRF52840 (PCA10059). Other Cortex-M targets work at the
  DP/AP/MEM-AP level; flashing currently has only the nRF52 native-NVM path.

## Layers

| Module | Role |
|---|---|
| `swd_pio.SWDPio` | SWD bit transport on a PIO state machine; raw DP/AP read/write, plus inlined `read_drw_block` / `write_drw_block` for fast block transfer. |
| `swd_dap.DebugPort` / `MEMAP` / `CortexM` | ADIv5 debug port (line bring-up, power, SELECT banking, sticky-error recovery, `resync`), MEM-AP (8/16/32-bit + 32-bit block with TAR auto-increment), and Cortex-M halt/resume/reset. |
| `flash_nrf52.NRF52Flash` | nRF52 NVMC flash loader (erase / program / verify) driven through the MEM-AP, bounded-memory chunked. |
| `swd_stream.DRWStreamer` | Experimental, opt-in PIO2 write-streamer (see below). |
| `ops` | High-level entry points the host drives over the REPL. |

## Deployment

The package must be importable on the pod. Two ways:

- Resident (production): copy `__init__.py`, `_version.py`, and `debug/*.py`
  under `/lib/annealage_pod/` on the pod, e.g.

  ```bash
  DEV=/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_<serial>-if00
  mpremote connect "$DEV" resume exec "import os
  for d in ('/lib','/lib/annealage_pod','/lib/annealage_pod/debug'):
      try: os.mkdir(d)
      except OSError: pass"
  mpremote connect "$DEV" resume cp src/mpy/annealage_pod/__init__.py :/lib/annealage_pod/__init__.py
  mpremote connect "$DEV" resume cp src/mpy/annealage_pod/_version.py :/lib/annealage_pod/_version.py
  for f in __init__ swd_pio swd_dap flash_nrf52 swd_stream ops; do
    mpremote connect "$DEV" resume cp "src/mpy/annealage_pod/debug/$f.py" ":/lib/annealage_pod/debug/$f.py"
  done
  ```

- Mounted (development): `mpremote connect <dev> mount src/mpy exec "..."` makes
  the repo package importable without copying. Re-mount after edits. Note the
  `resume` + PIO caveat below.

## `ops` API (the host-facing interface)

`ops` lazily creates one `DebugPort`/`MEMAP`/`CortexM`/`NRF52Flash` session and
reuses it across calls (so repeated host commands do not re-create PIO state
machines). All functions take an optional `clkdiv` (default 8).

- `info() -> {dpidr, cpuid, part, flash_kb, ram_kb}` - identify the target.
- `flash_stream(addr, total_len, port=3333, chunk=4096, verify=True) -> {ok, addr, bytes, err}`
  Open a short-lived TCP receiver on `port`; the host connects and streams the
  image straight into a pod RAM buffer that is erased-once then programmed +
  verified chunk by chunk. Nothing is written to the pod filesystem. This is
  what `Pod.flash_dut` drives.
- `flash_file(addr, path, verify=True, chunk_words=256) -> {ok, addr, bytes, ms}`
  Program from a pod-resident file in bounded chunks. Use when the image is
  already on the pod; otherwise prefer `flash_stream` (no filesystem).
- `dump_stream(addr, length, port=3334) -> {ok, addr, bytes, err}`
  Open a short-lived TCP sender on `port`; reads the target in bounded blocks
  and streams them to the host. The only path that returns target contents.
- `reset(mode="sysreset") -> {ok, mode}` - `sysreset` resets and runs; `halt`
  resets and catches the reset vector.
- `close() -> {ok}` - resume the target and drop the cached session.

Driving it from the host REPL (USB-CDC for development):

```bash
DEV=/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_<serial>-if00
mpremote connect "$DEV" resume exec "import annealage_pod.debug.ops as o; print(o.info())"
# -> {'part': 337984, 'flash_kb': 1024, 'ram_kb': 256, 'dpidr': 731911287, 'cpuid': 1091551809}
```

`flash_stream` / `dump_stream` need a host on the other end of the TCP port, so
they are normally invoked through `Pod.flash_dut` / `Pod.read_dut` rather than a
bare `exec` (which would block at `accept`).

## Lower-level API (advanced / bring-up)

```python
import annealage_pod.debug.swd_dap as dap
dp = dap.DebugPort(swdio=14, swclk=15, sm_id=4)   # clkdiv default 8
dp.connect()                                       # line reset + DPIDR + power-up
ap = dap.MEMAP(dp)
cm = dap.CortexM(ap)
cm.halt()
print(hex(ap.read32(0xE000ED00)))                  # CPUID
words = ap.read_block32(0x10000000, 16)            # block read, TAR auto-increment
cm.resume()

import annealage_pod.debug.flash_nrf52 as fl
flash = fl.NRF52Flash(ap, cm)
flash.program(0x000FF000, open("img.bin","rb").read(), erase=True, verify=True)
```

## Throughput

On the nRF52840 at 9.375 MHz: program ~2900 words/s, block read/verify ~7800
words/s, end-to-end program+verify ~2246 words/s (8.77 KB/s). The write path is
the bound; reads were the original bottleneck until `read_drw_block` was inlined.

## Optional PIO write-streamer (experimental)

`swd_stream.DRWStreamer` runs the whole AP-DRW write per FIFO word on PIO2 and is
opt-in via `NRF52Flash(ap, cm, streamer=DRWStreamer(dp.swd))`. It measured ~3131
vs ~2934 words/s (~7%) for a second PIO block, GP14/15 funcsel switching, and a
per-burst DP resync, so it is disabled by default. Kept because the margin may
matter once other write-path work lands.

## Limitations

- Flashing: only the nRF52 native-NVM path. RP-native (bootrom) and the generic
  CMSIS-FLM loader are not yet built (RP-native needs an RP DUT wired; CMSIS-FLM
  can be brought up on the nRF52840's own FLM).
- SWD clock tops out at 9.375 MHz reliably; >= 10 MHz needs PIO input-phase
  tuning.
- `resume` + editing PIO modules accumulates PIO instruction memory; clear with
  `rp2.PIO(1).remove_program()` or a soft reset (see `dev-notes.md`).
