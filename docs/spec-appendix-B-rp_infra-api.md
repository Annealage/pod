# Appendix B: RP_INFRA API mimicry

Status: research draft, populated from upstream sources cloned 2026-04-29.

This appendix enumerates the existing Octoprobe RP_INFRA MicroPython surface so the
new ESP32-S3 annealage_pod can offer a compatible API. It also catalogues the wire
protocol the Python host uses today, the RP_PROBE host-side dependencies that
must survive the swap to the synthetic CMSIS-DAP-v2 probe, and the gaps in
spec.md section 7.

The new annealage_pod replaces RP_INFRA, RP_PROBE, the on-PCB 4-port USB hub, and the
upstream USB transport in one go. All file:line citations below refer to the
clones under /tmp/octoprobe-research/.

## B.1 Repos consulted

Cloned to /tmp/octoprobe-research/ as plain git checkouts (not submodules).

| Repo | Role | Relevance |
|---|---|---|
| octoprobe/octoprobe | Core Python library: annealage_pod abstraction, USB hub control, MpRemote wrapper, RP_INFRA MicroPython base code, DUT programmer dispatch | Primary source of the RP_INFRA API. The MicroPython that runs on RP_INFRA is shipped here as a Jinja2 string literal, not as a separate firmware repo. |
| octoprobe/testbed_micropython | pytest-based MicroPython test harness | Consumer of the RP_INFRA API. Touches a small subset only via annealage_pod.infra.switches and annealage_pod.dut. |
| octoprobe/annealage_pod | Hardware (KiCad, docs); same content as referencea/annealage_pod | No firmware. Confirmed by listing top level: docs, kicad, model3d_spacer_bolzone. |
| octoprobe/usbhubctl | Python uhubctl reimplementation | Host-side helper used to switch USB hub power per port; not on the annealage_pod. Listed for completeness; not relevant to the firmware API. |
| octoprobe/octohub4 | 4-port USB hub design | Hardware only, not consulted further. |
| octoprobe/fork_micropython | MicroPython fork | Not relevant to the API; the firmware spec pins a stock RPI_PICO build. |
| octoprobe/testbed_heatguard, testbed_showcase | Other testbeds using octoprobe | Not consulted; do not change the RP_INFRA contract. |
| octoprobe/build_binaries, testbed_micropython_reports, testbed_micropython_runner_obsolete | Tooling and reporting | Not relevant. |

Key finding: there is no separate "RP_INFRA firmware" repo. The MicroPython
running on the RP_INFRA Pico is a stock RPI_PICO MicroPython release loaded
once at boot, then a Jinja2-rendered code blob is shipped over the raw REPL by
the host on every fresh power-up of the Pico. The pinned firmware is declared
in octoprobe/src/octoprobe/util_annealage_pod_infra_firmware.json:1-5 and is
currently RPI_PICO-20250911-v1.26.1.uf2.

## B.2 RP_INFRA on-Pico MicroPython surface

The complete on-Pico symbol table is the Jinja2 base code in
lib_annealage_pod_infra_pico.py:23-79 (octoprobe/src/octoprobe/lib_annealage_pod_infra_pico.py).
Everything visible to the host is one of:

1. A module-level variable set during base-code load.
2. A module-level `Pin` object in a fixed naming scheme.
3. One of three module-level helper functions.

### B.2.1 Module-level variables

| Symbol | Type | Source | Description |
|---|---|---|---|
| `pico_unique_id` | str (hex) | lib_annealage_pod_infra_pico.py:30 | `ubinascii.hexlify(machine.unique_id())`. Read once into `InfraPico._unique_id`. |
| `gpio_hw_version` | int (0..3) | lib_annealage_pod_infra_pico.py:31-33 | 5-bit value sampled from GPIOs 29,28,27,26,25 with pull-down. Mapped to v0.3..v0.7 in lib_annealage_pod_infra_pico.py:138-148. |
| `files_on_flash` | int | lib_annealage_pod_infra_pico.py:34 | `len(os.listdir())` from CWD; used to assert the flash is clean. |

### B.2.2 Module-level Pin objects

All declared at base-code load time. No runtime introspection of Pin objects;
the host only writes/reads through the helper functions or by calling
`.value()` directly via REPL eval.

| Symbol | GPIO | Direction | Source | Notes |
|---|---|---|---|---|
| `pin_LED_ACTIVE` | GPIO24 | OUT | lib_annealage_pod_infra_pico.py:36 | Maps to `Switch.LED_ACTIVE`. |
| `pin_LED_ERROR` | GPIO20 | OUT | lib_annealage_pod_infra_pico.py:37 | Not connected on v0.3. Maps to `Switch.LED_ERROR`. |
| `pin_DUT` | GPIO23 | OUT | lib_annealage_pod_infra_pico.py:38 | Not connected on v0.3. Controls DUT power switch on >= v0.5. |
| `pin_PICO_PROBE_RUN` | GPIO22 | OUT | lib_annealage_pod_infra_pico.py:39 | Holds RP_PROBE in reset (False) or release (True). |
| `pin_PICO_PROBE_BOOT` | GPIO21 | OUT, value=1 | lib_annealage_pod_infra_pico.py:41 | Drives RP_PROBE BOOTSEL. False = pressed (programming mode). |
| `pin_RELAY1` .. `pin_RELAY7` | GPIO1..GPIO7 | OUT | lib_annealage_pod_infra_pico.py:43-45 | The 7 opto-coupled relays. `pin_relays` dict at lib_annealage_pod_infra_pico.py:47-51 indexes them by 1..7. |

