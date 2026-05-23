# Annealage Pod bring-up runbook

End-to-end recipe for bringing up a freshly built Annealage Pod firmware on the connected ESP32-S3 dev board, then exercising it against a DUT.

## 1. Prerequisites

On the host:
- ESP-IDF v5.5.1 provisioned (the in-tree `src/tools/setup-idf.sh` finds it under `/home/corona/cyd/...` or installs).
- `mpy-dev` configured with the ESP32-S3 board labelled `esp32-s3`.
- `mpremote`, `usbip-utils`, `pyOCD` installed.
- A DUT registered with `mpy-dev` (e.g. `pico-w` or `pico2-w`).

On the Annealage Pod board:
- ESP32-S3-WROOM-1 module with octal PSRAM (N16R8) recommended; lesser variants fit but the SWO PSRAM tier-2 ring shrinks.

## 2. Build and flash

```bash
cd /home/corona/mpy-pod
bash src/tools/build.sh              # idempotent; ~3 minutes clean, seconds incremental
bash src/tools/flash.sh                # writes to mpy-dev label esp32-s3
```

Smoke-verify on UART0 console:

```bash
mpremote connect "$(mpy-dev tty esp32-s3)" resume exec '
import sys; print(sys.implementation._build)
'
# expected: ESP32_S3_ANNEALAGE_POD
```

## 3. Provide Wi-Fi credentials

Drop `credentials.json` onto the device's filesystem:

```bash
cat > /tmp/credentials.json <<'EOF'
{
  "ssid": "YOUR_SSID",
  "password": "YOUR_PASSWORD",
  "hostname": "annealage-pod-myboard"
}
EOF
mpremote connect "$(mpy-dev tty esp32-s3)" resume cp /tmp/credentials.json :credentials.json
```

The `hostname` key sets the mDNS name (`annealage_pod-myboard.local`). If omitted, the device uses `annealage_pod-<last6hex>` derived from the chip UID. (Credentials may also be stored in NVS via IDF APIs; `boot.py` reads `credentials.json` first.)

## 4. Wire the DUT

The canonical source of truth is `src/mpy/annealage_pod/_pinmap.py`. Summary of current assignments:

| GPIO | `_pinmap.py` constant | Function | Notes |
|---|---|---|---|
| 1-7 | `RELAY_GPIO[1..7]` | Relay drives | opto-coupler low-side switches |
| 8 | `LOCAL_I2C_SDA` | Local I2C SDA | INA228 monitors, carrier EEPROM |
| 9 | `LOCAL_I2C_SCL` | Local I2C SCL | |
| 10 | `SWCLK` | SWD clock | SPI2 SCLK via IO_MUX |
| 11 | `SWDIO` | SWD data | SPI2 D, half-duplex 3-wire |
| 12 | `SWDIO_DIR` | SWD direction | controls SWDIO translator |
| 13 | `SWO` | SWO / trace | UART1 RX, UHCI |
| 14 | `NRST` | DUT nRST | open-drain |
| 17 | `DUT_UART_TX` / `DUT_I2C_SDA_OR_SPI_MOSI` | DUT UART2 TX / I2C SDA / SPI MOSI | UART2 TX; IDF requires explicit GPIO_ENABLE_W1TS after uart_set_pin |
| 18 | `DUT_UART_RX` / `DUT_I2C_SCL_OR_SPI_SCK` | DUT UART2 RX / I2C SCL / SPI SCK | UART2 RX |
| 19 | `DUT_USB_DP` | DUT USB D+ | direct, 3v3 USB FullSpeed |
| 20 | `DUT_USB_DM` | DUT USB D- | direct |
| 21 | `DUT_I2C_SDA_DIR` | I2C SDA translator direction | |
| 38 | `DUT_SPI_MISO` | SPI MISO | |
| 39 | `DUT_SPI_CS` | SPI CS | |
| 40 | `VTARGET_EN` | VTARGET rail enable | TPS2595 EN/UVLO |
| 41 | `DUT_USB_VBUS_EN` | DUT USB VBUS enable | TPS2595 EN/UVLO |
| 42 | `VBUS_SENSE` | VBUS analog sense | reserved; VBUS read via INA228 in practice |
| 43 | `UART0_TX` | Console UART0 TX | CH340N onboard bridge |
| 44 | `UART0_RX` | Console UART0 RX | |
| 45 | `LED_STATUS_1` | Status LED 1 | |
| 46 | `LED_STATUS_2` | Status LED 2 | |
| 47 | `GPD0` | General purpose DUT IO 0 | carrier v0.7 GPD0; used as DUT RUN/RESET on dabao carrier |
| 48 | `GPD1` | General purpose DUT IO 1 | carrier v0.7 GPD1; used as DUT PROG on dabao carrier |

