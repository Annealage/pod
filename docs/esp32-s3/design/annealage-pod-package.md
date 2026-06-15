# annealage_pod.* MicroPython package design

WS-E deliverable. Describes the layout of `src/mpy/annealage_pod/`, the
dependency on each user C module, the INA228 driver origin, and the
RP_INFRA compat-shim notes.

## Module layout

```
src/mpy/annealage_pod/
├── __init__.py             package surface; re-exports __version__
├── _version.py             "0.1.0-ws-e"
├── _pinmap.py              GPIO assignment table from Appendix A §A.5.1
├── _ina228.py              INA228 I2C driver (origin: TI datasheet)
├── boot.py                 boot orchestration (Wi-Fi, mDNS, REPL, C modules)
├── carrier.py              carrier-id EEPROM read + hw_version
├── compat.py               RP_INFRA-equivalent shim (Appendix B)
├── dut.py                  reset(mode='swd'|'nrst'|'power'|'relay')
├── power.py                VTARGET / DUT-USB rails + INA228 telemetry
├── relays.py               opto-coupled relay 1..7 control
├── slave.py                I2C / SPI slave personality wrappers
├── supervisor.py           cleanup-hook lifecycle
├── credentials.example.json template; users supply /credentials.json
└── ops/                    OTA, watchdog, log, time
    ├── __init__.py
    ├── ota.py
    ├── wdt.py
    ├── log.py
    └── time.py
```

`__init__.py` re-exports only `__version__`. Submodules are imported
on demand by callers (`from annealage_pod import compat`) so a Unix-port
unit test that only exercises `compat.set_relays(...)` does not pull
in `boot`'s `network` import.

## Dependency on each C user module

| MP module                  | C module    | Surface used                                      | If C surface missing                         |
|----------------------------|-------------|---------------------------------------------------|----------------------------------------------|
| `annealage_pod.boot`            | `usbip`     | `usbip.start()`                                   | logs and continues                           |
| `annealage_pod.boot`            | `dapprobe`  | `dapprobe.attach()` (preferred) or `dapprobe.start()` | logs and continues                       |
| `annealage_pod.boot`            | `uartbridge`| `uartbridge.start()`                              | logs and continues                           |
| `annealage_pod.dut.reset(swd)`  | `dapprobe`  | `dapprobe.system_reset()` (TODO WS-C)             | logs TODO; falls back to `start()`           |
| `annealage_pod.slave.i2c.*`     | `slaveio`   | `i2c_start / i2c_read_table / i2c_write_table / i2c_on_write` (TODO WS-F) | raises NotImplementedError |
| `annealage_pod.slave.spi.*`     | `slaveio`   | `spi_start / spi_read_table / spi_write_table / spi_on_read` (TODO WS-F)  | raises NotImplementedError |
| `annealage_pod.ops.ota.update`  | `esp_https_ota` shim | `esp_https_ota.update(url)` (TODO WS-H)  | raises unless `experimental_pure_mp=True`    |

The C modules currently expose only `start()` (Phase 1 skeletons).
The WS-E code paths above call `start()` and add a TODO comment
naming the workstream that will land the wider surface.

`annealage_pod.power`, `annealage_pod.relays`, `annealage_pod.dut.reset(nrst|power|relay)`,
`annealage_pod.compat`, `annealage_pod.supervisor`, `annealage_pod.carrier`, and the
`annealage_pod.ops` submodules call no C user-module APIs; they use only
MicroPython core (`machine.Pin`, `machine.I2C`, `machine.WDT`,
`network`, `socket`, `os.dupterm`, `ntptime`, `esp32.Partition`).

## INA228 driver origin

`_ina228.py` is a clean-room implementation written from the Texas
Instruments INA228 datasheet (SBOS882, July 2021 revision). No vendor
source vendored. The driver is covered by the repo-root PolyForm Noncommercial licence.

Scope kept narrow: configure shunt calibration, read VBUS voltage and
shunt CURRENT registers, optionally die temperature. Wider features
(alerts, energy and charge accumulators, SHUNT_CAL temperature
compensation) are out of scope until a downstream consumer needs them.

Register layout used (matches SBOS882 Table 7.6):

| Addr | Reg          | Width | Notes                                          |
|------|--------------|-------|------------------------------------------------|
| 0x00 | CONFIG       | 16    | software reset bit 15; ADCRANGE bit 4          |
| 0x01 | ADCCONFIG    | 16    | conversion times and averaging                 |
| 0x02 | SHUNT_CAL    | 16    | 13107.2e6 * CURRENT_LSB * RSHUNT (ADCRANGE=0)  |
| 0x05 | VBUS         | 24    | unsigned, 195.3125 µV/LSB                      |
| 0x07 | CURRENT      | 24    | signed, CURRENT_LSB scaling                    |

CURRENT_LSB chosen as `max_expected_amp / 2^19` so that 0.5 LSB error
is well below the rail's expected operating noise.

VBUS-present detection (DUT-USB) uses the INA228 voltage register and
not GPIO42 ADC2: per Appendix A §A.7 §8 the ADC2/Wi-Fi conflict makes
a runtime VBUS read on GPIO42 unreliable when Wi-Fi is active. Spec
§8.8 closes this open item by routing the VBUS sense through the
INA228 already monitoring the rail.

## Compat shim notes (Appendix B)

`annealage_pod.compat` reproduces every name the upstream Octoprobe
`lib_annealage_pod_infra_pico.py` exposes so that `testbed_micropython`
sees the same symbols over the new TCP REPL transport.

Reproduced surface:

- 3 module-level vars: `pico_unique_id`, `gpio_hw_version`,
  `files_on_flash`.