### B.2.3 Module-level helper functions

| Function | Signature | Source | Behavior |
|---|---|---|---|
| `set_switch(pin, on: bool) -> bool` | one Pin, bool | lib_annealage_pod_infra_pico.py:53-57 | Writes the value, returns True iff the value actually changed. |
| `get_relays(relays) -> bool` | int (1..7) | lib_annealage_pod_infra_pico.py:59-61 | Returns True if relay closed. Note: parameter named `relays` but indexed as `i`; latent bug, see B.7 open question. |
| `set_relays(list_relays) -> bool` | list[tuple[int, bool]] | lib_annealage_pod_infra_pico.py:63-71 | For each (relay_number, close) pair, writes the relay; returns True iff at least one relay state changed. |
| `set_relays_pulse(relays, initial_closed, durations_ms) -> None` | int, bool, list[int] | lib_annealage_pod_infra_pico.py:73-79 | Sets relay to `initial_closed`, then for each duration_ms sleeps that long and toggles. Used for double-tap reset patterns (SAMD, NRF). |

There are no other functions, no class definitions, no event loops, no
asyncio, no persistent main.py. The Pico runs the raw REPL only.

## B.3 Host-side wrapper API

The Python-side wrapping that any test code (or testbed_micropython) calls.
This is the surface the new ESP32 annealage_pod needs the host adapter to keep
working byte-compatibly.

### B.3.1 octoprobe.lib_annealage_pod.Annealage PodBase (lib_annealage_pod.py:105-326)

Aggregates `Annealage PodInfra`, `Annealage PodDut`, `Annealage PodDebugprobe`. Public surface
that is RP_INFRA-related:

| Member | Source | Description |
|---|---|---|
| `annealage_pod.infra` | lib_annealage_pod.py:133-136 | A `Annealage PodInfra`. |
| `annealage_pod.switches` | lib_annealage_pod.py:206-207 | Shortcut to `annealage_pod.infra.switches`; a `Annealage PodInfraSwitches`. |
| `annealage_pod.power_dut_off_and_wait()` | lib_annealage_pod.py:217-221 | Closes DUT mp_remote, switches DUT off, waits. |
| `annealage_pod.set_relays_by_FUT(fut, open_others=False)` | lib_annealage_pod.py:265-280 | Looks up `annealage_pod_spec.relays_closed[fut]` and applies. Used by the test runner before each test. |
| `annealage_pod.dut_boot_and_init_mp_remote(udev)` | lib_annealage_pod.py:282-286 | Powers DUT, waits for udev, opens MpRemote on the DUT tty. |
| `annealage_pod.verify_hw_version()` | lib_annealage_pod.py:319-325 | Reads `gpio_hw_version` and warns if it disagrees with inventory. |
| `annealage_pod.active_led_on` (contextmanager) | lib_annealage_pod.py:257-263 | Sets `LED_ACTIVE` True for the duration of the block. |

### B.3.2 octoprobe.lib_annealage_pod_infra.Annealage PodInfra (lib_annealage_pod_infra.py:31-267)

| Member | Source | Description |
|---|---|---|
| `infra.label` | lib_annealage_pod_infra.py:51 | str. |
| `infra.usb_annealage_pod` | lib_annealage_pod_infra.py:52 | A `UsbAnnealage Pod`. The 4-port hub abstraction. |
| `infra.mcu_infra` | lib_annealage_pod_infra.py:54 | An `InfraPico`. The on-Pico symbol-table proxy. |
| `infra.switches` | lib_annealage_pod_infra.py:55 | A `Annealage PodInfraSwitches`. The 14-entry switch dict. |
| `infra.list_all_relays` | lib_annealage_pod_infra.py:71-73 | `[1,2,3,4,5,6,7]`. |
| `infra.usb_location_infra` / `_probe` / `_dut` | lib_annealage_pod_infra.py:75-85 | Sysfs USB path strings. |
| `infra.mp_remote` | lib_annealage_pod_infra.py:98-101 | The `MpRemote` for the RP_INFRA Pico. |
| `infra.mp_remote_close()` | lib_annealage_pod_infra.py:87-96 | Closes the serial port; returns the tty path. |
| `infra.connect_mpremote_if_needed()` | lib_annealage_pod_infra.py:127-151 | Opens the MpRemote if not already open, or reopens after a power-cycle (detected via `changed_counter`). |
| `infra.load_base_code_if_needed()` | lib_annealage_pod_infra.py:123-125 | Connects MpRemote and pushes the Jinja2 base code if the Pico was power-cycled. |
| `infra.power_dut_off_and_wait()` | lib_annealage_pod_infra.py:107-114 | `switches[DUT].set(on=False)`, then sleep 0.5 s if state changed. |
| `infra.setup_infra(udev)` | lib_annealage_pod_infra.py:153-165 | Loads base code, verifies MicroPython version, re-flashes if mismatch. |
| `infra.flash(udev, filename_firmware, usb_location, directory_test)` | lib_annealage_pod_infra.py:202-261 | Picotool-based reflash of RP_INFRA. Drives BOOTSEL via `infraboot` then power-cycles via the upstream hub. |
| `infra.borrow_tty()` (contextmanager) | lib_annealage_pod_infra.py:263-267 | Yields the tty after closing MpRemote, then re-opens. |
| `infra.get_firmware_spec()` (static) | lib_annealage_pod_infra.py:39-41 | Loads the JSON. |
| `infra.verify_micropython_version(spec)` | lib_annealage_pod_infra.py:167-179 | Compares `sys.version + ',' + sys.implementation[2]` against the pinned string. |
| `infra.pico_test_mp_remote()` | lib_annealage_pod_infra.py:116-121 | Asserts unique_id matches and version matches. |