For Phase 3 P3.1 a minimal subset suffices: USB D+/D-, GND, and 5V VBUS. SWD and UART can be deferred.

## 5. Boot the Annealage Pod, confirm Wi-Fi

After a power cycle (or `machine.reset()`), watch the UART0 console:

```bash
bash src/tools/monitor.sh
```

Expected log lines:

```
annealage_pod.boot: connecting to <SSID>...
annealage_pod.boot: WiFi up, IP=192.168.x.y
annealage_pod.boot: mDNS announce: annealage_pod-<chipid>.local
annealage_pod.boot: usbip server bound on :3240
annealage_pod.boot: dapprobe attached as busid 1-2
annealage_pod.boot: uartbridge bound on :2000
annealage_pod.boot: REPL listener on :8266
```

Find the Annealage Pod's IP / mDNS name:

```bash
# mDNS (preferred - works once the device is on the network):
avahi-browse -rt _annealage_pod._tcp
# macOS:
dns-sd -B _annealage_pod._tcp local.
```

The TXT record `firmware-version` and `mp-version` fields confirm the running build.

If mDNS is not available, query the IP directly over the UART0 serial REPL before the network address is known:

```bash
mpremote connect "$(mpy-dev tty esp32-s3)" resume exec \
  "import network; print(network.WLAN(network.STA_IF).ifconfig()[0])"
```

The boot log printed to UART0 also contains `Wi-Fi up, ifconfig=(IP, ...)` so monitoring the console immediately after power-on is sufficient if the device is freshly booting:

```bash
bash src/tools/monitor.sh
```

Save the hostname; subsequent steps use it. Integration tests resolve the mDNS name automatically and fall back to the raw IP if resolution fails - override with `ANNEALAGE_POD_HOST=annealage_pod-myboard.local` or `USBIPD_IP=192.168.x.y`.

## 6. Run the Phase 3 smoke test

```bash
cd /home/corona/mpy-pod
bash test/integration/phase3/run.sh annealage_pod-<chipid>.local pico2-w
```

The script walks through:

| Check | Expectation |
|---|---|
| P3.1.1 | `usbip list -r` shows >=2 busids (DUT + synthetic CMSIS-DAP) |
| P3.1.2 | TCP REPL on :8266 returns build id `ESP32_S3_ANNEALAGE_POD` |
| P3.1.3 | TCP/2000 accepts UART bridge connections |
| P3.1.4 | `pyocd list` includes a CMSIS-DAP probe |
| P3.2 | Cleanup hook drops both rails on REPL disconnect |
| P3.5 | Log socket (opt-in, separate test) |

Failures print which check failed and dump the relevant tool output. Re-run individual sub-tests by editing the script.

## 7. Drive a real DUT through pyOCD

After `usbip attach -r <host> -b 1-2` (the synthetic CMSIS-DAP busid), pyOCD sees the probe locally. Then:

```bash
pyocd reset --target rp2040           # halt + reset the DUT via SWD
pyocd flash --target rp2040 my_dut.elf
pyocd gdbserver --target rp2040 &
arm-none-eabi-gdb my_dut.elf -ex 'target extended-remote :3333'
```

For SWO: `pyocd commander --target rp2040 -O swv=1` (configures the trace pipeline; trace data flows back through the Annealage Pod's SWO ring and out the synthetic device's EP3 Bulk-IN).

## 8. Auto-reconnect across DUT resets: `pod-connect`

When the DUT resets (firmware reload, bootloader entry, brownout) its USB device disappears and the local vhci port drops with no automatic recovery. `src/tools/pod-connect.py` is a small daemon that re-attaches the DUT whenever it (re)appears.

```bash
python3 src/tools/pod-connect.py
```

