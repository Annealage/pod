# RP2350 pod: bring-up spike findings

Hardware-validated results from the RP2350 pivot bring-up spike (2026-05-30).
Companion to `dev-notes.md` (gotchas/recipes). The phased plan builds on these.

## Context

The Annealage Pod is adding an RP2350 (Pico 2 W) target as a parallel variant
alongside the ESP32-S3 design (`docs/spec.md`, `docs/architecture.md`). The pivot
inverts one core decision: instead of synthesising a CMSIS-DAP-v2 probe over
USB/IP for a host-side pyOCD to drive, the debugger runs **on the pod** in
MicroPython, driving SWD directly. The host talks to the pod over Wi-Fi.

Spike hardware:

- Pod under development: `pico2-w` (RP2350), reached over its USB CDC and, after
  the transport spike, over Wi-Fi.
- SWD target (DUT): nRF52840 dongle, wired pod `GP14`=SWDIO, `GP15`=SWCLK, GND.
- Pod reflash path: wired `pico-probe` (CMSIS-DAP) on the pod's SWD debug header.

## 1. On-pod SWD works in pure MicroPython

A pure-Python bit-bang SWD implementation
(`prototypes/rp2350-swd-spike/swd_bitbang.py`) drove the full ADIv5 stack
reliably (15/15 trials):

| Step | Result | Proves |
|---|---|---|
| DPIDR read | `0x2BA01477` | SWD line protocol + JTAG-to-SWD switch |
| DP power-up | `CTRL/STAT = 0xF0000000` | DP read + write, power handshake |
| AP IDR | `0x24770011` | AHB-AP present and selectable |
| MEM-AP read `0xE000ED00` | `0x410FC241` | arbitrary target memory read (Cortex-M4 r0p1 CPUID) |

The MEM-AP memory read is the load-bearing result: it is the primitive the CMSIS
FLM flash loader and the pod's own ADIv5 DP/AP/MEM-AP layer are built on. Reading the
nRF52840's CPUID through MEM-AP confirms the pod can reach arbitrary target memory
over its own SWD link.

Bit-bang is the protocol proof, not the production path; the PIO SWD port (speed)
is the next hardware spike.

## 2. Wi-Fi management transport works

The pod was reflashed to stock `RPI_PICO2_W` (v1.29.0-preview), and the
management transport was proven end-to-end:

- CYW43 Wi-Fi station up.
- MicroPython REPL exposed on a TCP socket via `os.dupterm`, served from a
  background thread (`prototypes/rp2350-swd-spike/netrepl.py`).
- Driven from the host with `ampremote connect socket://<ip>:8266` (andrewleech's
  mpremote build carrying the `socket://` transport, PR #19062): both `exec` and
  `mount`-over-socket confirmed. The pod read host files from `/remote` over
  Wi-Fi.

`mount` over the socket transport is what lets the pod consume CMSIS-pack target
data, flash-algorithm blobs, and DUT firmware images served live from the host,
without writing them to the pod's flash filesystem first.

## 3. Architecture points settled by the spike

### USB: native controller in host mode

The RP2350's native USB controller is used in **host** mode for the DUT. Native
host is more reliable than Pico-PIO-USB (which bit-bangs USB on PIO and is
timing-fragile), and it consumes no PIO. Because the single native USB port is
dedicated to the DUT host role, the pod has no native USB device for its own
management, which is exactly why pod REPL and flashing ride Wi-Fi (socket REPL),
not USB.

Consequence for the PIO/core budget: with native USB host (0 PIO) and CYW43
Wi-Fi (PIO-SPI, ~1 state machine), the bulk of the PIO blocks remain free for the
PIO SWD, SWO capture, and I2C/SPI-target engines. This is a more favourable budget
than an earlier assumption of Pico-PIO-USB host. It still needs a coexistence
spike, but the headroom is larger.

### Debug probe runs on the pod

The probe is a pure-MicroPython debug stack: PIO SWD line layer, DP/AP/MEM-AP
register access, a CMSIS FLM-blob flash loader, and a ported GDB server. Target
descriptions and flash-algorithm blobs are CMSIS-pack data stored on the pod VFS
or served over `mount`; host-side tooling handles pack search/download/extraction.
Reference for the on-device PIO-SWD + GDB-server + flashing shape:
`github.com/essele/pico_debug` (C, RP2040-specific).

### mDNS must be browsable service discovery

Because every host interaction with the pod is over the network, discovery has to
be robust. A bare hostname is not enough: advertise a browsable mDNS **service**
(e.g. `_annealage-pod._tcp`) carrying the relevant ports and identity in TXT
records (REPL/socket port, USB/IP port, UART-forward port, carrier-id,
firmware-version), so host tooling can enumerate pods by service type and connect
without a pre-known name. The `ampremote`-based host wrapper should browse this
service. This extends the ESP32-S3 spec's mDNS record (`docs/spec.md` §5.2)
toward discovery-first rather than name-first.

## 4. Remaining hardware risks (next spikes)

1. **PIO SWD port**: re-validate DPIDR / MEM-AP at speed via `rp2.asm_pio`,
   porting the `pico_debug` dispatch model. Retires the throughput risk.
2. **PIO/core coexistence**: native USB host + CYW43 + PIO SWD/SWO/I2C-SPI-target
   running together within 3 PIO blocks and 2 cores. Eased by native USB host but
   not yet measured.

## 5. Spike artifacts

- `prototypes/rp2350-swd-spike/swd_bitbang.py`: bit-bang SWD (DP/AP/MEM-AP), the
  validated turnaround framing (see `dev-notes.md` §3).
- `prototypes/rp2350-swd-spike/netrepl.py`: Wi-Fi + `os.dupterm` TCP REPL server.