### B.3.3 octoprobe.lib_annealage_pod_infra.Annealage PodInfraSwitches (lib_annealage_pod_infra.py:459-611)

A dict-like object keyed by `Switch`. Each entry exposes `.set(on)`, `.get()`,
`.changed_counter`. There are also property descriptors for direct attribute
access:

| Property | Switch | Behavior |
|---|---|---|
| `switches.infra` | `PICO_INFRA` | USB hub port 1 power. Delegated to `UsbAnnealage PodSwitch` (off-annealage_pod). |
| `switches.infraboot` | `PICO_INFRA_BOOT` | USB hub port 2 power, drives BOOTSEL. Delegated. |
| `switches.proberun` | `PICO_PROBE_RUN` | RP_PROBE reset release. Implemented as `pin_PICO_PROBE_RUN.value(on)` via REPL. |
| `switches.probeboot` | `PICO_PROBE_BOOT` | RP_PROBE BOOTSEL (False = pressed). REPL-driven. |
| `switches.dut` | `DUT` | DUT power. v0.3: USB hub port 3 power. v0.5+: REPL-driven `pin_DUT`. Has DUT_POWER_OFF_TIME_MIN_S = 2.0 enforcement (lib_annealage_pod_infra.py:325-338). |
| `switches.led_error` | `LED_ERROR` | v0.3: USB hub port 4. v0.5+: REPL-driven `pin_LED_ERROR`. |
| `switches.led_active` | `LED_ACTIVE` | REPL-driven `pin_LED_ACTIVE`. |
| `switches.relay1` .. `switches.relay7` | `RELAY1..7` | REPL-driven `pin_RELAYn`. Reading uses `bool(pin.value())`; writing uses `set_relays(...)` so the firmware can collapse multiple relay updates into one round trip. |

Methods on `Annealage PodInfraSwitches` itself:

| Method | Source | Description |
|---|---|---|
| `relays(relays_close=None, relays_open=None) -> bool` | lib_annealage_pod_infra.py:541-550 | Batch update; returns True iff at least one relay changed. Wraps `set_relays(...)` on-Pico. |
| `default_off()` | lib_annealage_pod_infra.py:552-558 | Powers everything off including PICO_INFRA. |
| `default_off_infra_on()` | lib_annealage_pod_infra.py:560-567 | Powers everything off except PICO_INFRA; sets `probeboot=True, proberun=False, dut=False, led_error=False`. |
| `powercycle(power_cycle: TyperPowerCycle)` | lib_annealage_pod_infra.py:569-611 | Six modes: INFRA, INFRABOOT, PROBE, PROBEBOOT, DUT, OFF. Each is a fixed sequence of switch ops with sleeps. |
| `delay_set_dut_on(on)` | lib_annealage_pod_infra.py:523-539 | Enforces minimum DUT off time. |

### B.3.4 octoprobe.lib_annealage_pod_infra_pico.InfraPico (lib_annealage_pod_infra_pico.py:14-220)

Type-safe wrapper around the on-Pico symbol table.

| Member | Source | Description |
|---|---|---|
| `mcu_infra.unique_id` | lib_annealage_pod_infra_pico.py:150-155 | str, hex of `machine.unique_id()`. |
| `mcu_infra.gpio_hw_version` | lib_annealage_pod_infra_pico.py:129-134 | int 0..3. |
| `mcu_infra.hw_version` | lib_annealage_pod_infra_pico.py:136-148 | One of `HwVersion.V03..V07` (StrEnum). |
| `mcu_infra.get_micropython_version()` | lib_annealage_pod_infra_pico.py:157-161 | Returns `sys.version + ',' + sys.implementation[2]`. |
| `mcu_infra.exception_if_files_on_flash()` | lib_annealage_pod_infra_pico.py:163-172 | Raises if any file is in `os.listdir()`. Used by commissioning. |
| `mcu_infra.is_base_code_loaded(will_load=False)` | lib_annealage_pod_infra_pico.py:95-109 | Compares `Switch.PICO_INFRA.changed_counter` against the last-load counter. |
| `mcu_infra.assert_base_code_loaded()` | lib_annealage_pod_infra_pico.py:111-113 | Raises if not loaded. |
| `mcu_infra.load_base_code()` | lib_annealage_pod_infra_pico.py:115-127 | If the Pico was power-cycled, renders the Jinja2 template and `exec_raw`'s it; reads `pico_unique_id` and `gpio_hw_version`. |
| `mcu_infra.base_code_lost()` | lib_annealage_pod_infra_pico.py:92-93 | Resets the loaded counter to -1. |
| `mcu_infra.relays_ctx(description, relays_close=None, relays_open=None)` (contextmanager) | lib_annealage_pod_infra_pico.py:174-195 | Closes/opens relays on enter, inverts on exit. Important: leaves relays in the inverted state, not the prior state. |
| `mcu_infra.relays_pulse(relays: int, initial_closed: bool, durations_ms: list[int]) -> None` | lib_annealage_pod_infra_pico.py:197-220 | Calls on-Pico `set_relays_pulse`; timeout on the host side is `1.5 * 1000 * sum(durations_ms)`. |

