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
  "password": "YOUR_PASSWORD"
}
EOF
mpremote connect "$(mpy-dev tty esp32-s3)" resume cp /tmp/credentials.json :credentials.json
```

(or set them in NVS via the IDF NVS APIs; `boot.py` reads `credentials.json` first.)

## 4. Wire the DUT

Refer to `docs/spec-appendix-A-pinmap.md` §A.5.1 for the canonical S3 GPIO assignments. For the dev-kit-on-perfboard configuration in Phase 3:

| DUT signal | S3 GPIO | Translator | Notes |
|---|---|---|---|
| USB D- (DUT) | GPIO19 | none | direct, 3v3 USB FullSpeed |
| USB D+ (DUT) | GPIO20 | none | direct |
| SWCLK (DUT) | GPIO10 | 74LVC1T45 (DIR low, A->B fixed-out) | SPI2 SCLK via IO_MUX |
| SWDIO (DUT) | GPIO11 | 74LVC1T45 (DIR controlled by GPIO12) | SPI2 D, half-duplex 3-wire |
| nRST (DUT) | (TBD per board) | OD translator | optional, falls back to SWD AIRCR |
| UART_TX (S3 -> DUT) | GPIO15 | 74LVC1T45 fixed B<-A | UART2 TX |
| UART_RX (DUT -> S3) | GPIO16 | 74LVC1T45 fixed A<-B | UART2 RX |
| SWO (DUT -> S3) | GPIO13 | 74LVC1T45 fixed A<-B | UART1 RX, UHCI |
| 5V VBUS to DUT | (FET-gated) | n/a | gated by annealage_pod.power.dut_usb |
| 3v3 VTARGET | (FET-gated) | n/a | gated by annealage_pod.power.vtarget |
| GND | shared | n/a | star at annealage_pod |

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
avahi-browse -tr _annealage_pod._tcp
# or:
dns-sd -B _annealage_pod._tcp local.
```

Save the hostname; subsequent steps use it.

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

## 8. Tear down

```bash
usbip detach -p 0
mpremote connect "tcp://<host>:8266" exec 'machine.reset()'   # optional
```

The cleanup hook runs on REPL disconnect: rails go off, level translators tristate, queues drain. The next session starts from a known idle state.

## 9. Troubleshooting

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
