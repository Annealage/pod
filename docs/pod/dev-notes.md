# RP2350 pod: development gotchas and recipes

Operational learnings from the RP2350 bring-up spike. These cost real debugging
time; read them before working on the RP2350 target so you do not re-learn them.

For the end-to-end build/flash/provision runbook (and the `config.py` network
configuration), see `src/boards/ANNEALAGE_POD_RP2350/README.md`.

## 1. `mpremote resume` caches imported modules

`mpremote ... resume` connects without resetting the runtime, so `import X`
returns the module already in `sys.modules` even after you have overwritten the
file on the device with `fs cp`. Editing a file, copying it, then re-running via
`resume` silently tests the **old** code.

After any edit, do both:

```bash
mpremote connect "$PORT" resume fs cp file.py :file.py
mpremote connect "$PORT" resume exec "import sys; sys.modules.pop('file', None); import file; ..."
```

or soft-reset the board between copy and test.

When recovering a board whose REPL is wedged (a running program holding stdin),
use a plain `connect` rather than `resume`: `connect` sends Ctrl-C and breaks in,
`resume` does not.

Always address devices and probes by their stable identity, never by a
`/dev/ttyACMx` number, which moves: use `/dev/serial/by-id/...` for serial and
`VID:PID:Serial` for probes. Multiple MicroPython boards and CMSIS-DAP probes are
usually attached at once; a bare `grep MicroPython | head -1` will pick the wrong
board.

## 2. Building and flashing RP2350 MicroPython over SWD

**Building:** `make` (or `make firmware`) from the repo root. A fresh cmake
configure must pass the picotool fetch flag, or the pico-sdk aborts at configure on
the host picotool version gate (`Incompatible picotool installation found`) - before
the qstr / root-pointer collection even runs, which can masquerade as a missing
root pointer / `mp_state_vm_t has no member` error if you only look at the compile
step. The top-level `Makefile` pre-configures the build dir with
`-DPICOTOOL_FORCE_FETCH_FROM_GIT=1 -DPICOTOOL_FETCH_FROM_GIT_PATH=$(PTCACHE)`, so
`make clean && make firmware` works; if you configure by hand, include those flags.

**Use `make flash` (OpenOCD) - it halts BOTH cores first.** probe-rs `download`
halts only core0; if core1 is running (the netboot drives XIP from flash), an
erase interrupted while core1 reads flash wedges the external QSPI chip into
continuous-read mode, which no SWD reset clears - only a power cycle does (this
bricked the pod once; see the flash-XIP-wedge investigation). OpenOCD's rp2350
`reset init` halts cm0 AND cm1 before any flash access, so `make flash` (which
runs `openocd ... program firmware.elf verify reset`) is safe even while the pod
is live, and it flashes the ELF directly so no UF2 flatten is needed. The
probe-rs route below (`make flash-probe-rs`) is a fallback; use it only from a
clean/bootrom state with no running netboot.

The probe-rs route also needs a UF2 workaround. The MicroPython RP2350 `.uf2` is
multi-section. `probe-rs download --binary-format uf2` mis-handles it:

```
WARN probe_rs::flashing::loader: More than 1 section found in UF2 file. Using first section.
```

It flashes only a stray metadata block (family `0xe48bff57`) targeting
`0x10FFFF00`, which is outside the 4 MB flash, and **skips the real ~840 KB
program** (family `0xe48bff59`) at `0x10000000`. The board does not boot, yet may
still enumerate as a MicroPython USB CDC device from a residual image, so "it
shows up on USB" is not proof of a good flash.

Fix: flatten the UF2 to a program-region bin and flash that as raw `bin`.

