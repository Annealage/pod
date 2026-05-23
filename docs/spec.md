# Annealage Pod (ESP32-S3) Specification

Status: idea-honing draft, rev1 firmware target.

## 1. Goal

A drop-in replacement for the existing Octoprobe Annealage Pod, built on a single ESP32-S3 instead of two RP2040s (RP_INFRA + RP_PROBE). Network transport over Wi-Fi instead of upstream USB. The new PCB mates with existing Octoprobe DUT carriers via the same 2x20 0.1" pitch socket connector. Internal layout, level translation, and firmware are clean-sheet.

Capabilities reachable over Wi-Fi:

- USB host port for the DUT, exported to a remote PC as a normal local USB device via USB/IP.
- A synthetic CMSIS-DAP-v2 debug probe exported on the same USB/IP server, so stock pyOCD / OpenOCD / probe-rs see a normal local probe after `usbip attach`.
- DUT UART forwarded over a separate TCP socket.
- MicroPython REPL on a TCP socket and on a wired console UART.
- Per-rail current and voltage telemetry.
- I2C-slave and SPI-slave personality so DUT-as-master code can be tested against the annealage_pod.
- Standard 7 opto-coupled relays unchanged from the existing carrier, for boot-button presses and arbitrary digital control.
- mDNS-announced on the lab subnet.

Out of scope for rev1: wired Ethernet, programmable VTARGET voltage, USB high-speed, on-annealage_pod MSC interception (DUT MSC is forwarded through USB/IP unchanged).

## 2. Silicon and module

ESP32-S3-WROOM-1-N16R8: 16 MB octal flash, 8 MB octal PSRAM. Octal PSRAM is required for the SWO tier-2 ring (see §4.6). The N16R8 module places PSRAM/flash on internal pins so they do not consume external GPIO budget beyond the few reserved indices.

Two-core layout:

- **PRO_CPU (core 0)** runs Wi-Fi, lwIP, MicroPython VM, mDNS, the UART bridge task, and the supervisor cleanup hook. ESP-IDF pins Wi-Fi and lwIP here by default.
- **APP_CPU (core 1)** runs TinyUSB host, the USB/IP server frontend, the CMSIS-DAP command interpreter, the SWO drain task, and the INA228 sampler. Latency-sensitive paths sit off the Wi-Fi core.

USB/IP URBs cross core boundaries (lwIP RX on PRO_CPU into the USB/IP frontend queue on APP_CPU and back); cost is a few microseconds per packet, well below the SWD and USB-host timing budgets.

## 3. Hardware architecture

### 3.1 Carrier compatibility

Replicate the existing Octoprobe v0.7 carrier connector layout on the new PCB. Specifically: J1501 (24-pin RP_PROBE breakout, repurposed as S3-PROBE breakout exposing the same 14 channel DUT-side signals through new direction-controlled translators + VDUT level-shifter reference), J201 (10-pin S3-INFRA GPIO breakout, plays the existing infra role), J501 (28-pin opto-relay output pairs, unchanged), J202 (USB-A receptacle for DUT, now driven by the S3 USB-OTG host controller instead of upstream pass-through). VTARGET reaches the DUT through the level-shifter B-side reference, not on a dedicated carrier pin (matches v0.7).

This preserves drop-in mechanical compatibility with existing Octoprobe DUT carriers. The cost is a less convenient DUT-side carrier design (4 connectors instead of 1) retained for compatibility.

The S3-side GPIO assignment is in `docs/spec-appendix-A-pinmap.md`. Appendix A §A.2 enumerates the v0.7 signals that map across these connectors and is the source of truth for the new PCB's signal list. Appendix A §A.5's proposed consolidated 40-pin layout is informational only and deprecated by this decision; the same S3 GPIO bindings still apply, but they reach the DUT through the four existing connector footprints rather than one new socket.

### 3.2 Level translation

Replace TXB0108 auto-direction translators with 74LVC1T45-style direction-controlled translators on SWD, UART, SWO, nRST, and the I2C/SPI-slave lines toward the DUT. Direction is statically strapped on always-out and always-in lines (SWCLK, UART TX, UART RX, SWO, MISO, SCK, CS, etc.). Only the genuinely bidirectional lines need a runtime-controlled DIR pin, namely SWDIO (toggled per SWD frame phase) and the I2C-slave SDA.