### B.3.5 octoprobe.lib_mpremote.MpRemote (lib_mpremote.py:114-481)

The transport adapter the new annealage_pod must mimic. This is the layer that
Octoprobe will need to be sub-classed (or its `SerialTransport` swapped) to
talk over TCP REPL instead of `/dev/ttyACM*`.

Public surface used by RP_INFRA:

| Member | Source | Used by |
|---|---|---|
| `MpRemote(tty, label, baudrate=115200, wait_s=5, timeout_s=2.0)` | lib_mpremote.py:124-140 | `Annealage PodInfra.connect_mpremote_if_needed`. |
| `close()` | lib_mpremote.py:157-168 | Closes the underlying `mpremote.transport_serial.SerialTransport`. |
| `borrow_tty()` (contextmanager) | lib_mpremote.py:170-183 | Yields the tty for direct subprocess use, then re-attaches. |
| `set_rtc(now=None)` | lib_mpremote.py:185-201 | DUT only. |
| `cp(src, dest, multiple=True)` | lib_mpremote.py:203-213 | Wraps `mpremote.commands.do_filesystem_cp`. DUT only. |
| `file_equal(src, dest)` | lib_mpremote.py:215-233 | sha256 against `transport.fs_hashfile`. DUT only. |
| `mip_install_package(package)` | lib_mpremote.py:235-269 | DUT only. |
| `exec_render(code, follow=True, **kwargs)` | lib_mpremote.py:271-279 | Jinja2-render then exec_raw. |
| `exec_file(filename, follow=True, timeout=2, soft_reset=None)` | lib_mpremote.py:281-297 | Read file then exec_raw. |
| `exec_file_result(...)` | lib_mpremote.py:299-316 | Like exec_file but with `[RESULT]` extraction. |
| `exec_raw2(cmd, follow, timeout, soft_reset)` | lib_mpremote.py:318-367 | The lowest-level call: `transport.exec_raw_no_follow(cmd)` then `transport.follow(timeout)`. |
| `exec_raw(cmd, check_result=False, follow=True, timeout=2, soft_reset=None)` | lib_mpremote.py:369-390 | The standard call. With check_result=True, parses `[RESULT]` and `[ERROR]` markers. |
| `eval_expression(expr, check_result, timeout=2)` | lib_mpremote.py:421-439 | Wraps the expression as `_v=repr(expr); print('[RESULT]'+_v)` and `eval()`s the result. |
| `read_None / read_bool / read_int / read_float / read_str / read_bytes / read_list` | lib_mpremote.py:441-480 | Type-checked sugar over `eval_expression(check_result=True)`. |

### B.3.6 octoprobe.lib_annealage_pod_dut.Annealage PodDut (lib_annealage_pod_dut.py:28-275)

DUT side, reachable from MicroPython tests via `annealage_pod.dut`. Not RP_INFRA but
on the wire it goes to the DUT's own MicroPython REPL. Listed here because the
new annealage_pod must keep this MpRemote-on-DUT path working, and because in the
new design the DUT REPL is exposed only through the USB-host -> USB/IP path
(not directly).

| Member | Source | Description |
|---|---|---|
| `dut.mp_remote` | lib_annealage_pod_dut.py:60-65 | The `MpRemote` for the DUT. |
| `dut.mp_remote_is_initialized` | lib_annealage_pod_dut.py:67-69 | bool. |
| `dut.get_tty()` | lib_annealage_pod_dut.py:71-79 | Returns the tty after closing MpRemote. |
| `dut.mp_remote_close()` | lib_annealage_pod_dut.py:81-93 | Closes silently, swallowing OSError. |
| `dut.boot_and_init_mp_remote_dut(annealage_pod, udev)` | lib_annealage_pod_dut.py:95-109 | Powers up DUT, opens MpRemote. |
| `dut.dut_installed_firmware_full_version_text()` | lib_annealage_pod_dut.py:111-135 | Reads `sys.version`, `sys.implementation[2]`, optionally `sys.implementation._build`, joined with `;`. |
| `dut.is_dut_required_firmware_already_installed(firmware_spec, exception_text=None)` | lib_annealage_pod_dut.py:137-160 | True iff strings match. |
| `dut.flash_dut(annealage_pod, udev, directory_logs, firmware_spec)` | lib_annealage_pod_dut.py:170-246 | Dispatches to the configured DutProgrammer. |
| `dut.dut_power_cycle(udev)` | lib_annealage_pod_dut.py:248-250 | Off then on; reopens MpRemote. |
| `dut.mpremote_success(cmd)` | lib_annealage_pod_dut.py:265-275 | True iff `exec_raw(cmd)` does not raise. |