```python
# uf2 -> program bin (Pico 2 W, 4 MB flash at 0x10000000)
import struct
data = open("RPI_PICO2_W.uf2", "rb").read()
FLASH, END = 0x10000000, 0x10400000
blocks = {}
for i in range(len(data) // 512):
    b = data[i*512:(i+1)*512]
    _, _, flags, addr, size, _, _, fam = struct.unpack("<8I", b[:32])
    if FLASH <= addr < END:
        blocks[addr] = b[32:32+size]
lo = min(blocks); hi = max(a + len(blocks[a]) for a in blocks)
buf = bytearray(b"\xff" * (hi - lo))
for a, d in blocks.items():
    buf[a-lo:a-lo+len(d)] = d
open("prog.bin", "wb").write(buf)
```

```bash
# flash by probe serial, then reset (example serial = the wired pico-probe)
PROBE=2e8a:000c:0501083219160908
probe-rs download --probe "$PROBE" --chip RP235x --binary-format bin \
    --base-address 0x10000000 prog.bin
probe-rs reset --probe "$PROBE" --chip RP235x
```

The pod's own SWD debug header (driven by the wired pico-probe) is separate from
the GPIOs the pod uses to debug a DUT, so reflashing the pod and using the pod as
a debugger do not conflict.

Validate intent before flashing with `picotool info <file>.uf2` (confirms chip,
version, image type) and `probe-rs chip list | grep -i rp235`.

## 3. SWD bit-bang turnaround framing

For the pure-Python bit-bang reference (`prototypes/rp2350-swd-spike/swd_bitbang.py`),
the turnaround framing that proved reliable on real silicon:

- **write -> read** (request to ACK, or ACK to read-data): **0** explicit
  turnaround clocks. The line-direction switch at the start of `read_bits`
  (clock-low half-cycle before the first sample) absorbs the turnaround bit.
- **read -> write** (after read data+parity, or ACK to write-data): **2** SWCLK
  cycles of turnaround. One cycle desyncs the DP; the next transfer floats
  (`ACK = 0b111`).

This is specific to the bit-bang sample-edge phase. The PIO port re-derives its
own timing; do not assume these counts carry over to PIO.

A read of an unexpected but **deterministic** ACK (not `0b111` floating) means
the target is driving the line and the wiring is good; the fault is almost always
turnaround/alignment, not electrical. Capture raw bits and search for the known
IDCODE signature across bit-shifts to find the true alignment fast.

## 4. The pod has a USB-CDC REPL for development, use it

The board is configured `MICROPY_HW_USB_HOST (1)`, but the RP2350 native USB is
device-by-default, host-on-demand: it enumerates as a normal MicroPython USB-CDC
REPL until something calls `machine.USBHost()`. So during development (no DUT on
the host port) the pod is reachable over USB-CDC at
`/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_*-if00`. This is the reliable
dev transport; drive SWD/flash work over it. The Wi-Fi socket REPL is for
deployment (when the USB port is hosting a DUT) and is fine for serial use, but a
single-client `os.dupterm` server with no clean teardown (see below).

Do NOT try to "harden" `netboot._serve` with a `setsockopt(SO_KEEPALIVE)` in the
accept loop: `SO_KEEPALIVE` is not supported on the lwIP socket and raises, and
because the call sits outside the accept `try`, it kills the `_serve` thread on
the first connection every boot, the board comes up Wi-Fi-unreachable. Recover by
fixing `/netboot.py` over the USB-CDC REPL (the VFS `/netboot.py` shadows the
frozen one; `sys.path` is `['', '.frozen', '/lib']`). A proper clean-teardown
server wants a non-blocking accept + per-client EOF handling (webrepl-style), not
a `prev.close()` hack in the accept loop (closing the previous dupterm out from
under the REPL read thread wedges it under connection churn).

## 5. `resume` + PIO code: soft-reset to a clean VM between iterations

`mpremote ... resume` keeps the running VM, so `sys.modules` persists (re-`import`
returns the stale cached module after an edit) AND re-importing PIO code reloads
the `asm_pio` program into PIO instruction memory without freeing the previous
copy, a few iterations exhaust it and `StateMachine()` raises `ENOMEM`. For
iterating on PIO/DAP/flash code over USB-CDC, drop `resume` so mpremote
soft-resets to a clean VM each run (this board's REPL is plain dupterm, not
aiorepl, so soft reset is safe; it just leaves Wi-Fi down until the next hard
reset because `main`/`netboot` does not run after a raw-REPL soft reset). Use
`mpremote connect <dev> mount src/mpy exec "..."` and let it soft-reset.