Rationale: TXB0108's auto-sense burns one SWDIO clock per direction change, capping useful SWD clock to ~2 MHz. With explicit DIR control the SPI-DMA SWD backend can run at 25 MHz comfortably and 40 MHz with short wires.

### 3.3 Power rails

Two rails to the DUT, both ramped, both with current monitoring:

| Rail | Voltage | Switch | Slew rate | Monitoring |
|---|---|---|---|---|
| VTARGET | 3v3 fixed | MT9700 or TPS2595 | 5 V/s ramp | INA228 |
| DUT-USB VBUS | 5 V | MT9700 or TPS2595 | 5 V/s ramp | INA228 + ADC sense |

INA228 chosen over INA219 for the wider dynamic range (20-bit ADC, 1.25 µV LSB shunt), which catches both leakage-current and runaway-stuck-in-bootloader cases in one part. Both INA228s share the local I2C bus on the S3.

VBUS ADC sense lets MP detect when the DUT comes up as a USB device before TinyUSB enumeration arrives.

Programmable VTARGET deferred to rev2.

### 3.4 Reset paths

Four independent paths, all addressable from the MP API:

| Method | Mechanism | Use case |
|---|---|---|
| `swd` | AIRCR.SYSRESETREQ via the synthetic CMSIS-DAP probe | Cleanest, software-only, requires SWD enabled |
| `nrst` | Open-drain nRST line through a direction-controlled translator | Hardware reset, works on bricked targets |
| `power` | VTARGET ramped switch off then on | Last-ditch full power cycle |
| `relay` | One of the 7 opto-coupled relays drives a button input | DUT-specific patterns, e.g. BOOTSEL-and-RESET sequences |

The 7 opto-coupled relays are unchanged from the existing Octoprobe carrier. They cover boot-button holds (BOOTSEL, BOOT0, BOOT1) and arbitrary user digital signals.

### 3.5 Console UART

Onboard USB-UART bridge wired to UART0 (GPIO43 TX, GPIO44 RX), with the standard DTR/RTS-to-EN/IO0 reset circuit so esptool one-shots flashing without buttons. Bridge IC: CH340N (cheap default) or CP2102N (drop-in alternate, slightly more reliable on USB-2 hubs). Plus a 4-pin TX/RX/GND/EN header exposing the same lines for hand-debug.

This is the only flash path while USB-OTG is in host mode, because USB-Serial/JTAG shares D+/D- pins (GPIO19/20) with USB-OTG and is unavailable. It is also the fallback REPL when Wi-Fi is down.

### 3.6 Carrier identification

Existing Octoprobe carriers identify themselves either via an I2C EEPROM or via strapping resistors. The S3 reads the ID on boot via the local I2C bus (shared with the INA228 monitors) and publishes it in the mDNS TXT record and through the MP API.

## 4. Firmware architecture

### 4.1 Build base

- **ESP-IDF**: latest stable v5.x at the time of build (currently the v5.4 series). Pinned via the IDF submodule SHA together with the partition table and `sdkconfig.defaults`.
- **MicroPython**: latest released tag at time of build (currently v1.28.x). Pinned via `src/micropython` submodule SHA.
- **Build flow**: MicroPython esp32 port with `USER_C_MODULES` pointing at this repo's `src/c_modules/`. A custom board variant disables MP's USB-CDC stack (USB-OTG goes to host mode), enables PSRAM in octal mode, and includes the partition layout below.

Versioning policy: re-pin both submodules deliberately on each rev cut; track upstream master between cuts only for development branches.

### 4.2 Partition layout

```
nvs        : data,  size 24K
otadata    : data,  size 8K
phy_init   : data,  size 4K
factory    : app,   size 2M    (provisioned once via UART, recovery image)
ota_0      : app,   size 4M    (active or rollback slot A)
ota_1      : app,   size 4M    (active or rollback slot B)
vfs        : data,  fat, size remaining
```

Bootloader rollback enabled. New OTA images mark themselves valid only after Wi-Fi connects and MP boot completes; on either failure the bootloader rolls back automatically.

### 4.3 OTA

ESP-IDF `esp_https_ota` driven from MP. Update API on the MP side:

```python
import annealage_pod_ota
annealage_pod_ota.update("https://updates.example/annealage_pod-firmware-v1.2.3.bin")
```

Triggered remotely over the TCP REPL socket. Image fetched over HTTPS, written to the inactive OTA slot, atomic switch on next boot, automatic rollback on failure of the post-boot validation hook.

### 4.4 C user modules

Three C user modules, each linking directly against ESP-IDF and lwIP, each owning its own FreeRTOS tasks pinned to APP_CPU:

- **`usbip`**: TCP server on port 3240 (USB/IP standard). Multiplexes two virtual devices on one server: busid 1 for the real DUT (URBs to/from TinyUSB host), busid 2 for the synthetic CMSIS-DAP-v2.
- **`dapprobe`**: synthesises a CMSIS-DAP-v2 USB device on busid 2 in `usbip`. Implements the protocol via combined ARM-software CMSIS-DAP `DAP.c` and `SWO.c` (Apache-2.0). SWD I/O on the SPI2 + GDMA backend (§4.6); SWO capture via UART1 + UHCI (§4.7). License-clean for combination with MicroPython MIT.
- **`uartbridge`**: TCP server on port 2000 (configurable). Forwards bytes between an S3 UART (default UART2 on configurable GPIO) and the socket, bidirectionally, with configurable baud / parity / data bits from MP.

MP-exposed surface (preliminary):

```python
import usbip; usbip.start()                                  # opens TCP/3240
import dapprobe; dapprobe.attach()                            # registers virtual CMSIS-DAP busid into usbip
import uartbridge; uartbridge.start(uart=2, port=2000, baud=115200)
```

Detailed Python API surface: §7.

### 4.5 USB host

TinyUSB host stack on the USB-OTG controller (FullSpeed, 12 Mbps). Enumerates one DUT device. Handles composite devices (CDC + MSC simultaneously, the typical MicroPython-DUT shape). URBs are forwarded to and from the `usbip` virtual device on busid 1 unmodified.

DUT MSC is forwarded through USB/IP unchanged. The host PC mounts `/dev/sdX` via its own MSC stack post-attach. Drag-drop UF2 deployment works through the host PC's filesystem, not the annealage_pod.

#### 4.5.1 Pipeline depth and throughput ceiling

The `usbip` server has a per-EP lane task that submits URBs to the host stack one at a time, gated by a counting semaphore initialised to `USBIP_PIPELINE_DEPTH` (`src/c_modules/usbip/usbip_server.c`). The lane takes one slot before each `tuh_edpt_xfer` submit; the responder gives it back after `RET_SUBMIT` reaches the wire.

The IDF host backend on main (R22 era) ran depth=16 - matching the 16 read URBs cdc-acm typically submits - and the R23 deep-dive measured `avg_depth=13-15` under load, confirming the queue genuinely filled. The overlap between "device pushing the next IN packet" and "us sending RET_SUBMIT for the previous URB over TCP" was load-bearing for the 677 KiB/s ceiling on that path.

On the TinyUSB host backend (R27+) `USBIP_PIPELINE_DEPTH` is forced to 1 by gotcha #2 in `src/c_modules/usbhost/usbhost.c`:

> tuh_edpt_xfer allows only one transfer in flight per (dev,ep); subsequent submits while busy return `false`.

Calling `tuh_edpt_xfer` while an earlier transfer is still in flight on the same (dev,ep) returns false (`ep_status.busy == true`). There is no internal queue. Depth=1 structurally avoids the rejected-submit race - the lane semaphore blocks until `RET_SUBMIT` releases the slot - at the cost of all bus-vs-TCP overlap.

