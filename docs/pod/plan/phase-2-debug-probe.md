# Phase 2: Debug-probe stack

Workstream D. The core differentiator: turn the proven PIO SWD link into a target
flasher driven by reused CMSIS pack data.

Goal: flash and verify a real DUT firmware image over the network, end to end,
using an on-pod CMSIS FLM flash loader plus reused target descriptions.

## Dependencies

- D1 (PIO SWD at speed), F1 (transport + discovery).

## Tasks

### D2.1 DP/AP/MEM-AP layer (ADIv5, from the ARM spec)
- Implement the ADIv5 debug-port / access-port / MEM-AP logic in MicroPython on top of
  the PIO SWD primitive: DPIDR, CTRL/STAT power handshake, SELECT banking, MEM-AP
  CSW/TAR/DRW with auto-increment, 8/16/32-bit access, block read/write, RDBUFF
  posted-read handling, sticky-error/ABORT recovery, WAIT retry.
- Keep the layering conventional (ADIv5 DP/AP/MEM-AP) so behaviour is familiar and portable.

### D2.2 CMSIS FLM flash loader
- Implement the CMSIS flash-algorithm contract: load the position-independent
  Thumb blob into target SRAM, set SP/R9(static_base)/PC + args, run, breakpoint
  on return, read R0 status. Implement Init / UnInit / EraseSector /
  ProgramPage / (EraseChip).
- Drive it through the MEM-AP layer: halt core, set up the algo, stage page
  buffers, loop sectors/pages, verify.
- RP-native fast path: for RP2040/RP2350 targets, flash via bootrom routines (the
  `pico_debug` approach) instead of a generic FLM blob, for speed. Keep FLM as the
  general path.
- Per-family native-NVM path (e.g. nRF52 NVMC, MCU_Flasher-style): drive the
  chip's own flash controller directly where that is simpler than an FLM blob.
  Used for the first flash bring-up (nRF52840) and as the R4 fallback for targets
  the generic loader cannot drive.

### D2.3 Target data on the pod VFS
- Define a compact on-VFS format for what the loader needs per target: memory map
  (flash/RAM regions), the FLM blob, page/sector geometry, RAM load address.
- Host-side extraction tool (part of the `pod` toolchain, Phase 6 seed): turn a
  CMSIS pack `FLASH_ALGO` into the on-VFS format. On-device pack search and
  download are out of scope; the host produces the data and serves it via the VFS
  or `ampremote mount`.

### D2.4 End-to-end flash
- Order of bring-up: (1) the already-wired nRF52840 via its native NVMC (simplest
  flash-control proof), (2) a CMSIS-FLM target via the general loader (e.g. STM32),
  (3) an RP2040/RP2350 via the RP-native fast path. Flash a known image and verify
  (read-back compare / CRC) at each step.
- Wire the flash entry into the `annealage_pod` API and the `pod` CLI
  (`flash <image>`).

## Deliverables

- MicroPython modules: `swd_dap` (DP/AP/MEM-AP), `flash_loader` (FLM + RP-native),
  target-data loader.
- Host extraction tool: CMSIS pack algo -> on-VFS target data.
- A flashed-and-verified DUT over the network.

## Exit gate

The pod flashes and verifies a real DUT firmware image over Wi-Fi via the on-pod
loader, for both an RP-native target and a CMSIS-FLM target.

## Risks

- R4 (FLM generalisation): fall back to per-family native-NVM for targets the
  generic loader cannot drive.

## References

- CMSIS FlashAlgo model (FLASH_ALGO dict: instructions, pc_* entry points,
  begin_data/stack, static_base); `github.com/pyocd/FlashAlgo`
- `github.com/essele/pico_debug` (`flash.c`, bootrom-routine flashing)
- Adafruit_CircuitPython_MCU_Flasher (per-family native-NVM precedent)