## 6. nRF52840 dongle flash map, do not flash-test in the FS region

The wired nRF52840 SWD target is itself a MicroPython board (PCA10059). Its
flash: 1 MB, 4 KB pages. The MicroPython nrf filesystem (littlefs) is the top
64 KB, `0xF0000`-`0x100000` (16 blocks). Flashing into that region touches FS
blocks; pick a target region deliberately.

Real flashing is erase + program + verify and never reads the prior contents.
Do NOT wrap flash tests in a read-modify-write that saves the original to pod
RAM and restores it: a MemoryError (or any pod-side fault) between the test write
and the in-RAM restore loses the original. That exact sequence cost a free FS
block's contents during bring-up (littlefs self-healed, both test files survived,
but the saved-in-RAM original was unrecoverable). If a region's prior contents
matter, read them back to a HOST file first with the explicit read-flash command,
that is a separate, deliberate operation, not part of the flash path.

## 7. On-pod flash loader: throughput and memory

- Write path: stream words through the MEM-AP with TAR auto-increment (one TAR
  per 1 KB window) rather than a TAR+DRW pair per word, and skip the per-word
  NVMC `READYNEXT` poll, each SWD write already takes far longer than the ~41 us
  flash write, so the buffer is always drained; one `READY` wait at the end
  confirms commit. This took the loader from ~89 words/s to ~425 words/s
  (1.66 KB/s) at 9.375 MHz. Still Python/SWD-overhead-bound, not clock-bound.
- Memory: `MEMAP.read_block32` materialises a Python list, a full-image block
  read exhausts the pod heap (worse under socket+`mount`, which adds RemoteFS
  overhead). Program and verify in bounded chunks (`flm.FLMFlasher` programs
  one page at a time); for real images stream from a pod-side file rather than
  holding the image in RAM.
- SWD clock: the default is `clkdiv=16` (`swd_pio.DEFAULT_CLKDIV`) = 4.69 MHz
  write / 3.13 MHz read, inside the nRF52840's characterised 8 MHz SWDCLK max.
  The PIO clocks the write phase at 2 cyc/bit and the read phase at 3 cyc/bit, so
  the read side is the tighter limit and `f_swclk` reports only the write clock.
  `clkdiv=8` = 9.4 MHz write / 6.25 MHz read runs over spec: it samples the
  target-driven bits in too narrow a plateau and gives intermittent ACK=3 /
  parity errors (single-bit flips), worst on a cold DUT with the least margin -
  it is not "reliable", it was riding rig margin. Hard cliff at `clkdiv=6` =
  12.5 MHz write (0/100). Reaching a >= 10 MHz write clock cleanly would need
  input-phase PIO tuning (add a settle cycle / restructure the read), not just a
  lower clkdiv. Note: a warm `ops` session ignores a new `clkdiv` unless it
  differs from the live one (then it rebuilds the transport); it is honoured on
  the first `_ensure` or across `ops.close()`.

## 8. CYW43 Wi-Fi is on PIO2, not PIO0 - never build a PIO SM on PIO2

On the RP2350 Pico 2 W the CYW43 wireless SPI runs on **PIO2 SM0**, not PIO0 as
on the RP2040. The pico-sdk claims a free SM that can reach the high-numbered WL
pins (`pio_claim_free_sm_and_add_program_for_gpio_range`), which lands on PIO2.
Confirm on a live pod by reading the PIO enable registers (CTRL bit per SM):

```python
import machine
for blk, b in ((0,0x50200000),(1,0x50300000),(2,0x50400000)):
    print('PIO%d CTRL=0x%08x' % (blk, machine.mem32[b]))   # PIO2 CTRL=0x1 => SM0 live
```

