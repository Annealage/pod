# RP2350 pod: bring-up spike findings

Hardware-validated results from the RP2350 pivot bring-up spike (2026-05-30).
Companion to `dev-notes.md` (gotchas/recipes). The phased plan builds on these.

## Context

The Annealage Pod's canonical target is the RP2350 (Pico 2 W); the ESP32-S3
design (`docs/esp32-s3/spec.md`, `docs/esp32-s3/architecture.md`) is the prior,
superseded design. The RP2350 inverts one core decision: instead of synthesising
a CMSIS-DAP-v2 probe over USB/IP for a host-side pyOCD to drive, the debugger
runs **on the pod** in MicroPython, driving SWD directly. The host talks to the
pod over Wi-Fi.

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
service. This extends the ESP32-S3 spec's mDNS record (`docs/esp32-s3/spec.md` §5.2)
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

## 6. Phase 1/2 results on hardware (nRF52840 target, no USB DUT)

Productionised under `src/mpy/annealage_pod/debug/` (`swd_pio`, `swd_dap`,
`flash_nrf52`), driven from the host over the pod's USB-CDC REPL for development
and over the Wi-Fi socket REPL for the network proof.

- **DP/AP/MEM-AP + Cortex-M (D2.1)**: validated on the nRF52840, DPIDR
  `0x2BA01477`, AP IDR `0x24770011`, CPUID `0x410FC241`, FICR.PART `0x52840`,
  1 MB / 4 KB pages / 256 pages. Halt/resume toggles `S_HALT`; 32-bit block reads
  with TAR auto-increment (re-armed at the 1 KB boundary) match single reads.
- **nRF52 NVMC flash (D2.2/D2.4 step 1)**: erase / program / read-back-verify
  works, including a payload crossing the 1 KB TAR boundary, driven **over Wi-Fi**
  (the Phase 2 over-the-network milestone for this hardware). ~425 words/s
  (1.66 KB/s) at 9.375 MHz after block-write tuning. Full Phase 2 gate's
  RP-native and CMSIS-FLM targets stay hardware-blocked (no RP / STM32 DUT wired);
  full-image streaming-from-file landed (host streams into pod RAM over TCP, no
  pod filesystem).
- **Generic CMSIS-FLM loader (D2.2)**: runs a standard CMSIS flash algorithm on
  the target (load blob to SRAM, call Init/EraseSector/ProgramPage via core
  registers + MEM-AP, resume with interrupts masked, BKPT-return). Validated on
  the nRF52840 with the CMSIS-pack flash algorithm (FLM erase+program+verify, ~570 ms
  / 1 KB), 3/3 deterministic. Generalises flashing to any CMSIS-pack target;
  `tools/flm_extract.py` produces the on-VFS algo data. RP-native (bootrom) flash
  remains, blocked on a wired RP DUT.
- **GDB debugging (Phase 3)**: hybrid GDB server, a stateless on-pod binary
  debug-command server (`dbgsrv`, port 3335) + host GDB RSP translator
  (`pod.gdbserver`), with FPB hardware breakpoints in `CortexM` and a framed,
  interruptible `RESUME_WAIT` for Ctrl-C. Validated with real `arm-none-eabi-gdb`
  through the pod to an nRF52840: reset-halt, read registers/memory, an FPB
  hardware breakpoint that hit, backtrace, single-step, continue + re-hit (global
  observed incrementing), clean detach. `pod gdb <label>` and a `gdb` MCP tool.
- **PIO SWD clock (D1.1 partial)**: the spike ran `clkdiv=8` = 9.375 MHz write
  and read 100/100 DPIDR clean, but that clock is over the nRF52840's 8 MHz
  SWDCLK max - the clean run was rig margin, not spec, and it later showed
  intermittent ACK=3 / parity errors on a cold DUT. The default is now
  `clkdiv=16` (spec-compliant; `swd_pio.DEFAULT_CLKDIV`). Hard cliff at 12.5 MHz
  (input-sampling phase). A clean >= 10 MHz write clock needs PIO input-phase
  tuning. RP-target + multidrop (TARGETSEL) remain blocked (no RP DUT wired).
- **PIO/core coexistence (D1.2 partial)**: CYW43 Wi-Fi + PIO SWD (PIO1 SM4)
  running together, 300/300 MEM-AP read-pairs clean while commands flow over
  Wi-Fi; Wi-Fi stays connected and mDNS keeps answering throughout. The
  USB-host leg of the three-way test is deferred until a USB DUT is available.
  Note: CYW43 actually runs on **PIO2 SM0** on this Pico 2 W (confirmed
  2026-06-04 by reading `PIO->CTRL`), not PIO0 as earlier notes assumed - it
  claims a free SM reaching its WL pins. SWD (PIO1) never collided with it
  regardless. The logic analyser, originally placed on PIO2, hard-wedged the
  chip until moved to PIO0; see `logic-analyser.md`.