Throughput consequence: the per-EP ceiling on TinyUSB is bounded by `1 / (TinyUSB IN roundtrip + TCP send roundtrip)`, materially below the IDF baseline. Multi-URB pipelining at depth>1 must be achieved by other means: parallel transfers across multiple endpoints (cdc-acm has only one bulk-IN, so doesn't help that case), DWC2 hardware-side scheduling tweaks, or a class-driver-based forwarder that maintains its own pipeline above the `tuh_edpt_xfer` API.

This is a TinyUSB host API design limitation, not a per-device or per-port issue. Any TinyUSB host application that wants high single-EP throughput hits the same ceiling.

### 4.6 SWD backend (CMSIS-DAP)

ESP32-S3 SPI2 in half-duplex 3-wire mode driven by GDMA. Pin map at the translator boundary: SCLK to SWCLK (fixed-direction translator), SPI2 D pin to SWDIO (bidirectional translator with DIR controlled by a separate S3 GPIO toggled per SWD frame phase). Realistic clock: 25 MHz steady, up to 40 MHz with short wiring. Bit-bang via dedicated-GPIO + assembly is the fallback at 10-15 MHz, used as the runtime fallback if a target rejects 25 MHz.

Firmware base for the CMSIS-DAP protocol layer is the ARM-software/CMSIS-DAP reference (`DAP.c`, `SWO.c`, Apache-2.0). The USB/IP transport scaffolding is taken from windowsair/wireless-esp8266-dap (MIT). `dap42` is ruled out for LGPL via libopencm3.

Reference: `research/cmsis-dap-survey.md` for the firmware survey, alternative SWD backends considered (ULP RISC-V, I2S/LCD-CAM, RMT, all rejected), and license analysis.

Implementation constraint (Phase 0.3 spike, see `research/spi2-swd-benchmark.md`): the standard ESP-IDF `spi_master` polling driver imposes ~25 µs of software overhead per transaction. A typical SWD read frame is two SPI transactions (header out, then data in across a turnaround), so under `spi_master` each frame takes ~50 µs regardless of SCLK rate; measured 53.28 µs at 10 MHz vs 49.77 µs at 40 MHz, a 7% gain for a 4x clock. Production code therefore uses one of:

- SPI2 SCT (Segmented Configure Transfer) mode, supported on ESP32-S3 (`SOC_SPI_SCT_SUPPORTED=1`), which queues a multi-segment descriptor list and runs them back-to-back without per-transaction software entry.
- Direct HAL or register-level programming of the SPI peripheral, bypassing `spi_master` entirely. This is the route taken by windowsair/wireless-esp8266-dap.

The DIR strobe driving the SWDIO translator must use `dedic_gpio` (single-cycle CPU-mapped GPIO) rather than `gpio_set_level()`. Measured: `gpio_set_level()` is ~335 ns per transition, which is 8.4 SWD bit cycles at 25 MHz, far longer than one turnaround bit. `dedic_gpio` is single-cycle (~6 ns).

### 4.7 SWO pipeline

UART1 in NRZ mode, RX-only, with the GDMA-capable UHCI peripheral as the DMA backend. Two-tier ring required because UHCI cannot DMA into PSRAM:

- **Tier 1**: 64 KB DRAM ring, UHCI writes directly.
- **Tier 2**: 8 MB PSRAM ring, drained from tier 1 by a pinned task on APP_CPU.
- **Network drain**: the USB/IP virtual SWO endpoint on the synthetic CMSIS-DAP busid pulls from tier 2 and emits virtual Bulk-IN URB completions back over the existing TCP connection.

Overflow policy follows DAPLink: set the `DAP_SWO_BUFFER_OVERRUN` flag and continue streaming on the next frame.

Designed-around footguns from the survey:

- probe-rs #448: emit ZLPs after exact-`wMaxPacketSize` writes on the SWO endpoint to avoid host-side stall.
- pyocd #855: GDB+SWV simultaneous use drops trace on some probes; integration tests must exercise this path.

### 4.8 Slave-mode personalities

Hybrid I2C-slave / SPI-slave model:

- **C-side**: hardware-driven register table responder. Two flat buffers per personality (read-table the DUT can read, write-table the DUT writes into), sized at activation time. The peripheral ISR fills the write-table and serves bytes from the read-table during transactions. No MP code in the critical path, response is sub-millisecond, no clock-stretching.
- **MP-side**: read or write either buffer at any time, optionally register a "notify on write to range X" callback that fires on a non-ISR FreeRTOS task after the relevant transaction completes.

I2C address, SPI mode, and frequency cap are configurable from MP at personality activation. I2C-slave and SPI-slave personalities are mutually exclusive on a given test (same translator pins are shared).

## 5. Network transport and discovery

### 5.1 Wi-Fi

Station mode only on rev1. MP brings up `network.WLAN(STA_IF)` with credentials read from NVS or a boot-time config file. Auto-reconnect on disconnect. lwIP runs on PRO_CPU (IDF default).

C modules link directly against `lwip/sockets.h` (BSD socket API) and own their own listeners.

### 5.2 mDNS

ESP-IDF `mdns` component, brought up by MP after Wi-Fi connect.

- Hostname: `annealage_pod-<chipid-suffix>.local`
- Service: `_annealage_pod._tcp.` advertising port 3240 (USB/IP)
- TXT record: `carrier-id`, `firmware-version`, `mp-version`, `repl-port`, `uart-port`

Static IP assignment is not part of firmware; deployments wanting stable addressing pin MAC-to-IP at the DHCP server.

### 5.3 TCP services

| Port | Service | Owner |
|---|---|---|
| 3240 | USB/IP server (DUT busid 1, synthetic CMSIS-DAP busid 2) | C `usbip` module |
| 8266 | MP REPL via `os.dupterm()` over a listening socket | MP boot script |
| 2000 | DUT UART forwarder (default; configurable) | C `uartbridge` module |
| 514/UDP | syslog out, optional | MP |

Port 8266 chosen for symmetry with WebREPL convention.

### 5.4 Lifecycle (hybrid stateless + cleanup hook)

Services start at boot and run forever. There is no explicit session state machine in firmware. On REPL TCP disconnect, an MP-registered cleanup hook runs; default behaviour is DUT power off, level translators tristated, in-flight queues drained. Test scripts that need DUT state to persist across host reconnect override the hook.

USB/IP detach is independent of REPL connection state; either may disconnect without affecting the other.

### 5.5 Trust model

The USB/IP server does not authenticate the TCP/3240 client. Anyone reachable at L2/L3 can issue `OP_REQ_IMPORT` and drive arbitrary `CMD_SUBMIT` (control + bulk + interrupt) on the bridged DUT, full URB-level access. This is the same model as the Linux kernel's `usbip-host`; mpy-pod inherits it.

Over Wi-Fi the attacker surface is wider than the Ethernet case the upstream kernel module assumes: any device on the same SSID can reach the annealage_pod unless network-level isolation is in place. For production deployments, restrict to a trusted lab VLAN, an isolated AP, or tunnel via SSH / WireGuard. The wire parser refuses `SET_ADDRESS` proxied from a remote (would silently desync TinyUSB's view of the DUT's bus address) and bounds-checks `ep`/`direction`/`blen` on every CMD_SUBMIT, but otherwise treats the client as trusted.

The TCP log socket and the MP REPL TCP socket share the same trust model. Telemetry exposure (current/voltage readings, SWO trace) is similarly unauthenticated.

## 6. Logging, watchdog, time

### 6.1 Logging

MP `print()` and ESP-IDF `ESP_LOGx` both go to stdout. stdout is dup'd to UART0 (always) and to a TCP log socket when a client is connected. No flash storage, no log rotation, no persistent journal.

Deployments that want persistence emit syslog over UDP from MP to a central collector. Off by default.

### 6.2 Watchdog

ESP-IDF task watchdog (`esp_task_wdt`). Each long-running C-module task subscribes; the MP main task subscribes and resets in its asyncio loop tick. Failure to reset within 30 s reboots the chip. Bootloader rollback covers the case where a fresh OTA image triggers the WDT before marking itself valid.

Hardware RTC-WDT enabled at the second-tier limit (60 s) as defence against task-WDT failure.

### 6.3 Time sync

MP-managed. After Wi-Fi connects, MP optionally calls `ntptime.settime()` against a configured NTP server. SWO timestamps and INA228 sample timestamps use the synced clock once available; before sync they use the monotonic uptime counter, and the host stitches by recording the first synced sample's offset.

Time sync is not required for the annealage_pod to function. Deployments without internet access just leave clocks uptime-based.

## 7. MicroPython API surface

### 7.1 Compatibility layer (RP_INFRA-equivalent)

Mimics the existing Octoprobe RP_INFRA API so testbed_micropython needs only a transport adapter (TCP REPL instead of USB serial). The full RP_INFRA surface is to be enumerated by parsing octoprobe/testbed_micropython; see §B.

### 7.2 Extensions

Methods the existing RP2040-based annealage_pod cannot offer:

```python
annealage_pod.power.vtarget.current_mA()
annealage_pod.power.vtarget.voltage_mV()
annealage_pod.power.dut_usb.current_mA()
annealage_pod.power.dut_usb.voltage_mV()
annealage_pod.power.dut_usb.vbus_present()

annealage_pod.dut.reset(mode='swd'|'nrst'|'power'|'relay', relay=N)

annealage_pod.slave.i2c.start(addr=0x42, read_buf_size=256, write_buf_size=256)
annealage_pod.slave.i2c.read_table()
annealage_pod.slave.i2c.write_table()
annealage_pod.slave.i2c.on_write(start, end, callback)
annealage_pod.slave.spi.start(mode=0, freq_max=10_000_000, ...)

annealage_pod.swo.bytes_buffered()
annealage_pod.swo.overruns_total()

annealage_pod.carrier.id()
annealage_pod.firmware.version()
```

Surface is provisional and frozen after the first prototyping pass.

## 8. Open items for follow-up

1. **DUT carrier pinout**: closed. See `docs/spec-appendix-A-pinmap.md`.
2. **RP_INFRA API surface**: closed. See `docs/spec-appendix-B-rp_infra-api.md`. The appendix flags 19 RP_INFRA methods and switch objects as gaps in §7 below; the appendix table is the canonical mapping.
3. **TinyUSB host on S3**: open. Verify ESP-IDF v5.5 TinyUSB host stack supports the operations the USB/IP server needs (raw URB submit on arbitrary endpoints, non-canned class-driver flow). If insufficient, fall back to the underlying `usb_host` IDF component directly. Tracked as risk R2.
4. **USB/IP synthetic-device extension**: closed. See `research/usbip-multiplexing-design.md`. Key: multiplexing is essentially free in the USB/IP protocol (esp-usbip-bridge's existing `virtual_device_t` ops table merges devices in `OP_REP_DEVLIST`); host-tool recognition is iInterface-string-based, not VID/PID-based, so the load-bearing field is `iInterface = "CMSIS-DAP"`; `OP_REP_IMPORT` does not carry interface descriptors. The design names six open implementation issues with concrete bring-up tests in §5.1.
5. **Pin budget reality check**: closed in Appendix A. Budget closes exactly at 33/33 with one decision required between (a) dropping legacy GPD6/7 channels in rev1, or (b) muxing them onto SPI-slave-only translator pins via a 74CBT3257.
6. **Carrier-compatibility scope re-confirm**: closed. Decision: replicate v0.7's 4-connector layout (J1501 / J201 / J501 / J202) on the new PCB, preserving drop-in mechanical compatibility with existing Octoprobe DUT carriers at the cost of a less convenient DUT-side carrier design. See §3.1.
7. **SPI2 SCT-mode (or HAL-direct) SWD driver**: open. Phase 0.3 spike (see `research/spi2-swd-benchmark.md`) showed the standard `spi_master` driver caps SWD throughput regardless of SCLK rate; production WS-D path is SCT mode or direct HAL/registers, plus `dedic_gpio` for the DIR strobe. Design and prototype before WS-D enters Phase 2.
8. **ADC2 / Wi-Fi conflict on VBUS_SENSE**: closed by Appendix A. Workaround is to read VBUS via the INA228 already monitoring the DUT-USB rail; the spec §3.3 ADC sense becomes the INA228 voltage register. GPIO42 freed.
9. **PCB design**: not part of this firmware spec; a separate PCB project consumes this spec and the appendix.

## 9. Alternate build variants and pivot paths

Not part of rev1 firmware. Documented routes if rev1 hits hard limits during Phase 3 or Phase 4 validation.

### 9.1 Ethernet + PoE variant on ESP32-P4

If Wi-Fi proves unable to deliver predictable URB timing for CMSIS-DAP under POD load (Phase 4.5 reliability runs the trigger), an Ethernet build variant replaces both the medium AND the silicon:

- **Silicon**: switch to ESP32-P4. The S3 has no built-in MAC and bolting a SPI MAC (W5500) on it adds latency and burns a SPI controller; using a sibling Espressif silicon with native EMAC is cleaner. ESP32-P4 brings: native 10/100 EMAC with RMII PHY (LAN8720 or RTL8201), USB 2.0 OTG HighSpeed host (480 Mbps; 40x the S3's FullSpeed and a free upgrade for DUT-USB throughput), dual RISC-V cores at 400 MHz, larger SRAM, 32-bit external DDR controller. Trade-off: P4 has no Wi-Fi or BLE; for this variant Wi-Fi is intentionally dropped, so this is acceptable.
- **PHY and PoE**: RMII PHY (LAN8720 typical) on the P4's EMAC pins; magnetics + RJ45 jack with center-tap leads to a PoE front-end. PoE options: passive 12V splitter (cheapest, off-board), active 802.3af/at front-end (TI TPS23730 or onsemi NCP1095) for full-spec deployment. PoE on-board is a rev2-class addition; rev1 of the Ethernet variant uses an external PoE injector.
- **Build**: a sibling board variant `ESP32_P4_ANNEALAGE_POD_ETH` against MicroPython's esp32 port (P4 support landed upstream in MP, verify the released tag at variant-cut time; if not yet mature this variant is gated on it). sdkconfig replaces Wi-Fi with the IDF Ethernet driver. C modules are unchanged because they speak BSD sockets via lwIP irrespective of medium. SPI2-SCT SWD backend, UHCI SWO pipeline, GDMA, and `dedic_gpio` are all available on P4 with the same APIs.
- **Maintenance posture**: this is more than a build variant; it is a sibling silicon target sharing firmware concepts (USB host, USB/IP server, CMSIS-DAP, MP package). Expect divergence in the board variant directory and the SWD/SWO engine pin choices. Treat as ~80% shared code, ~20% silicon-specific.
- **Trigger criteria**: telemetry from Phase 4.5 reliability runs (`test/pod/reliability-rev1.md`) showing pass-rate degradation correlated with Wi-Fi disconnects, USB/IP RTT spikes > 50 ms, or SWO drop rate > 0.1% under sustained trace.

### 9.2 Embedded Black Magic Probe pivot

If CMSIS-DAP-v2 over USB/IP turns out too timing-sensitive (most likely failure mode: the SPI driver overhead + USB/IP RTT + USB-host scheduling jitter stack-up makes flash programming throughput or SWD step-over latency unacceptable), pivot the debug-probe role from synthetic CMSIS-DAP-v2 to embedded Black Magic Probe (BMP):

- BMP exposes a GDB extended-remote server over USB-CDC instead of CMSIS-DAP-v2 over USB-Bulk. The host's `gdb` connects directly via the CDC; OpenOCD, pyOCD, and probe-rs's CMSIS-DAP paths are bypassed.
- The S3 still acts as USB host to the DUT; the probe identity becomes a synthetic USB-CDC device (instead of synthetic CMSIS-DAP-v2) on the same USB/IP server. The multiplexing pattern from `research/usbip-multiplexing-design.md` is unchanged; only the synthetic device's descriptor stack changes.
- SWD I/O and SWO capture engines (WS-D scope) are reused unchanged; only the USB-side framing changes from CMSIS-DAP-v2 Bulk to GDB-remote-over-CDC.
- Cost: pyOCD and probe-rs lose direct support of the probe through their CMSIS-DAP backends. probe-rs has a separate BMP probe driver; pyOCD has experimental BMP support. testbed_micropython does not currently invoke any debug-probe tooling (per Appendix B), so the GDB-only pivot does not regress today's CI flow.
- Source: a fork of `blacksphere/blackmagic`, replacing its ARM-Cortex SWD bit-bang backend with our SPI2 SCT engine from WS-D. Existing community ports of BMP to ESP32-S3 to be evaluated as the starting point.
- **Trigger criteria**: WS-D + WS-C integration in Phase 3 measures flash-programming throughput < 50 KB/s, SWD half-word write latency > 100 µs, or step-over RTT > 200 ms. Any one is sufficient; Phase 3 P3.1 step 5 captures these numbers.

Both variants share most of rev1's firmware: build infrastructure, MP package, USB host, USB/IP server, lifecycle, OTA, slave-mode personalities, console UART. Only the network medium (9.1) or the probe identity (9.2) changes. Treating them as build variants rather than forks keeps the maintenance burden low and allows the same hardware PCB to host either configuration.

## Appendix A: pin map

See `docs/spec-appendix-A-pinmap.md`.

## Appendix B: RP_INFRA API mimicry

See `docs/spec-appendix-B-rp_infra-api.md`.