Constructing a `rp2.StateMachine` on PIO2 (or otherwise touching its instruction
memory / SMs) while Wi-Fi is actively servicing a socket **corrupts the running
CYW43 SM and hard-wedges the whole chip**: the REPL dies, Ctrl-C does nothing,
and only a power-cycle (`uhubctl -l 3-1 -p 3 -a cycle`) recovers it. The wedge is
timing-sensitive - it needs the tight, un-yielded SM-construction sequence to
coincide with Wi-Fi activity, so adding `print`/`sleep_ms` between steps masks it
(a Heisenbug). The logic analyser originally defaulted to PIO2 (`sm_id=10`) and
hung exactly this way over Wi-Fi; it now uses **PIO0** (`sm_id=0`).

PIO block map for the pod: **PIO0 = free** (logic analyser), **PIO1 = SWD**,
**PIO2 = CYW43 Wi-Fi (off-limits)**. Any new PIO consumer (SWO, I2C/SPI bitbang,
the optional DRW write-streamer) must use PIO0 or PIO1, never PIO2. The
authoritative map (and the live-register verification recipe) is in code at
`annealage_pod.debug.pio_arbiter.PIO_MAP` - check it before adding a consumer.

To debug a tight-sequence wedge like this, drive the suspect path over the Wi-Fi
REPL and watch the pod over the USB-CDC REPL out-of-band (the two REPLs share the
main thread, so a Ctrl-C on one interrupts the other - but passive reads and
single-checkpoint / truncation probes localise the wedging call without the
yields that mask it).

## 9. Backup UART REPL via the pico-probe (for when USB is host)

Once the native USB is taken for the DUT host, the USB-CDC REPL is gone and Wi-Fi
is the only management channel. As an out-of-band backup, the board enables a
UART REPL (`MICROPY_HW_ENABLE_UART_REPL (1)` in `mpconfigboard.h`): a REPL on
**UART0, GP0 = TX / GP1 = RX, 115200**. It is a separate stdio path, not the
single `os.dupterm` slot (which the Wi-Fi socket REPL uses), so USB-CDC + UART0 +
Wi-Fi all give a REPL at once, and UART0 survives USB switching to host mode.

The pico-probe already exposes a USB-UART bridge as a second CDC interface
(`/dev/serial/by-id/usb-Raspberry_Pi_Debugprobe_*-if01`), so one probe gives both
SWD programming and the backup REPL. The debugprobe bridge is uart1 on the probe
at **GP4 = TX, GP5 = RX**. Cross-wire to the pod:

| Pod UART0 | wire | Probe bridge |
|---|---|---|
| GP0 (TX) | -> | GP5 (RX) |
| GP1 (RX) | <- | GP4 (TX) |
| GND | -- | GND (already common via the SWD link) |

Then `mpremote connect <the probe -if01 by-id> resume` is a REPL into the pod.
(GP0/GP1 are now reserved for this REPL and are not available as DUT/LA capture
pins. The probe-side bridge pins are GP4/GP5 on stock debugprobe firmware, not
GP0/GP1 - verify against your probe build, or reflash it to remap.)

Validated end-to-end (2026-06-08): a full REPL into the pod over the bridge, and
it survives host mode - `machine.USBHost().active(True)` drops the USB-CDC REPL
while the UART REPL keeps working (used it to run `machine.reset()` and recover).
Note `machine.USBHost().active(False)` does NOT re-enumerate the USB-CDC device;
a `machine.reset()` is needed to return to USB-CDC dev mode.

## 10. A `cp` deploy needs a reboot to reach an already-running netboot task,
not just a `sys.modules` purge

