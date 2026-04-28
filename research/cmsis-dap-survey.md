# CMSIS-DAP / SWD / SWO survey for ESP32-S3 POD probe

Scope: ESP32-S3 firmware that exposes a synthetic CMSIS-DAP-v2 device over USB/IP to a host PC. Goals are SWD throughput, SWO capture quality, and tooling compatibility.

## Goal 1: open-source CMSIS-DAP firmware candidates

### ARM-software/CMSIS-DAP (reference)
- License: Apache-2.0. Spin-off from CMSIS_5, repo at <https://github.com/ARM-software/CMSIS-DAP>. ([CMSIS-DAP repo](https://github.com/ARM-software/CMSIS-DAP), [CMSIS-DAP overview](https://arm-software.github.io/CMSIS-DAP/latest/))
- I/O: vendor-supplied port files; default reference is GPIO bit-bang via `PIN_SWCLK_TCK_SET` / `PIN_SWDIO_OUT` macros in `DAP_config.h`. SPI-assisted ports exist downstream but not in the canonical tree.
- USB: HID v1 and Bulk v2 both supported; default packet size 512 (bulk) / 64 (HID), packet count 4.
- SWO: full UART + Manchester support in `SWO.c`, buffer is `static uint8_t TraceBuf[SWO_BUFFER_SIZE]`, must be 2^n; reference config sets 4096. Overflow pauses capture and sets `DAP_SWO_BUFFER_OVERRUN`. ([SWO.c](https://github.com/ARMmbed/DAPLink/blob/main/source/daplink/cmsis-dap/SWO.c))
- Recommendation: this is the cleanest base license-wise; shares its `DAP.c` / `DAP_vendor.c` / `SWO.c` with DAPLink and most forks. **Use this as the protocol layer.**

### ARMmbed/DAPLink
- License: Apache-2.0. ([DAPLink](https://github.com/ARMmbed/DAPLink))
- Adds mass-storage drag-drop, CDC UART, board-specific HIC HALs (sam3u2c, lpc4322, stm32f103, nrf52, kl26z, max32625…). The CMSIS-DAP core (`source/daplink/cmsis-dap/`) is essentially the ARM reference with v2 bulk endpoints wired up.
- I/O: GPIO bit-bang per HIC, no SPI accel in upstream.
- SWO: 4096 byte buffer default in `sam3u2c/DAP_config.h`; ring buffer with overflow flag. Known historical bugs: <https://github.com/probe-rs/probe-rs/issues/448> (reads larger than `wMaxPacketSize` stall on `==wMaxPacketSize` boundary; fixed host-side in probe-rs by issuing reads matching the descriptor's max-packet, but old DAPLink builds still misbehave with hand-written hosts). Also <https://github.com/ARMmbed/DAPLink/issues/179> (UART overflow corrupts stream into "asterisk" output).
- Last meaningful commit: still maintained but cadence has slowed since Mbed sunset. Treat as a frozen reference, not a moving target.
- Recommendation: **excellent reference for endpoint setup, descriptors, and SWO state machine; the CMSIS-DAP source under `source/daplink/cmsis-dap/` lifts cleanly.**

### Alex Taradov / free-dap
- License: BSD-3-Clause. ([free-dap](https://github.com/ataradov/free-dap))
- Targets: SAM D11, SAM D21, RP2040 (the RP2040 port was added when v2 support landed). 70 commits, small, hand-rolled, no RTOS, no Mbed cruft.
- I/O: bit-bang on SAMD; on RP2040 the official tree is still bit-bang (PIO acceleration is downstream forks).
- USB: HID v1 + Bulk v2.
- SWO: `dap.h` declares the SWO command IDs but free-dap does not implement SWO capture in upstream (the SAMD ports lack it).
- Recommendation: **cleanest minimal CMSIS-DAP implementation; very readable; great study reference but doesn't bring SWO.**

### raspberrypi/debugprobe (was picoprobe)
- License: BSD-3-Clause.
- I/O: RP2040 PIO state machines for SWD and UART. Native PIO bit-bang at >25 MHz on a USB-FS link.
- USB: CMSIS-DAP v2 (PR #31 by P33M, <https://github.com/raspberrypi/debugprobe/pull/31>) plus a CDC UART. v2.3.0 release Feb 2026.
- SWO: not implemented (the PIO is busy with SWD/UART; SWO would need a third SM).
- Recommendation: **best in class on RP2040, but the I/O backend is PIO-specific and does not port to ESP32-S3.**

### windowsair/wireless-esp8266-dap (and forks)
- License: MIT. Last release v0.4.0 May 2025; 176 commits, active.
- Targets: ESP8266, ESP32, ESP32-C3, **ESP32-S3**.
- Transport: USB/IP over TCP, exactly the architecture you want. Plus optional WCID/WinUSB and HID modes.
- I/O: hybrid: pure GPIO at <2 MHz, hardware SPI peripheral at ≥10 MHz, claimed up to 40 MHz SCLK. (`README_CN.md` describes the three-tier strategy.)
- pyOCD/OpenOCD: declared supported. probe-rs: not explicitly listed, but compliance with v2 bulk should make it work; no specific regression reports surfaced.
- SWO: **not implemented**. The "Debug Trace (UART)" feature is a TCP-bridged CDC UART, not SWO.
- Recommendation: **the closest existing prior art for what you want. Fork this for the ESP32-S3 USB/IP frame and SPI-assisted SWD; replace its CMSIS-DAP core with the ARM reference and add SWO.**

### bugadani/espdap (Rust)
- License: not declared in Cargo.toml visible content; flag this. Built on `dap-rs` and `bitbang-dap` crates (also unlicensed-by-default risk - confirm before reuse).
- Targets: ESP32-S2, ESP32-S3 (default S3). Uses esp-hal beta + Embassy USB.
- I/O: pure bit-bang via `bitbang-dap`. Marked WIP, 23 commits, no releases.
- SWO: none.
- Recommendation: **interesting Rust path but immature, license unclear, bit-bang only. Skip unless you commit to a Rust firmware stack.**

### devanlai/dap42
- License: ISC for project portions; libopencm3 portions are LGPL. (LGPL is dynamic-link compatible but requires shipped object files / relinking ability; flag if you ship binaries.)
- Targets: STM32F042 / F103.
- I/O: bit-bang.
- USB: v1 HID and v2 Bulk (mutually exclusive on F103 due to USB EP count).
- SWO: not advertised.
- Recommendation: **license is the worst of the survey for combining; do not lift code.**

### Notable also-rans
- ESP-PROG and Espressif's `openocd-on-esp32` (<https://github.com/espressif/openocd-on-esp32>) port a full OpenOCD onto an ESP32-S3, but use the `esp_gpio` bit-bang driver capped at ~1 MHz default. Not a CMSIS-DAP probe.
- `kerms/wireless-esp32-tools`, `Haku060/wireless-esp32-dap`, `iexplore123/wireless-esp32s3-dap` are all stale forks of windowsair with no meaningful divergence.
- `embedded-cli/mbed` ecosystem CMSIS-DAPs are all DAPLink HIC ports; nothing new beyond ARMmbed/DAPLink.

### License combinability summary
Apache-2.0 (CMSIS-DAP, DAPLink) + MIT (windowsair) + BSD (free-dap, debugprobe) all combine cleanly with each other and with MicroPython's MIT. **Avoid dap42 (LGPL via libopencm3) and confirm espdap/dap-rs licensing before reuse.**

---

## Goal 2: ESP32-S3 SWD I/O acceleration

### Bit-bang ceiling
Direct register writes via `GPIO.out_w1ts` / `out_w1tc` peak around 8 MHz toggle on ESP32-S3 ([forum thread](https://esp32.com/viewtopic.php?t=27963)). Dedicated GPIO bundles with assembly `EE.WUR` instructions reach 12 ns per pin change ≈ 80 MHz square wave ([dedic_gpio docs](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/dedic_gpio.html)). For a real SWD half-bit (set clk, set/sample data, conditional turnaround, parity), the achievable SWCLK is much lower: figure 5–10 MHz with C-only dedicated GPIO, maybe 15–20 MHz with hand-tuned assembly. Both cores are blocked while bit-banging.

### SPI peripheral as SWD engine (recommended)
ESP32-S3 SPI2/SPI3 have half-duplex 3-wire mode where MOSI (`spid`) is driven and tri-stated for read phases ([SPI master driver](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/spi_master.html)). This is exactly the SWDIO bidirectional pattern. SPI clock supports up to 80 MHz; for SWD the practical ceiling is ~40 MHz because (a) the target chip's debug clock max is typically 10–25 MHz, (b) GPIO matrix routing adds 1 cycle, and (c) the windowsair project empirically settles at 40 MHz.

The hard part is the turnaround bit and the variable framing. Recipe used by windowsair (see `components/DAP/source/SWD_host.c` family):
- SCLK on SPI sclk, SWDIO on MOSI with the line tied to MISO via either internal loopback routing (`io_mux` set so MOSI pin's input feeds MISO peripherally) or a series resistor.
- Send the 8-bit header as one SPI transaction; read ACK as a 3-bit transaction with MOSI tri-stated; data and parity as further transactions, with explicit OEN toggling around the Trn cycle.
- GDMA on SPI2/SPI3 lets you queue many transactions back-to-back in an interrupt-free path. For `DAP_TransferBlock` (the bulk path used by flash programmers) this is a giant win because each 32-bit AP write is one descriptor.

Realistic effective SWD clock: 10–25 MHz reliably, 40 MHz with short, well-terminated wiring and a target that tolerates it. This dwarfs anything achievable by bit-bang on this CPU.

### I2S parallel / LCD_CAM
Two issues: (a) ESP32-S3 split the parallel-bus role into the LCD_CAM peripheral; I2S on S3 is audio-only. (b) LCD_CAM is one-direction-at-a-time (LCD = TX, CAM = RX) and is overkill for two wires. Skip.

### RMT
RMT is symbol-time-driven, not clock-driven, and tops out around 80 MHz tick rate but with 32-bit symbol items and no built-in tri-state semantics. It's the right tool for WS2812 / IR, the wrong tool for SWD. Skip.

### ULP RISC-V
Effective ~3-5 MIPS at 17.5 MHz; a GPIO toggle takes 3 instructions × 8 cycles, yielding a few-kHz square wave ([IDFGH-9446](https://github.com/espressif/esp-idf/issues/10813)). Slower than a CPU bit-bang. Useless for SWD. Skip.

### Prior art that picked a winner
- windowsair/wireless-esp8266-dap: SPI-accelerated, 40 MHz claimed. **The proven approach on ESP32-class chips.**
- espdap (Rust): bit-bang, no published numbers. Author has not posted SPI port plans.
- Espressif's openocd-on-esp32: bit-bang, 1 MHz default. Slow.

The community has converged on SPI-as-SWD wherever the silicon allows it, and the ESP32-S3 silicon allows it cleanly.

---

## Goal 3: SWO capture pipeline

### What "SWO" actually demands
- Cortex-M0+/M0 typically lacks ITM entirely (no SWO).
- Cortex-M3/M4: ITM via SWO, async UART NRZ encoding, baud chosen by host. Typical: 2 MHz from OpenOCD defaults on STM32F1; J-Link auto-tunes up to 6 Mbps; FTDI-based probes go to ~12 Mbps. ([adastra-soft](https://adastra-soft.com/poor-man-arm-cortex-m-swo/), [japaric.io](https://blog.japaric.io/itm/))
- Cortex-M33/M55: same SWO, often clocked higher (10+ MHz NRZ) on faster targets.
- Manchester is in the spec but essentially no one uses it; UART-only support is acceptable.

Design target: comfortably sustain 6 Mbps UART RX with deep buffering.

### ESP32-S3 UART RX options
1. **Plain UART driver, internal RAM ring buffer.** ESP-IDF default. Interrupt-driven. Fine to ~2 Mbps, marginal at 4 Mbps, usually fails above 4 Mbps when Wi-Fi takes priority interrupts.
2. **UHCI (UART DMA).** ESP-IDF documents this as the high-rate path: <https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/uhci.html>. Caveats:
   - **All three HP UARTs share one UHCI DMA group** - only one UART can be DMA'd at a time.
   - **UHCI buffers must be in DRAM, not PSRAM** (`CONFIG_UHCI_ISR_CACHE_SAFE` keeps the driver object out of PSRAM; the docs are explicit that UHCI cannot target PSRAM directly).
   - UHCI shares HCI hardware with the Bluetooth controller. If you ever enable BT classic / BLE HCI, UHCI is unusable simultaneously. For a Wi-Fi-only POD, fine.
3. **PSRAM ring buffer behind a DRAM staging buffer.** Because UHCI cannot DMA into PSRAM, the design has to be two-tier: a small DRAM circular buffer (e.g. 16-64 KB) is the UHCI target, and a CPU-side task drains it into a much larger PSRAM ring (1-8 MB) via memcpy. PSRAM octal at 80 MHz DDR is ~84 MB/s sustained when contended with cache fills ([forum data](https://www.esp32.com/viewtopic.php?t=33312)), more than sufficient for 6 Mbps SWO.

### PSRAM contention
LCD_CAM + GDMA writes to PSRAM are documented to stall under cache pressure. For SWO at 6 Mbps = 750 KB/s, the bandwidth headroom is ~100×, so even with Wi-Fi TX bursting and cache thrashing, the second-tier copy will keep up. The risk is latency spikes, not bandwidth; size the DRAM tier-1 buffer to absorb the worst expected stall (e.g. 50 ms × 750 KB/s = ~38 KB).

### How DAPLink and free-dap frame SWO back to host
- DAPLink: 4096 byte trace buffer, `USB_BLOCK_SIZE = 512`, `TRACE_BLOCK_SIZE = 64`. Streams over a dedicated bulk-IN endpoint (EP3 in v2). On overflow, sets `DAP_SWO_CAPTURE_ACTIVE | DAP_SWO_CAPTURE_PAUSED` and reports `DAP_SWO_BUFFER_OVERRUN` on next status query - it does not silently drop.
- free-dap: SWO not implemented in upstream.
- windowsair: SWO not implemented.

CMSIS-DAP-v2 spec: SWO uses the third bulk-IN endpoint; max packet 512 (HS) or 64 (FS). Probe-rs `wMaxPacketSize` regression (issue #448) is a known historical pothole - if your probe advertises EP3 with 512 byte max-packet, never write USB packets that are exactly 512 bytes without a follow-up zero-length packet, otherwise some hosts hang waiting.

---

## Recommendations

### (a) CMSIS-DAP base codebase
**Lift the protocol core (`DAP.c`, `DAP_vendor.c`, `JTAG_DP.c`, `SWO.c`) from ARM-software/CMSIS-DAP**, which is the same code DAPLink ships. Take the **ESP32-S3 USB/IP transport, board scaffolding and SPI-assisted SWD primitives from windowsair/wireless-esp8266-dap**. Both are license-clean to combine (Apache-2.0 + MIT) into a MicroPython port. Do not start from espdap (license unclear, immature) or dap42 (LGPL via libopencm3).

Trade-off honestly: if you're willing to write Rust, espdap + dap-rs is a more modern stack and Embassy gives nicer USB plumbing, but you lose the proven SPI-SWD path and have to confirm crate licenses.

### (b) SWD backend on ESP32-S3
**SPI peripheral (SPI2 or SPI3) in half-duplex 3-wire mode, with GDMA on the bulk transfer path.** Same scheme as windowsair. Expect 25 MHz reliably, 40 MHz on short wiring. Use dedicated-GPIO + assembly bit-bang as the slow-clock fallback (`<2 MHz`) for line-reset / target-recovery sequences where SPI framing overhead dominates. Reserve the second SPI controller for a future JTAG / multi-target slot.

Trade-off: pure dedicated-GPIO assembly is simpler and gets you to ~10 MHz with one core pinned. If you can live with 10 MHz and want a much smaller code surface, that's defensible. SPI is the right answer if peak throughput on flash programming is a real requirement.

### (c) SWO buffering scheme
**Two-tier ring: 64 KB DRAM tier-1 fed by UHCI DMA on UART1, drained by a pinned core into an 8 MB PSRAM tier-2 ring; CMSIS-DAP-v2 SWO endpoint reads from the PSRAM tier.** UART0 stays free for the chip's own console; UART2 reserved for future expansion. Overflow policy: match DAPLink (set `DAP_SWO_BUFFER_OVERRUN`, keep streaming, never emit asterisks - the DAPLink #179 behaviour is widely loathed). Size the tier-2 ring so >1 second of 6 Mbps SWO survives a Wi-Fi stall.

Trade-off: if you can guarantee <50 ms stalls (wired Ethernet, dedicated AP), a single 256 KB DRAM ring without PSRAM is simpler and avoids the UHCI-can't-touch-PSRAM gymnastics. PSRAM tier is only worth it if Wi-Fi is the transport and you want multi-second resilience.

## References
- ARM-software/CMSIS-DAP, Apache-2.0: <https://github.com/ARM-software/CMSIS-DAP>
- ARMmbed/DAPLink, Apache-2.0: <https://github.com/ARMmbed/DAPLink>; SWO source <https://github.com/ARMmbed/DAPLink/blob/main/source/daplink/cmsis-dap/SWO.c>
- ataradov/free-dap, BSD-3-Clause: <https://github.com/ataradov/free-dap>
- raspberrypi/debugprobe, BSD-3-Clause; v2 PR <https://github.com/raspberrypi/debugprobe/pull/31>
- windowsair/wireless-esp8266-dap, MIT: <https://github.com/windowsair/wireless-esp8266-dap>
- bugadani/espdap (Rust, license TBC): <https://github.com/bugadani/espdap>
- devanlai/dap42 (ISC + LGPL): <https://github.com/devanlai/dap42>
- ESP32-S3 dedicated GPIO: <https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/dedic_gpio.html>
- ESP32-S3 SPI master half-duplex 3-wire: <https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/spi_master.html>
- ESP32-S3 UHCI UART DMA: <https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/uhci.html>
- ULP RISC-V speed analysis: <https://github.com/espressif/esp-idf/issues/10813>
- 9names CMSIS-DAP probe benchmarks: <https://9names.github.io/embedded/rust/debug/2022/04/07/cmsisdap-probe-performance.html>
- probe-rs SWO oversized-read regression: <https://github.com/probe-rs/probe-rs/issues/448>
- DAPLink UART overflow corruption: <https://github.com/ARMmbed/DAPLink/issues/179>
- Cortex-M ITM / SWO baud notes: <https://blog.japaric.io/itm/>, <https://black-magic.org/usage/swo.html>
