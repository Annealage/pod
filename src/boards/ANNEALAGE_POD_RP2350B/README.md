# Annealage Pod RP2350B board

MicroPython board variant for a pod built on a **Waveshare RP2350B-Plus-W**.

The pod role is identical to `ANNEALAGE_POD_RP2350` (Pico 2 W): CYW43 Wi-Fi +
Bluetooth, the native USB controller usable as a host for the DUT
(`machine.USBHost`, host-on-demand), a Wi-Fi socket REPL, and a backup UART REPL.
Everything in `../ANNEALAGE_POD_RP2350/README.md` about USB reachability,
`config.py`, the Wi-Fi REPL and the UART REPL applies here unchanged and is not
repeated. This file documents only what differs, which is all board hardware.

## What differs from the Pico 2 W pod

| | ANNEALAGE_POD_RP2350 | ANNEALAGE_POD_RP2350B |
|---|---|---|
| MCU | RP2350A, QFN-60, 30 GPIO | RP2350B, QFN-80, 48 GPIO |
| Flash | 4 MB | 16 MB (W25Q128), 14.5 MB filesystem |
| PSRAM | none | 8-pin QSPI footprint on XIP CS1 = **GPIO47**, shipped unpopulated |
| Radio | CYW43439, GPIO23-29 | Raspberry Pi RM2 (CYW43439), **GPIO36-39** |
| LEDs | one, on WL_GPIO0 | two: `LED` = WL_GPIO0, `LED2` = GPIO23 |
| VSYS sense | GP29 | GPIO46 (ADC6), divider ratio 3 |
| VBUS sense | WL_GPIO2 | WL_GPIO2 |
| ADC pins | GP26-GP28 | GPIO40-GPIO45 (ADC0-ADC5); GP26-28 are **not** ADC-capable here |
| Programming | SWD via wired pico-probe | USB BOOTSEL (no probe wired) |

**The DUT-facing pin map is unchanged.** `annealage_pod._rp2_pinmap` (SWD
GP14/GP15, nRST GP13, I2C1 GP10/GP11, UART1 GP4/GP5, SPI GP16-GP19, LA block
GP16-GP21, backup REPL GP0/GP1) is identical on both pods and every one of those
pins is on this board's 40-pin header, so a DUT harness moves between the two
pods without rewiring.

## Board definition

The upstream pico-sdk has no header for this board, so `waveshare_rp2350b_plus_w.h`
is carried here and put on the SDK's board-header search path by
`mpconfigboard.cmake`. It is written from the vendor schematic
(`files.waveshare.com/wiki/RP2350B-Plus-W/RP2350B-Plus-W.pdf`), not from
Waveshare's demo header, which sets `PICO_VSYS_PIN` to 43; the schematic routes
VSYS_SENSE to GPIO46 and leaves GPIO43 as a plain bottom pad.

GPIOs deliberately absent from `pins.csv` because they are consumed on-board and
unsafe to drive: **36, 37, 38, 39** (RM2 radio bus), **46** (VSYS sense), **47**
(PSRAM chip select). The remaining 41 are broken out, plus `WL_GPIO0..2`.

## PSRAM

`MICROPY_HW_ENABLE_PSRAM` is on with the chip select at GPIO47. This is a probe,
not an assertion: `psram_init()` reads the device ID and returns 0 when the
footprint is empty, and the rp2 port then falls back to the SRAM-only GC heap.
So one firmware image serves a populated and an unpopulated board.

With a chip fitted the heap is *split* (`MICROPY_GC_SPLIT_HEAP`) rather than
replaced - SRAM stays the first allocation arena and the PSRAM window at
`0x11000000` is added to it, so short-lived objects still land in fast SRAM.
Confirm what a given board actually has with `gc.mem_free()` shortly after boot:
roughly 190 KB means no PSRAM, roughly 8.2 MB means an 8 MB (64 Mbit) part.

PSRAM shares the QSPI bus with the flash, so `rp2_flash.c` re-runs `psram_init()`
after every flash write. Code and data in PSRAM are unavailable while a flash
erase or program is in flight; this is handled by the port, but it is the reason
the pod's USB/IP and lwIP buffers stay in static C storage rather than the heap.

## PIO allocation

Unchanged from the Pico 2 W pod, and verified on hardware here: PIO0 free (logic
analyser / SPI target), PIO1 SWD, **PIO2 CYW43 Wi-Fi - off-limits**
(`annealage_pod.debug.pio_arbiter.PIO_MAP` is authoritative).

That this still holds is not an accident of the pin move. The radio sits above
GPIO31, so its PIO needs `GPIOBASE = 16`, and the SDK's
`pio_claim_free_sm_and_add_program_for_gpio_range()` scans PIO instances in
*descending* order and takes the first block whose state machines are all free
so it can set the base. At boot that is PIO2. PIO0 and PIO1 are left at
`GPIOBASE = 0`, which is what SWD on GP14/GP15 and the logic analyser on
GP16-GP21 need - a PIO with base 16 could not reach GP14 at all.

Measured on hardware with Wi-Fi up and the pod's SWD transport built:

```
PIO0 sm_enabled=0x0 gpiobase=0
PIO1 sm_enabled=0x1 gpiobase=0     <- SWD (GP14/GP15)
PIO2 sm_enabled=0x1 gpiobase=16    <- CYW43 (GPIO36-39)
```

## Build and flash

```
make BOARD=ANNEALAGE_POD_RP2350B              # build
make BOARD=ANNEALAGE_POD_RP2350B flash-usb    # flash, board held in BOOTSEL
```

Output: `src/micropython/ports/rp2/build-ANNEALAGE_POD_RP2350B/firmware.{uf2,elf}`.

No debug probe is wired to this board, so `flash-usb` drives `picotool` over USB
with the board in BOOTSEL. It selects the target by chip serial
(`POD_USB_SERIAL` in the Makefile) so that a second RP-series board sitting in
BOOTSEL cannot be flashed by mistake; read the serial off the intended board with
`picotool info -a` (the `chipid`, uppercased, without the `0x`).

A BOOTSEL UF2 load rewrites the program region and leaves the filesystem - and
so `config.py` and `/lib/annealage_pod` - intact. A full chip erase does not.

## Pod identity

`netboot.pod_name()` derives the hostname and mDNS instance name from
`machine.unique_id()`, e.g. `annealage-pod-8b97a`, so several pods coexist on one
LAN from one firmware image with no per-unit build. Hosts find pods by browsing
the `_annealage-pod._tcp` service type, not by a fixed name, so discovery is
unaffected.