### B.3.7 octoprobe.lib_annealage_pod_debugprobe.Annealage PodDebugprobe (lib_annealage_pod_debugprobe.py:15-74)

Trivial: only powers on PICO_PROBE and records the resulting tty via udev.

| Member | Source | Description |
|---|---|---|
| `probe.tty` | lib_annealage_pod_debugprobe.py:38-41 | str, set after `power_on`. |
| `probe.power_on(udev)` | lib_annealage_pod_debugprobe.py:43-74 | Sets `Switch.PICO_PROBE_RUN=True`, waits for udev `add` of vendor 0x2E8A product 0x000C, records tty. |

## B.4 Wire protocol summary

There is no custom RPC. Every host-to-RP_INFRA call is one of:

1. `mpremote.transport_serial.SerialTransport.exec_raw_no_follow(cmd)` over a
   USB-CDC ACM serial line at 115200 8N1, talking to the Pico's stock raw REPL
   (Ctrl-A, paste, Ctrl-D).
2. `transport.follow(timeout)` to capture the printed output.

State machine: the host opens the serial port (`SerialTransport(tty, baudrate,
wait, timeout)`, lib_mpremote.py:142-149), enters raw REPL on first call
(`state.ensure_raw_repl(soft_reset=...)` triggered inside `exec_raw2`), then
issues a `Ctrl-A; cmd; Ctrl-D` sequence per call. Output is a `bytes` blob.
The follow timeout default is 2 s for top-level callers and 2 s for
`exec_raw`.

Result framing for value-returning calls: the host wraps the user expression
into `_v=repr(<expr>); print('[RESULT]' + _v)` (lib_mpremote.py:431). It then
splits the captured stdout on the literal `[RESULT]` tag, expects exactly two
parts, splits the prefix on `[ERROR]` (lib_mpremote.py:392-419), and `eval()`s
the suffix. So the wire framing is: ASCII output from the program, then a
single occurrence of the literal `[RESULT]`, then a Python `repr()` of the
result on the same logical print line. There is no length prefix and no
checksum.

Error framing: a runtime exception in MicroPython is caught at the
`transport.follow` level: `ret_err` non-empty triggers `ExceptionCmdFailed`
(lib_mpremote.py:342-356). Application-level errors are signalled by the
on-Pico code calling `print('[ERROR]' + repr(reason))` before printing the
`[RESULT]`; the host raises `ExceptionCmdError` (lib_mpremote.py:411-415).

State assumptions:

- Each Pico power cycle invalidates the loaded base code. The host detects
  this via `UsbAnnealage PodSwitch.changed_counter` (usb_annealage_pod.py:388-417), an
  integer that the upstream-hub power switch increments on every power
  transition. The new annealage_pod must offer an equivalent monotonically
  increasing power-cycle counter; the natural fit is per-rail boot-id derived
  from RTC or a free-running counter incremented by the cleanup hook in spec
  section 5.4.
- The host does not assume any persistent state on the Pico. After every
  power cycle it re-pushes the Jinja2-rendered base code via `exec_raw` on
  the next call.
- Default exec timeout: 2 s. Special timeout for `relays_pulse`:
  `1.5 * 1000 * sum(durations_ms)` ms (lib_annealage_pod_infra_pico.py:218-220).
- The flash workflow uses `borrow_tty()` to release the serial port for an
  external picotool invocation, then re-enters raw REPL.

## B.5 Mapping table: RP_INFRA -> spec.md section 7

Classification key:
- exact: signature is identical, no adapter needed
- semantic: same behavior, signature differs, adapter rewrites
- new: not present in RP_INFRA, added in spec.md section 7
- gap: present in RP_INFRA, not covered by spec.md section 7 (must be added)