Item 1's `sys.modules.pop(...)` fix is per-exec: it makes the *next* `import`
statement re-read the file, which is enough for a one-shot `pod_exec`/`ampremote
exec` that imports the module fresh each call. It does **not** reach a name a
long-running `asyncio` task already bound at import time and holds in its own
closure - `netboot.py`'s `_repl_accept` and `control.serve()` both `import
annealage_pod.holders` once at boot and keep using that object for the rest of
the process's life. Overwriting `holders.py` on the filesystem and popping
`sys.modules` changes what a *fresh* `pod_exec` sees; it does not change what
`control.serve()` is still calling, so its `who()` answers can silently lag the
deployed file (e.g. missing a newly-added read-through key) until the pod is
rebooted. Confirmed 2026-09-03: after a `holders.py` cp mid-session, a fresh
`pod_exec` of `annealage_pod.holders.who()` showed the new usbip key
immediately, while the control port's `who` kept answering without it across
several real usbip attach/detach cycles - only a reboot (`machine.reset()`,
or power-cycle) makes the already-running listener pick it up.

## 11. `sys.modules.pop()` alone does not force a `from package import name`
site to see a redeployed submodule - pop the submodule and re-import it
BEFORE popping/re-importing whatever does `from .. import name`

`from package import name` resolves via `getattr(package, name)` first and
only falls through to a real `sys.modules` lookup if that attribute is
missing; deleting `sys.modules["package.name"]` does not clear `package`'s
own `name` attribute, so a module that reached its dependency with `from ..
import name` keeps its **already-bound** reference even after the dependency
is popped and reimported by someone else. `ops.py`'s `from .. import
_rp2_pinmap, holders` hit exactly this while hardware-validating the phase-7
SWD guard (2026-09-03): after redeploying both `holders.py` and `ops.py` and
popping both from `sys.modules`, importing `ops` FIRST (so its `from ..
import holders` line ran) then explicitly `import annealage_pod.holders as
h` SECOND left two live objects answering to "annealage_pod.holders" -
`ops.holders is h` was `False`, and `ops.holders` (the stale one, still
carrying whatever `annealage_pod`'s `holders` attribute pointed to before
either pop) was missing the just-added `held()`/`age_s()`/`now_ms()`.
Reimporting `holders` *before* reimporting `ops` fixed it (`ops.holders is h`
then `True`) -
package-attribute assignment happens as a side effect of finishing an
`import package.name` statement, so `annealage_pod.holders` only points at
the fresh module once that statement has actually run, and anything
`from`-importing it needs to run afterward, not before. A reboot sidesteps
the ordering question entirely (see item 10) and is the reliable fix when a
redeploy touches more than one module with a `from .. import` relationship
between them.

## 12. Driving `FLMFlasher` directly (outside `ops.py`'s entry points) needs
`reload()`, not `load()`, after any op that resumed the DUT

`FLMFlasher.load()` is deliberately idempotent - it no-ops once `_loaded` is
set, so a multi-page `program()` does not re-upload the algorithm blob per
page. That cache is only valid while the DUT's SRAM at `load_address` still
holds what was last uploaded there. `ops._flm_restore` (the gap-1 fix,
cmsis-flash-completion.md) ends every FLM operation with `cm.sysreset()`, which
reboots the DUT into its own firmware - and that firmware runs over the same
low-SRAM region the algorithm was loaded into, so the blob is gone the moment
the DUT's own code starts running. `ops.py`'s three entry points
(`flash_file`/`flash_stream`/`erase_all`) are safe because each calls
`_flm_begin()` -> `reload()` at the start of its own operation regardless of
what `_require_flm()` handed back (a fresh instance or last operation's
cached one), forcing a re-upload every time.

A script driving `_require_flm()` directly - `flm_validate.py`, or anything at
a REPL - does not get that for free, and `_require_flm` may return the **same
cached `FLMFlasher`** from an earlier operation in the same pod session with
`_loaded` still `True`. Calling `.load()` on it trusts that stale flag and
skips the re-upload; the algorithm's entry points then run against whatever
the DUT's own firmware left in that SRAM, which produces nonsense - hit
hardware-validating gap 2 (2026-09-03) as an `Init()` call hanging with PC
wandered off to `0x20000170` (deep inside neither the algorithm nor the BKPT
trampoline) until `_call`'s timeout fired. Call `.reload()` instead of
`.load()` whenever driving `_require_flm()`'s result directly.
