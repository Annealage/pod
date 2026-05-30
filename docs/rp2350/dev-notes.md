# RP2350 pod: development gotchas and recipes

Operational learnings from the RP2350 bring-up spike. These cost real debugging
time; read them before working on the RP2350 target so you do not re-learn them.

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

## 2. Flashing RP2350 MicroPython over SWD with probe-rs

The MicroPython RP2350 `.uf2` is multi-section. `probe-rs download
--binary-format uf2` mis-handles it:

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
