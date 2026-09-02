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
- Default SWCLK is `clkdiv=16` = 4.69 MHz write / 3.13 MHz read, inside the
  nRF52840's 8 MHz SWDCLK maximum (authoritative: `swd_pio.DEFAULT_CLKDIV`).
  Smaller divisors run over spec: `clkdiv=8` = 9.4 MHz write / 6.25 MHz read
  gives intermittent ACK=3 / parity errors, worst on a cold DUT.
- PIO allocation (authoritative map: `annealage_pod.debug.pio_arbiter.PIO_MAP`):
  CYW43 Wi-Fi runs on PIO2 (reserved, never claimable), the SWD transport uses
  PIO1 SM4, and the logic analyser plus the optional write-streamer use PIO0 (the
  free block). Do not build a state machine on PIO2 - it hard-wedges Wi-Fi.
- Validated target: nRF52840 (PCA10059). Other Cortex-M targets work at the
  DP/AP/MEM-AP level; flashing has the nRF52 native-NVM path plus the generic
  CMSIS-FLM runner (per-target algo required; both validated on the nRF52840
  only). Per-DUT-family status: the "DUT compatibility" table in
  [../website-features.md](../website-features.md).

## Layers

| Module | Role |
|---|---|
| `swd_pio.SWDPio` | SWD bit transport on a PIO state machine; raw DP/AP read/write, plus inlined `read_drw_block` / `write_drw_block` for fast block transfer. |
| `swd_dap.DebugPort` / `MEMAP` / `CortexM` | ADIv5 debug port (line bring-up, power, SELECT banking, sticky-error recovery, `resync`), MEM-AP (8/16/32-bit + 32-bit block with TAR auto-increment), and Cortex-M halt/resume/reset. |
| `flash_nrf52.NRF52Flash` | nRF52 NVMC flash loader (erase / program / verify) driven through the MEM-AP, bounded-memory chunked. The per-family native path. |
| `flm.FLMFlasher` + `flm_<target>.py` | Generic CMSIS flash-algorithm runner: loads a vendor FLM blob into target RAM and calls its Init/EraseSector/ProgramPage. Works for any chip with a CMSIS pack. |
| `swd_stream.DRWStreamer` | Experimental, opt-in PIO0 write-streamer (see below). |
| `dbgsrv` (pod) + `pod.gdbserver` (host) | GDB debugging: a binary debug-command server on the pod (port 3335) plus a host GDB RSP translator, so host `gdb` debugs a DUT through the pod. FPB hardware breakpoints live in `CortexM`. |
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
machines). All functions take an optional `clkdiv` (default `swd_pio.DEFAULT_CLKDIV`
= 16). Passing a different `clkdiv` than the live session rebuilds the SWD
transport at the new clock (the target's halt/breakpoint state is preserved).

Every function below except `stage_flm_blob`/`set_flm_algo`/`flm_algo_info`
also takes an optional `caller` keyword: a label the `Pod` client always
passes, naming whoever asked for the call. Two callers interleaving on the
shared session return wrong data rather than an error, so these entry points
are wrapped in a re-entrancy guard (`ops._guarded`, see
`plan/conflict-legibility.md` item 5): a second caller is refused with
`SwdBusy` while the first genuinely holds it, and for a few seconds after
(`ops.STICKY_S`) even once it has released, closing the gap a multi-call
`pod_exec` sequence opens between calls. `caller=None` (the default when
calling `ops.*` directly, e.g. from a human at the REPL) is never gated -
deliberate god-mode, not an oversight.

- `info() -> {dpidr, cpuid, part, flash_kb, ram_kb}` - identify the target.
- `flash_stream(addr, total_len, port=3333, chunk=4096, verify=True, loader="native") -> {ok, addr, bytes, err}`
  Open a short-lived TCP receiver on `port`; the host connects and streams the
  image straight into a pod RAM buffer that is erased-once then programmed +
  verified chunk by chunk. Nothing is written to the pod filesystem. This is
  what `Pod.flash_dut` drives.
- `flash_file(addr, path, verify=True, chunk_words=256, loader="native") -> {ok, addr, bytes, ms}`
  Program from a pod-resident file in bounded chunks. Use when the image is
  already on the pod; otherwise prefer `flash_stream` (no filesystem).
- `flash_crc(addr, length, clkdiv=16) -> {ok, crc, addr, length, err}` - CRC32 of a
  flash region read back over SWD (MEM-AP block reads, never held whole in RAM).
  The end-to-end integrity check the streaming path lacks: `flash_stream` verifies
  each chunk it programs, but cannot see a chunk lost mid-stream, so `Pod.flash_dut`
  (verify=True, default) re-reads each flashed region and compares this CRC to the
  source, failing loudly on a hole. A silent flash hole is the classic cause of a
  forwarded DUT REPL flooding `0xff` (an erased-`0xff` gap over a printed rodata
  string); an all-`0xff` region returns the CRC of `0xff` bytes, which a real image
  never matches. See `troubleshooting.md` Cause 3.

Both take a `loader` selecting the flash backend: `loader="native"` (default) is
the per-family native path (the validated nRF52 NVMC loader, `flash_nrf52`);
`loader="flm"` runs the generic CMSIS-FLM algorithm in target SRAM (`flm`, see
"Generic CMSIS-FLM flashing" below). Both are validated on the nRF52840.
- `dump_stream(addr, length, port=3334) -> {ok, addr, bytes, err}`
  Open a short-lived TCP sender on `port`; reads the target in bounded blocks
  and streams them to the host. The only path that returns target contents.
- `reset(mode="sysreset") -> {ok, mode}` - `sysreset` resets and runs; `halt`
  resets and catches the reset vector. Both go over SWD.
- `reset(mode="nrst") -> {ok, mode, level}` - pulses the dedicated DUT reset wire
  (pod GP13, `hardware-setup.md` section 5g) instead of going over SWD, so it
  needs no debug session and is the path to try when SWD itself is unavailable:
  target unpowered, wedged, or access-port locked. The pod drives the line
  open-drain, so the **DUT must supply the reset pull-up**; `level` is the line
  after release and a 0 means it stayed low (no DUT pull-up, or the DUT is
  holding its own reset). Implemented in `annealage_pod.debug.nrst`, which also
  exposes `park()` / `assert_reset()` / `release()` / `pulse()` for finer control.

  `nrst.park()` runs from `netboot.main()` at boot and is not optional: an
  RP2350 pad powers up with its internal pull-down enabled, so an unconfigured
  GP13 actively pulls the DUT's reset line down and can hold a DUT in reset from
  power-on. Parking is what makes "nobody is using nRST" mean inert.
- `close() -> {ok}` - resume the target and drop the cached session.

Single-shot register/memory peek-poke (the host `read_reg`/`write_reg`/
`read_mem`/`write_mem` and their MCP/CLI tools drive these). Unlike `gdb_serve`,
these never auto-resume, so a `halt()` holds across calls until `resume()`:

- `halt() -> {ok, halted, dhcsr}` / `resume() -> {ok, halted}` - hold/release the
  core. Required around register access; `halt()` freezes the DUT (incl. its USB).
- `read_reg(regsel) -> {ok, regsel, value}` / `write_reg(regsel, value) -> {ok, ...}`
  Core register access (regsel 0..18: R0..R12, SP=13, LR=14, PC=15, xPSR=16,
  MSP=17, PSP=18). The core must be halted first - a running core returns
  `{ok: False}` (registers go through the debug DCRSR/DCRDR, which need a halt).
- `read_mem(addr, length) -> {ok, addr, length, hex}` (length <= 4096) - a live
  MEM-AP read; works halted or running. For bulk dumps use `dump_stream`.
- `write_mem(addr, hex, protect=None) -> {ok, addr, length}` - a live MEM-AP
  write to RAM / peripherals. `protect` is a list of `[lo, hi)` write-protected
  ranges the host supplies from the declared DUT flash geometry plus the
  Cortex-M code-region floor (see `registry.dut_protect_ranges`); a write
  overlapping one is refused. Without `protect` (a direct REPL caller) it falls
  back to the code-region backstop - everything below `0x20000000`, the
  architectural Cortex-M SRAM base, is flash/ROM and not word-writable. Flash
  programming proper goes through `flash_stream`/`flash_file` where available.
  The aligned/unaligned access is shared with `dbgsrv`.

### Recovering a wedged DUT (do this before a power-cycle)

When the DUT is unresponsive or suspected wedged - hung firmware, a stuck
peripheral, or a `soft_reset` that left its USB/serial enumerated-but-dead - the
first thing to try is a **SWD system reset through the pod**: `ops.reset()` /
`pod dut reset <label>` / the `dut_reset` MCP tool. SYSRESETREQ re-inits the core
*and* peripherals (including the USB controller), so a target whose USB-CDC/REPL
wedged re-enumerates cleanly with no physical replug or power-cycle. Use
`mode="halt"` if you want to catch it at the reset vector instead of running.
Only fall back to a physical power-cycle if the reset itself errors (e.g. SWD not
connected). Validated: an nRF52840 whose USB-CDC REPL hung after a `machine.soft_reset()`
came straight back after `ops.reset(mode="sysreset")` (a soft reset only re-inits
the VM; the full system reset is what re-cycles USB).

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
dp = dap.DebugPort(swdio=14, swclk=15, sm_id=4)   # clkdiv default 16
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

## Generic CMSIS-FLM flashing

The per-family native path (`flash_nrf52`) is fastest where it exists; the
generic path runs a standard CMSIS flash algorithm on the target and works for
any chip with a CMSIS pack. `flm.FLMFlasher` takes an `algo` dict (the blob,
entry points, `begin_data`/`begin_stack`/`static_base`, flash geometry,
`page_size`, and the erase-sector map) sourced from the target's CMSIS Device
Family Pack.

The pod carries no algorithms of its own. The host extracts one from the
target's pack on demand and installs it with `ops.set_flm_algo(algo)`, which
holds it for the VM lifetime; `ops.flm_algo_info()` reports what is installed
without echoing the blob back. Selecting `loader="flm"` with nothing installed
raises. `loader="native"` needs no install and stays the default for
`ops.erase_all` / `ops.flash_file` / `ops.flash_stream`.

The image is shipped in base64 chunks via `ops.stage_flm_blob()` rather than as
one source literal, since a vendor algorithm runs to tens of KB and the pod
would otherwise hold the source text, the decoded bytes and the compiled code
at once.

Host side, `pod.cmsis_pack` resolves the pack (explicit path, then the local
cache, then an opt-in download from the vendor index) and `pod.flm` parses the
`.FLM` - an ELF32 ARM image whose `PrgCode`/`PrgData` sections become the blob,
whose symbols give the entry points, and whose `DevDscr` holds the
`FlashDevice` struct with the geometry and sector map. From the CLI:

```
pod flm <label>                          # what the pod has installed
pod flm <label> --device nRF52840_xxAA   # resolve from the pack cache + install
pod flm <label> --pack /path/to/x.pack   # or from an explicit pack
pod dut flash <label> fw.bin --loader flm    # installs automatically if needed
```

The device name defaults to the DUT's declared `target_family` in the registry.
Packs are cached in `$ANNEALAGE_POD_PACK_CACHE` (default
`~/.cache/annealage-pod/cmsis-packs`).

`page_size` is the *program* granularity (`FlashDevice.szPage`); erase
granularity comes from the `sectors` map, a list of `(offset_from_flash_base,
sector_size)` entries where each applies until the next one's offset. Omitting
it means uniform sectors of `page_size` (correct for the nRF52, wrong for parts
like the STM32F4 whose sectors run 16K/64K/128K).

Each operation re-uploads the algorithm once (`FLMFlasher.reload()`) because
every operation ends by resuming the DUT, which then runs its own firmware over
the load region. Within an operation `load()` is a no-op, so a multi-page
program does not re-upload per page.

Driving it directly once you have an `algo` dict for the target:

```python
import annealage_pod.debug.swd_dap as dap, annealage_pod.debug.flm as flm
dp = dap.DebugPort(swdio=14, swclk=15, sm_id=4); dp.connect()
ap = dap.MEMAP(dp); cm = dap.CortexM(ap)
f = flm.FLMFlasher(ap, cm, algo)  # algo: a CMSIS-FLM algo dict for the target
f.program(0x000FF000, open("img.bin", "rb").read(), erase=True, verify=True)
```

The runner loads the blob into target SRAM, then for each entry point sets the
call frame (R0..R3 args, R9 = static_base, SP = begin_stack, LR = the blob's
BKPT trampoline, PC = entry, xPSR Thumb) and resumes **with interrupts masked**
(`C_MASKINTS`, set while halted then held across the resume; otherwise an
interrupt vectors into the target's firmware and the algo never returns).
Validated on the nRF52840 (FLM erase+program+verify, ~570 ms / 1 KB).

`swd_stream.DRWStreamer` runs the whole AP-DRW write per FIFO word on PIO0 and is
opt-in via `NRF52Flash(ap, cm, streamer=DRWStreamer(dp.swd))`. It measured ~3131
vs ~2934 words/s (~7%) for a second PIO block, GP14/15 funcsel switching, and a
per-burst DP resync, so it is disabled by default. Kept because the margin may
matter once other write-path work lands.

## GDB debugging through the pod (Phase 3)

A host `arm-none-eabi-gdb` debugs a DUT through the pod. Architecture is hybrid:
the pod runs a small, stateless **binary debug-command server** (`dbgsrv`, port
3335, alongside flash 3333 / dump 3334) exposing read/write register, read/write
memory, halt/step/continue, reset, breakpoint set/clear, and an interruptible
`RESUME_WAIT`; a host-side **GDB RSP translator** (`pod.gdbserver`) speaks the
gdb remote protocol to gdb and the binary protocol to the pod. All RSP state,
`target.xml`, the Cortex-M register map and software-breakpoint bookkeeping live
on the host (CPython), so the pod code stays small and the link cost is one
round-trip per gdb operation (gdb's `g`/`m` batch into single commands).

Hardware breakpoints use the Cortex-M FPB (`CortexM.set_breakpoint` /
`clear_breakpoint` in `swd_dap.py`). Ctrl-C interrupt is carried as a framed
flag on the `RESUME_WAIT` re-arm (no out-of-band byte), so a continue is
interruptible within one wait-window without corrupting the wire framing.

Data watchpoints use the Cortex-M DWT. gdb's watchpoint packets map to DWT
comparator FUNCTION codes: `Z2` (write watchpoint) -> FUNCTION 6, `Z3` (read
watchpoint) -> FUNCTION 5, `Z4` (access watchpoint) -> FUNCTION 7. The RSP
translator issues `dbgsrv` `OP_WATCH_SET` / `OP_WATCH_CLEAR`, which drive
`swd_dap.DWT` to program / free a comparator. Comparators are cleared on session
teardown alongside the FPB. Validated on the nRF52840.

Usage:

```bash
pod dut gdb lab1 --listen-port 5005      # starts the pod dbgsrv + a local RSP listener
# then, in another shell:
arm-none-eabi-gdb -q firmware.elf \
    -ex 'target extended-remote 127.0.0.1:5005' \
    -ex 'hbreak main' -ex 'continue'
```

`pod dut gdb` reset-halts the DUT by default (`--no-reset-halt` to attach to a
running target). The session leaves the DUT in a defined state on exit (FPB
cleared) on every path including disconnect. There is also a `gdb` MCP tool.

Validated on the nRF52840: connect + reset-halt, read registers and memory,
set an FPB hardware breakpoint that hits, backtrace, single-step, and continue,
through the pod over Wi-Fi.

## Limitations

- Flashing: nRF52 native-NVM path and the generic CMSIS-FLM path both work
  (validated on the nRF52840). RP-native (bootrom) flashing is not built yet and
  needs an RP DUT wired to validate. Other CMSIS-FLM targets need an `algo` dict
  sourced from that part's CMSIS Device Family Pack (the host-side on-demand
  pack lookup is not built yet) and validation on that silicon.
- SWD clock tops out at 9.375 MHz reliably; >= 10 MHz needs PIO input-phase
  tuning.
- `resume` + editing PIO modules accumulates PIO instruction memory; clear with
  `rp2.PIO(1).remove_program()` or a soft reset (see `dev-notes.md`).