| RP_INFRA host call | On-Pico equivalent | spec.md section 7 mapping | Class |
|---|---|---|---|
| `annealage_pod.infra.switches.relay1..relay7 = bool` | `set_relays([(n, bool)])` | not specified | gap (relay control is implicit in section 3.4 reset paths but no API name given) |
| `annealage_pod.infra.switches.relays(relays_close=[...], relays_open=[...])` | `set_relays([...])` | not specified | gap (batched relay update missing) |
| `annealage_pod.infra.switches[Switch.RELAYn].get()` | `bool(pin_RELAYn.value())` | not specified | gap |
| `annealage_pod.infra.mcu_infra.relays_ctx(desc, close=[...], open=[...])` | inverted-state context manager | not specified | gap |
| `annealage_pod.infra.mcu_infra.relays_pulse(n, initial_closed, durations_ms)` | `set_relays_pulse(...)` | not specified | gap (NRF/SAMD double-tap, BOOTSEL pulse train) |
| `annealage_pod.infra.switches.dut = bool` (with 2 s minimum off enforcement) | `pin_DUT.value(on)` (v0.5+) | `annealage_pod.dut.reset(mode='power')` covers cycle, but no on/off primitive | gap (need a `annealage_pod.power.dut.set(on)` or similar) |
| `annealage_pod.infra.switches.led_active = bool` | `pin_LED_ACTIVE.value(on)` | not specified | gap (status LEDs not in section 7) |
| `annealage_pod.infra.switches.led_error = bool` | `pin_LED_ERROR.value(on)` (v0.5+) | not specified | gap |
| `annealage_pod.infra.switches.proberun = bool` | `pin_PICO_PROBE_RUN.value(on)` | n/a | new (RP_PROBE is replaced by synthetic CMSIS-DAP-v2; this method has no equivalent and must be a no-op or raise) |
| `annealage_pod.infra.switches.probeboot = bool` | `pin_PICO_PROBE_BOOT.value(on)` | n/a | new (same; no real RP_PROBE) |
| `annealage_pod.infra.switches.infra = bool` | upstream USB hub port 1 power | n/a | gap-not-applicable (the new annealage_pod has no separate INFRA Pico to power; should be a no-op that returns False / not-changed and never raises) |
| `annealage_pod.infra.switches.infraboot = bool` | upstream USB hub port 2 power | n/a | same as `infra` (no-op) |
| `annealage_pod.infra.switches.default_off()` | sequence | not specified | gap (test cleanup hook calls this; spec section 5.4 mentions a hook but not the API name) |
| `annealage_pod.infra.switches.default_off_infra_on()` | sequence | not specified | gap |
| `annealage_pod.infra.switches.powercycle(TyperPowerCycle.{INFRA,INFRABOOT,PROBE,PROBEBOOT,DUT,OFF})` | sequences with sleeps | partial; `annealage_pod.dut.reset(mode='power')` covers DUT only | gap (needed by `op` CLI; even if the new annealage_pod reduces to just DUT and OFF, the API name should exist for compat) |
| `annealage_pod.infra.mcu_infra.unique_id` | `pico_unique_id` | not specified | gap (must map to the ESP32-S3 chip ID or a stable equivalent so annealage_pods_inventory.py keeps matching) |
| `annealage_pod.infra.mcu_infra.gpio_hw_version` | sampled GPIOs 25..29 | n/a | new (carrier ID via I2C EEPROM per spec section 3.6 supersedes this; need a compat shim that returns a fixed value mapped from the new carrier-id) |
| `annealage_pod.infra.mcu_infra.hw_version` | derived | n/a | new (same; map to a string the test runner accepts) |
| `annealage_pod.infra.mcu_infra.get_micropython_version()` | `sys.version + ',' + sys.implementation[2]` | not specified | gap (testbed compares to a pinned RPI_PICO string; new annealage_pod reports its own ESP32-S3 string, so the version-check logic must be relaxed or re-pinned. See B.7) |
| `annealage_pod.infra.mcu_infra.exception_if_files_on_flash()` | `len(os.listdir())` | not specified | gap (commissioning only; cheap to add) |
| `annealage_pod.infra.mcu_infra.is_base_code_loaded(will_load)` | counter compare on host | not specified | semantic (base-code reload is a host concept; the new design loads MP modules from flash so this can be a stub that always returns True) |
| `annealage_pod.infra.mcu_infra.assert_base_code_loaded()` | raise if not | not specified | semantic (stub) |
| `annealage_pod.infra.mcu_infra.load_base_code()` | `exec_raw` of Jinja blob | not specified | semantic (stub: loads happen at boot from frozen modules) |
| `annealage_pod.infra.mcu_infra.base_code_lost()` | counter reset | not specified | semantic (stub) |
| `annealage_pod.infra.flash(udev, filename, usb_location, directory_test)` | picotool over USB | n/a | new (the new annealage_pod is OTA-flashed via `annealage_pod_ota.update(url)` from spec section 4.3; testbed_micropython does not call this directly in the test path, only `op` CLI) |
| `annealage_pod.infra.power_dut_off_and_wait()` | DUT off + 0.5 s sleep | partially in section 7.2 via `annealage_pod.dut.reset(mode='power')` | gap (need an explicit "off-only" primitive that does not turn DUT back on) |
| `annealage_pod.infra.usb_location_infra/_probe/_dut` | sysfs paths | n/a | new (no sysfs on the host side for an ESP32-S3 over Wi-Fi; the adapter must synthesise these or testbed_micropython code that reads them must be patched) |
| `annealage_pod.infra.borrow_tty()` (contextmanager) | release-and-reattach serial | n/a | new (the new design's REPL is TCP, not tty; subprocess flashers cannot share it the same way) |
| `annealage_pod.infra.mp_remote` | MpRemote | n/a | new (no direct MpRemote against RP_INFRA in the new design; replaced by the `annealage_pod.*` Python API on the ESP32-S3 itself) |
| `annealage_pod.set_relays_by_FUT(fut, open_others=False)` | indirect via switches.relays | not specified | gap (test runner uses this every test) |
| `annealage_pod.power_dut_off_and_wait()` | infra delegate | partial | gap (same as infra version) |
| `annealage_pod.active_led_on` (contextmanager) | LED_ACTIVE on/off | not specified | gap |
| `annealage_pod.verify_hw_version()` | hw_version compare | not specified | gap |
| `annealage_pod.dut.flash_dut(...)` | picotool / dfu-util / esptool / bossac / teensy_loader_cli | n/a | new (host side; in the new design the DUT is reached over USB/IP so the same flashers work but against the USB/IP virtual device; verify each flasher tolerates that) |
| `annealage_pod.dut.boot_and_init_mp_remote_dut(udev)` | host udev wait | n/a | new (host side; in the new design the host stack waits for USB/IP attach, then opens the DUT REPL via the locally-attached USB-CDC) |
| `annealage_pod.dut.get_tty()` | returns tty path | n/a | new (host side; in the new design returns the locally-attached tty after USB/IP attach) |
| `annealage_pod.dut.mp_remote.exec_raw / cp / set_rtc / mip_install_package / read_*` | DUT REPL | n/a | new-passthrough (against the DUT, not the annealage_pod; works unchanged after USB/IP attach) |
| Section 7.2 `annealage_pod.power.vtarget.current_mA / voltage_mV` | n/a | new in spec | new |
| Section 7.2 `annealage_pod.power.dut_usb.current_mA / voltage_mV / vbus_present` | n/a | new in spec | new |
| Section 7.2 `annealage_pod.dut.reset(mode='swd'\|'nrst'\|'power'\|'relay', relay=N)` | only `power` and `relay` modes have any RP_INFRA equivalent (DUT power cycle, relay drive); `swd` and `nrst` have no RP_INFRA path | new in spec | new |
| Section 7.2 `annealage_pod.slave.i2c.* / .spi.*` | n/a | new in spec | new |
| Section 7.2 `annealage_pod.swo.bytes_buffered() / overruns_total()` | n/a | new in spec | new |
| Section 7.2 `annealage_pod.carrier.id() / firmware.version()` | partial overlap with `mcu_infra.unique_id` and `get_micropython_version` | new in spec | new |

## B.6 RP_PROBE host-side dependencies the new probe must honor

testbed_micropython does not invoke pyOCD, OpenOCD, or probe-rs directly. The
only DUT programmer in the codebase that uses a debug-probe path is
`DutProgrammerDebugprobe` (util_mcu_debugprobe.py:39-56), and its `flash`
method is `raise NotImplementedError()`. The corresponding annealage_pod-spec entry
(NRF52840_USB_DONGLE, annealage_pod_specs.py:149) explicitly says "This dongle uses
SWD from PICO_PROBE for programming. This has not been implemented yet".

Practical consequence: the new synthetic CMSIS-DAP-v2 probe has no existing
contract from testbed_micropython to honor; testbed_micropython does not flash
DUTs over SWD today, only via DFU/picotool/esptool/bossac. Any future SWD
flash path goes through the synthetic CMSIS-DAP-v2 over USB/IP and is greenfield.

What the new probe does need to be:

1. A USB device with vendor/product matching either the Raspberry Pi
   Debugprobe (0x2E8A:0x000C, util_mcu_debugprobe.py:23-26) or one of the
   stock CMSIS-DAP-v2 IDs that pyOCD/OpenOCD/probe-rs auto-detect.
2. Exposed via `usbip attach` so that on the host, post-attach, the probe
   appears as a normal local USB device.
3. The host-side Annealage PodDebugprobe.power_on (lib_annealage_pod_debugprobe.py:43-74)
   waits for the udev `add` of the configured vendor/product on a specific
   USB location. In the new design the "USB location" of the synthetic probe
   is whatever the local Linux kernel assigns post-`usbip attach`. The
   adapter must either match by vendor/product alone (drop the
   `usb_location` filter) or synthesise a stable usb_location string.

## B.7 Gaps in spec.md section 7 to be added

The mapping table flags 19 RP_INFRA entries marked `gap`. Consolidated:

1. **Relay control surface**: `annealage_pod.relay[1..7].set(on)`, `.get()`,
   `.pulse(initial_closed, durations_ms)`, `.batch(close=[...], open=[...])`.
   These are core RP_INFRA functions used by every test fixture. Section 7.2
   only mentions relays in passing under `annealage_pod.dut.reset(mode='relay',
   relay=N)`, which is not enough; the test runner directly toggles relays
   for FUT routing (lib_annealage_pod.py:265-280) and for double-tap reset
   sequences (util_mcu_nrf.py:52, util_mcu_samd.py:90).
2. **DUT power on/off (without auto-cycle)**: spec only has cycle via
   `annealage_pod.dut.reset(mode='power')`. RP_INFRA exposes raw `switches.dut =
   bool` plus the 2 s minimum-off-time enforcement. Need
   `annealage_pod.power.dut.set(on)` or similar with the same minimum-off
   guarantee.
3. **Status LEDs**: `annealage_pod.led.active.set(on)`, `annealage_pod.led.error.set(on)`.
4. **Active-LED contextmanager**: `annealage_pod.led.active_on()` (used per test).
5. **Carrier identification readback**: `annealage_pod.carrier.id()` is in section
   7.2; need to confirm it returns a string compatible with the existing
   `annealage_pods_inventory.py` matching logic, which today uses `unique_id`
   (the Pico chip ID). Either return chip-id-like, or update the inventory
   schema to take a separate carrier-id field.
6. **Firmware version readback**: `annealage_pod.firmware.version()` is in section
   7.2 but the test runner today compares against
   `infra.mcu_infra.get_micropython_version()` (a Pico-format string). The
   compat layer will return the ESP32 string; testbed must be patched to
   either skip the check or compare against a new pinned string for ESP32
   builds.
7. **Power-cycle counter**: required by the host so it can detect that the
   annealage_pod was rebooted between calls (today it's
   `UsbAnnealage PodSwitch.changed_counter`). Propose `annealage_pod.boot_id()`
   returning a monotonically increasing int across reboots.
8. **Cleanup hook API**: section 5.4 mentions the hook conceptually; spec
   section 7 should expose `annealage_pod.on_disconnect = callback` or similar so
   tests can override the default DUT-off behavior.
9. **Compatibility shims for RP_PROBE switches**: `proberun`, `probeboot`,
   `infra`, `infraboot`. Decision needed: silently no-op or raise
   `NotImplementedError`. Recommend silent no-op returning False (no
   change) so that `op` CLI continues to work without exception spam, and
   add `annealage_pod.compat.set_strict(True)` for callers that want hard
   failures.
10. **Files-on-flash assertion**: `annealage_pod.fs.files_count()` for the
    commissioning script.
11. **`borrow_tty` semantics**: The new annealage_pod's REPL is TCP. Tests that
    call `borrow_tty()` to hand the serial port to a subprocess flasher have
    no equivalent. The fix is host-side: the testbed adapter must detect
    these call sites and route around them (the DUT tty is over USB/IP,
    obtained from the host's local enumeration, not from the annealage_pod).
12. **`exception_if_files_on_flash()` and `verify_micropython_version`**:
    used during `op query`. Either expose equivalents or document that the
    `op` tooling targets RP_INFRA only and is replaced by a new CLI for the
    ESP32-S3 annealage_pod.

Latent issue: on-Pico `get_relays(relays)` at lib_annealage_pod_infra_pico.py:59-61
declares the parameter as `relays` but indexes with `i`; Python sees `i` as
unbound. The new firmware should fix this rather than copy it bug-for-bug.

## B.8 Open questions

1. **Identity strategy**: should `annealage_pod.carrier.id()` return the ESP32-S3
   chip ID, the carrier EEPROM/strap-resistor value, or both? The existing
   `annealage_pods_inventory.py` matches by `unique_id` of the Pico, which on the
   new hardware does not exist. Cleanest option is two distinct fields:
   `annealage_pod_id()` (S3 chip-id, immutable across re-flashes) and
   `carrier_id()` (EEPROM/strap, immutable across S3 module swaps). Update
   the inventory schema accordingly.
2. **MicroPython version pin**: testbed_micropython today pins to
   `RPI_PICO-...` text. Decide whether the compat layer fakes the Pico
   string (bad: lies) or the test harness gets a per-annealage_pod version
   tolerance map.
3. **mpremote transport for TCP REPL**: is the cleanest path to subclass
   `mpremote.transport_serial.SerialTransport` to wrap a `socket.socket`, or
   to upstream a `TransportTCP` to mpremote? `MpRemote.__init__` takes a tty
   path (lib_mpremote.py:124-149); a parallel `MpRemote.from_socket(host,
   port, label)` constructor and a `TransportTCP` is the smallest
   change-set. This decision is host-side, not on-annealage_pod.
4. **Power-cycle counter source**: in the new design the annealage_pod does not
   power-cycle itself between tests (it stays up). The current
   `changed_counter` semantics are tied to USB-hub-port power transitions.
   In the compat layer, `boot_id` should increment only across actual S3
   reboots; per-DUT power cycles need a separate counter
   (`annealage_pod.power.dut.cycle_count()`).
5. **Relay numbering**: spec sections 3.4 and 7.2 use `relay=N`; RP_INFRA
   uses 1..7 throughout. Confirm 1-based indexing is preserved in the new
   API. Pin labels on the carrier PCB are 1..7.
6. **`borrow_tty` host-side**: the DUT's tty in the new design is the
   locally-mounted USB/IP device, not anything on the annealage_pod. Confirm
   that octoprobe's flash subprocesses (picotool, dfu-util, esptool,
   bossac, teensy_loader_cli) all work against the USB/IP-mounted DUT
   without modification. teensy_loader_cli is HID, dfu-util is USB control
   transfer, both should pass through cleanly; esptool resets via DTR/RTS
   on the CDC, must verify the USB/IP forwarding propagates DTR/RTS.
7. **Transport adapter ownership**: spec section 7.1 says testbed needs only
   a transport adapter. Where does that adapter live: in
   octoprobe/lib_mpremote.py as a new transport, in testbed_micropython as
   a monkey-patch, or in a new mpy-pod host-side companion package? The
   third is cleanest.
