# RP2350 pod: DUT-facing peripherals (`annealage_pod.peripherals`)

The pod can present peripherals to the DUT - act as an I2C target the DUT's
controller talks to, drive or read GPIO, sample an ADC - so an agent can
exercise the DUT's peripheral drivers from the other side of the wire. This is
the pod's "be a device on the DUT's bus" capability, distinct from the debug
stack (`annealage_pod.debug`) that drives the DUT over SWD.

For the host CLI / client / MCP that drives this, see `../../src/host/README.md`.

## Philosophy: thin passthrough, a few curated helpers

MicroPython's `machine` module already exposes the whole peripheral surface, so
the pod doesn't hide it behind an abstraction. The base mechanism is just
running `machine` code on the pod over the REPL (`pod exec` / the `dut_exec` MCP
tool); anything `machine` can do, an agent can do that way.

`annealage_pod.peripherals` adds curated helpers only where a raw one-liner is
not enough - chiefly the cases that must persist across calls. An I2C target
services the bus autonomously in the background, so it has to outlive the REPL
call that created it. The module keeps such instances in a registry (`_INST`,
name -> (object, backing-buffer)) that survives across `pod` commands under the
persistent REPL, and exposes `release(name='*')` to tear them down and
`instances()` to list them.

## I2C target

`i2c_target(addr=0x42, regs=None, bus=1, scl=11, sda=10, size=256, name=...)`
brings up `machine.I2CTarget` in mem-mode: the pod is an I2C device at `addr` on
hardware I2C `bus`, backing a `size`-byte register file. The DUT controller
reads and writes that buffer as an auto-addressed register file - e.g.
`readfrom_mem(addr, off, n)` returns `buf[off:off+n]`, and a write sets the
pointer then stores. Hardware does address-match, ACK, clock-stretch, and the
repeated-start of `readfrom_mem` in silicon.

`i2c_target_regs(off, length, write, name)` reads or writes the backing buffer
from the pod side, so a test can seed registers or inspect what the controller
wrote.

This is the RP2350 realisation of the ESP32-S3 `slaveio` register-table model
(`annealage_pod.slave`): the mem buffer is the register file, but it rides the
built-in `machine.I2CTarget` rather than a custom C module. A PIO I2C slave was
tried and abandoned - the full target doesn't fit a PIO block (the byte engine
plus a START/STOP detector overruns the 32-instruction limit), and the hardware
peripheral does the address-match / ACK / clock-stretch / repeated-start
properly anyway.

### Bench wiring

Hardware I2C1: pod **GP11 = SCL**, **GP10 = SDA** (for the validated nRF52840
bench: nRF P1.10/SCL -> pod GP11, P1.13/SDA -> pod GP10). Hardware-I2C pin
parity is fixed within an instance (even pins = SDA, odd = SCL), so the wiring
has to match an instance's pins - which is why I2C1's GP10/GP11 pair is used
rather than arbitrary pins. (A bus-event trace recipe using the IRQ-model
target as a logger lives in the dev notes / the nRF bench writeup.)

## SPI target

`spi_target(mode=0, bits=8, miso=16, mosi=19, sck=18, cs=17, size=1024,
personality="stream", table_size=256, name=...)` makes the pod an SPI
*peripheral* (the DUT is the controller) on one PIO0 state machine, with DMA
moving the byte streams so no MicroPython runs in the per-byte data path (the
single-core cooperative runtime cannot afford a per-byte pump). 8-bit frames,
MSB-first, SPI modes 0-3 (CPOL/CPHA selected at assembly time).

Two personalities:

- **stream** (default): MISO replays a 0..255 counter via a ring-read DMA over a
  256-entry table, so the controller reads a predictable counter for any
  transfer length with no growing buffer; MOSI is captured into a bounded
  `size`-byte overwrite ring. `bytes_rx` (the DMA transfer-count register) is the
  exact verification metric; `captured` exposes the ring tail.
- **regfile**: a `[reg_ptr][data...]` register-file responder over `table_size`
  bytes each way, the SPI analogue of the I2C target. A write CS stores its
  payload into `write_table[offset+]` and repoints the pointer; a following read
  CS serves `read_table[offset+]`. Bit7 of the command byte is the read/write
  flag, the low 7 bits the offset. The pointer is moved only by the CS-deassert
  soft IRQ, which parses the transaction and repoints the MISO DMA, never
  mid-transfer - so a same-CS write-then-read turnaround is out of scope.

`spi_target_status(name)` returns the live counters (bytes_rx, transfers_total,
last_cs_len, and in regfile mode the parsed reg_ptr / offset / direction).
`spi_target_regs(off, length, write, table, name)` reads or writes either
backing table from the pod side - the regfile parallel to `i2c_target_regs`, so
a test can seed the read table or inspect what the controller wrote.

### Bench wiring

Pod **GP16 = MISO**, **GP17 = CS**, **GP18 = SCK**, **GP19 = MOSI**. The pod is
the peripheral, so MISO is a pod output and MOSI/SCK/CS are pod inputs. For the
validated nRF52840 bench: pod GP19/MOSI <- nRF P0.20, GP18/SCK <- P0.17,
GP17/CS <- P0.15, GP16/MISO -> P0.13. CS is active-low and its input is pulled
up, so an undriven CS reads deasserted (a floating CS would otherwise clock the
SM on line noise).

Validated byte-exact against an nRF52840 driven as the SPI controller: stream
and regfile personalities, regfile read and write through the real two-CS
protocol, all four SPI modes, at SPI clocks from 125 kHz to 8 MHz. 8 MHz is the
test controller's own ceiling (nRF SPIM0), not the pod's - the pod SM held every
rate in range with no bit-slip, so its limit is higher and not yet characterised.

## GPIO / ADC

`gpio(pin, value=None, mode, pull)` reads a pin (value omitted) or drives it
(value set). `adc(pin)` samples a channel and returns the raw 16-bit reading
plus a 3.3V-reference voltage. Both are stateless one-liners; they exist for
ergonomics, not because they need lifecycle management.

## Coexistence

The I2C target uses a hardware I2C instance and its two pins; it doesn't touch
PIO, so it coexists with the SWD debug stack (PIO1) and Wi-Fi (PIO2 on the Pico
2 W) without arbitration. The SPI target and the logic analyser are both PIO0
consumers (the free block) and are mutually exclusive: each claims PIO0 through
the runtime PIO arbiter, so a live one blocks the other (PioConflict) rather
than silently sharing the block. SWD (PIO1) and Wi-Fi (PIO2) are tracked
alongside them in the arbiter.

## Deployment

`peripherals.py` ships in the resident package (`/lib/annealage_pod/`) alongside
the rest. After editing it, re-copy it and `sys.modules.pop` the module under
`mpremote resume` (the module-cache caveat in `dev-notes.md`) so the pod picks
up the change.