- 6 status / probe Pin objects: `pin_LED_ACTIVE`, `pin_LED_ERROR`,
  `pin_DUT`, `pin_PICO_PROBE_RUN`, `pin_PICO_PROBE_BOOT`.
- 7 relay Pin objects: `pin_RELAY1` through `pin_RELAY7`, plus
  `pin_relays` dict.
- 4 helpers: `set_switch`, `get_relays`, `set_relays`,
  `set_relays_pulse`.

Each Pin object is a thin `_ShimPin` proxy with `.value()` reads and
writes delegated to the underlying annealage_pod.* primitive:

| Shim Pin             | Backing primitive                          |
|----------------------|--------------------------------------------|
| `pin_LED_ACTIVE`     | local _led_state dict + GPIO45             |
| `pin_LED_ERROR`      | local _led_state dict + GPIO46             |
| `pin_DUT`            | `power.vtarget.set(on)` / `is_on()`        |
| `pin_PICO_PROBE_RUN` | local dict (no real RP_PROBE on new HW)    |
| `pin_PICO_PROBE_BOOT`| local dict (no real RP_PROBE on new HW)    |
| `pin_RELAY1..7`      | `relays.relays.set(n, v)` / `get(n)`       |

`gpio_hw_version` is hard-coded to `7` so the v0.7 mapping in
`octoprobe/lib_annealage_pod_infra_pico.py:138-148` resolves to "v0.7"
without code changes upstream. Carriers that want a different hw
version add a strap-pin sampler here.

`files_on_flash` is `0` because the new annealage_pod ships with frozen
modules and an empty `vfs` partition. Code that needs the live count
should call `os.listdir()` directly.

### get_relays bug fix

Appendix B §B.7 flags
`octoprobe/lib_annealage_pod_infra_pico.py:59-61` for shadowing the
parameter name:

```python
# Upstream, buggy:
def get_relays(relays):
    return bool(pin_relays[i].value())   # `i` is unbound
```

Our shim version uses the parameter consistently:

```python
def get_relays(relay):
    return bool(pin_relays[relay].value())
```

This is verified by `test/unit/annealage_pod/test_compat_shim.py::test_get_relays_uses_parameter_name_correctly`.

### RP_PROBE switch no-ops

Appendix B §B.5 flagged `proberun`, `probeboot`, `infra`, `infraboot`
as having no equivalent on the new hardware (RP_PROBE is replaced by
the synthetic CMSIS-DAP-v2). The recommendation was silent no-op so
`op` CLI keeps working without exception spam, with an opt-in
`set_strict(True)` for callers that want hard failures. This is what
the shim does: writes are stored in module-level dicts, reads return
the stored value, no exception is raised by default.

`compat.set_strict(True)` toggles a module flag callers can read via
`compat.is_strict()`. The flag is wired in tests; the shim itself
treats it as advisory until a follow-up phase needs the strict
behaviour.

## Boot orchestration

`annealage_pod.boot.up()` runs once at first call:

1. Read `/credentials.json` for SSID / password.
2. `network.WLAN(STA_IF)` connect with a 15 s timeout.
3. mDNS announce (`_annealage_pod._tcp.` on port 3240; TXT carries
   firmware-version, mp-version, repl-port, uart-port).
4. `usbip.start()` -> port 3240 listener.
5. `dapprobe.attach()` (or `dapprobe.start()` until WS-C exposes
   the wider surface).
6. `uartbridge.start()` -> port 2000 listener.
7. Bind a TCP listener for the REPL on port 8266.
8. If Wi-Fi came up, mark the running OTA image valid via
   `esp32.Partition.mark_app_valid_cancel_rollback()`.

The function returns a status dict so callers can introspect what
came up.

`supervisor.register_cleanup(cb)` lets test scripts replace the
default cleanup hook (DUT power off, both rails off, all relays
open). The boot path is responsible for running `run_cleanup()` on
REPL TCP disconnect; the WS-A integration test (Phase 3) wires that
in.

## Watchdog

`annealage_pod.ops.wdt.subscribe(timeout_ms)` returns the
`machine.WDT(timeout=timeout_ms)` object. The MP main task is
expected to call `annealage_pod.ops.wdt.kick()` once per asyncio tick.
Long-running C-module tasks subscribe to `esp_task_wdt` from C, not
through this MP-side wrapper.

## Test plan

Unit tests in `test/unit/annealage_pod/` cover:

- import smoke: every submodule (including the `ops/` subpackage)
  imports cleanly on the Unix port (CPython suffices).
- `annealage_pod.relays`: get/set/batch/pulse/all_off semantics with the
  software pin stand-in.
- `annealage_pod.power`: rail on/off/cycle, `vbus_present()` threshold,
  current/voltage default to 0.0 without I2C.
- `annealage_pod.dut.reset`: each of the four modes plus error paths
  (unknown mode, missing relay arg, out-of-range relay number).
- `annealage_pod.compat`: 3 module vars, 12 Pin objects, 4 helpers, the
  get_relays bug fix, and the strict-mode toggle.
- `annealage_pod.supervisor`: hook registration, ordered execution, swallow
  hook exceptions, default-cleanup does not raise.
- asyncio dry-run: every annealage_pod.* primitive reachable from inside
  an `asyncio.run()` body without blocking.

Run with `pytest test/unit/annealage_pod/` from the worktree root.

Phase 2 exit criteria for WS-E (per
`plan/phase-2-parallel-implementation.md`):

- frozen `annealage_pod.*` package compiles cleanly into the
  ESP32_S3_ANNEALAGE_POD board variant via `src/tools/build.sh`.
- import + asyncio dry-run on the Unix port pass without hardware.
- compat shim covers every RP_INFRA Pico-side symbol from
  Appendix B §B.2.

Hardware-flash + on-target REPL exercise of every method is Phase 3
work; it is explicitly out of scope for WS-E.