Defaults: watches `c251:f00b` (the annealage_pod's uartcdc bridge) on `annealage_pod-dabao.local` (override with `--host` or `ANNEALAGE_POD_HOST`). Detaches the vhci port on Ctrl-C / SIGTERM.

Bootloader recovery (the DUT enumerates with a different VID:PID in BOOTSEL): pass the comma-separated list so the daemon re-attaches either state.

```bash
python3 src/tools/pod-connect.py --vidpid c251:f00b,2e8a:0003,2e8a:000f
```

Options:

| Flag | Default | Effect |
|---|---|---|
| `--host` | `ANNEALAGE_POD_HOST` env / `annealage_pod-dabao.local` | annealage_pod hostname; falls back to `USBIPD_IP` env if mDNS fails |
| `--vidpid` | `c251:f00b` | comma-separated DUT VID:PIDs to watch |
| `--mode {auto,hub,poll}` | `auto` | event source - see below |
| `--hub-vidpid` | `c251:f00c` | notification beacon VID:PID |
| `--poll-waiting MS` | 150 | poll interval while waiting for the DUT |
| `--poll-attached MS` | 1000 | poll interval while attached |
| `--once` | off | attach once and exit (leaves the device attached) |

### Modes

- `poll`: the legacy path. Polls `usbip list -r HOST` every 150 ms while waiting, `usbip port` every 1 s while attached. Reconnect latency ~150-300 ms.
- `hub`: requires `python3-libusb1` and a udev rule (see below). Attaches the annealage_pod's notification device (`c251:f00c`, busid `2-2`) over usbip and reads its interrupt-IN endpoint for DUT mount/umount edges directly. Reconnect latency ~50 ms. Exits non-zero if libusb1 is not installed.
- `auto` (default): try hub mode first, fall back to polling on any failure. The fallback is announced at WARNING level.

### Hub mode prerequisites

```bash
pip install --user libusb1                                  # or: apt install python3-libusb1
sudo cp src/tools/99-annealage-pod-hub.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

The udev rule grants access to anyone in `plugdev` and to the logged-in console user (`TAG+="uaccess"`). On Fedora/Arch/NixOS the `plugdev` group may not exist; the `uaccess` tag covers those.

Notes:
- In hub mode, Ctrl-C may take up to 2 seconds to take effect (libusb is blocked on the interrupt read). The daemon still detaches the DUT cleanly before exiting.
- Hub mode does not eliminate `usbip attach` - the DUT is still a separate busid; the hub only signals when to re-attach.
- The daemon does NOT detach `hub_device` (busid 2-2) on exit so the next run reopens it immediately.

## 9. Tear down

```bash
usbip detach -p 0
mpremote connect "tcp://<host>:8266" exec 'machine.reset()'   # optional
```

The cleanup hook runs on REPL disconnect: rails go off, level translators tristate, queues drain. The next session starts from a known idle state.

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| MP REPL silent on UART0 after flash | Board mode stuck in BOOT | Press RESET; if persistent, hold BOOT, RESET, release BOOT |
| `boot.py` skips Wi-Fi | `credentials.json` missing or malformed | Re-copy with valid JSON, then `import machine; machine.reset()` |
| `usbip list -r` returns 1 busid | DUT not enumerated | Confirm USB D+/D- + 5V wired; check `annealage_pod.power.dut_usb.on()` |
| `usbip list -r` returns 0 busids | mDNS resolved to wrong IP, or USB/IP server not bound | Confirm mDNS, `mpremote connect tcp://...:8266 exec 'import usbip; print(usbip.is_running())'` |
| `pyocd list` empty after attach | iInterface descriptor missing "CMSIS-DAP" | Verify dapprobe attach succeeded; check synthetic_device.c iInterface string |
| OTA fails | rev1 ships pure-MP OTA only (urequests + esp32.Partition); the C shim is deferred (see commit affe3e4) | Use `annealage_pod.ops.ota.update(url, experimental_pure_mp=True)` |
| SWD TIMEOUT on flash | DIR strobe missing or wrong polarity | Check 74LVC1T45 DIR pin assignment; oscilloscope on SWDIO during a transfer |
| SWO trace drops | Tier-2 PSRAM ring full | Confirm host drains; check `annealage_pod.swo.overruns_total()` |
