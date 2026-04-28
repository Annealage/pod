# Ops C-shim design notes

Companion to `spec.md` §4.3 (OTA), §6.1 (logging), §6.2 (watchdog), §6.3
(time sync) and `plan/phase-2-parallel-implementation.md` WS-H. Records
the decisions for the three C user modules under `src/c_modules/ops_*/`
and how they compose with the pure-MicroPython wrappers under
`src/mpy/annealage_pod/ops/`.

## 1. Scope

WS-H lands four MP-side ops modules:

- `annealage_pod.ops.ota`: HTTPS OTA via the C shim `ops_ota`, wrapping
  `esp_https_ota`.
- `annealage_pod.ops.wdt`: task watchdog subscription via `ops_wdt`,
  wrapping `esp_task_wdt`.
- `annealage_pod.ops.log`: TCP log fan-out via `ops_log`, wrapping
  `esp_log_set_vprintf`.
- `annealage_pod.ops.time`: pure-MicroPython NTP sync via `ntptime`. No
  C shim.

Each MP-side wrapper imports the C shim if present, otherwise falls
back to a pure-MP path. Behaviour is identical from the test's
perspective for the cases tested on Unix (import succeeds, fallback
selected, public API stable).

## 2. C shim module layout

```
src/c_modules/
  ops_ota/
    micropython.cmake     - INTERFACE lib + IDF_COMPONENTS append
    ops_ota.h             - C public surface
    ops_ota.c             - esp_https_ota wrapper
    modops_ota.c          - MP module binding
  ops_wdt/
    micropython.cmake
    ops_wdt.h
    ops_wdt.c             - esp_task_wdt wrapper
    modops_wdt.c
  ops_log/
    micropython.cmake
    ops_log.h
    ops_log.c             - esp_log_set_vprintf + TCP fan-out
    modops_log.c
```

`src/c_modules/micropython.cmake` includes each module's CMake fragment
(append-only edit; existing modules untouched). Each fragment declares
an `INTERFACE` library, lists its sources/include directories, and
links itself against the `usermod` aggregate target. Modules that need
IDF components beyond the MicroPython esp32 port's default
`IDF_COMPONENTS` set append to that list before `idf_component_register`
runs (only `ops_ota` does this; the others fit inside `esp_system` and
`log`).

## 3. OTA: `ops_ota`

### 3.1 Partition handoff

`esp_https_ota()` opens an HTTPS connection to the supplied URL,
streams the image into the inactive OTA slot (selected automatically
from `otadata`), validates the image header and chip ID, and finally
calls `esp_ota_set_boot_partition()` to switch the next boot to that
slot. The shim does not call `esp_restart()`; the MP boot/REPL
caller decides when to reboot.

Flow at a successful update:

```
boot           caller (REPL or boot.py)        ops_ota                       IDF
 |                  |                             |                            |
 |                  | ops_ota.update(url)         |                            |
 |                  | ------------------------->  |                            |
 |                  |                             | esp_https_ota()            |
 |                  |                             | ------------------------>  |
 |                  |                             |                            | https connect
 |                  |                             |                            | stream into OTA slot
 |                  |                             |                            | ota_set_boot_partition()
 |                  |                             | <------------------------- |
 |                  | <------- ESP_OK ----------- |                            |
 |                  | machine.reset()                                          |
 | <- bootloader -- | (boots from new slot, marked pending-verify)             |
 | (boot.py) ops_ota.mark_app_valid() once Wi-Fi up                            |
```

### 3.2 Bootloader rollback contract

`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` is set in the board's
`sdkconfig.board`. After the bootloader boots a freshly-OTA-installed
slot, it marks that slot **pending-verify**. The application has
**one boot** to confirm itself by calling
`esp_ota_mark_app_valid_cancel_rollback()`. If it does not, the
bootloader rolls back to the previous slot on the next reboot.

The shim exposes this via `ops_ota.mark_app_valid()`. The contract
for `boot.py` is:

1. Boot, bring up Wi-Fi.
2. Start critical services (REPL, USB/IP, mDNS).
3. Call `annealage_pod.ops.ota.mark_valid()` (which calls the shim).
4. Enter the asyncio main loop.

If steps 1-3 hang or panic, the watchdog or RTC-WDT reboots; the
bootloader sees the slot is still pending-verify and rolls back.

### 3.3 Synchronous semantics

`esp_https_ota()` is blocking. The MP main task is the typical
caller (triggered remotely over the TCP REPL socket per spec
§4.3). Long-running C tasks must not invoke `ops_ota.update`; the
download keeps the MP main task off the asyncio loop for many
seconds, which is acceptable for an explicit OTA but not for any
implicit call.

Errors propagate as `OSError(esp_err_t)`. The IDF returns
`ESP_FAIL` on generic HTTP failure, `ESP_ERR_INVALID_ARG` on a
malformed URL, `ESP_ERR_OTA_VALIDATE_FAILED` on a corrupt image,
and `ESP_ERR_HTTPS_OTA_*` for the dedicated OTA error subset. The
shim does not translate these; the MP caller can compare against
the IDF constants if it wants finer reporting.

### 3.4 IDF v5.5.1 API check

`esp_https_ota.h` in v5.5.1 ships the same `esp_https_ota_config_t`
shape used here (`http_config` pointer + optional partition-detail
override block). No API drift relative to v5.x; the wrapper should
recompile against future v5.x point releases without change. The
shim does **not** use the optional decrypt callback or the partial
HTTP download path.

## 4. Watchdog: `ops_wdt`

### 4.1 Subscription model

The TWDT itself is initialised by IDF at boot when
`CONFIG_ESP_TASK_WDT_INIT=y` (set in the board's `sdkconfig.board`,
30 s timeout per spec §6.2). The shim's `subscribe()` only calls
`esp_task_wdt_add(NULL)` to register the calling task. The MP main
task is the typical caller; long-running C-side tasks (USB/IP
accept, UART bridge, SWO drain, TinyUSB host) call
`esp_task_wdt_add()` directly from C and reset from their own
loops.

`subscribe()` accepts a `timeout_ms` argument that, if it differs
from the currently-applied value, reconfigures the TWDT via
`esp_task_wdt_reconfigure()`. This is a global setting; reconfiguring
from one subscriber affects all of them. The MP main task is the
canonical owner of the timeout; C-side callers should leave
`timeout_ms` at 0 (= keep current).

### 4.2 Kick semantics

Every subscriber must call `esp_task_wdt_reset()` (= `kick()`)
within the timeout window. The MP asyncio loop ticks at hundreds of
Hz, so a single kick per loop iteration is conservative; the
reference rate is once per second. Failure to kick within the
timeout panics the chip (the IDF default; spec §6.2 wants this for
the bootloader-rollback safety net).

### 4.3 Pure-MP fallback

`annealage_pod.ops.wdt` falls back to `machine.WDT` when the C shim is
absent (Unix port, dev boards). On the esp32 port `machine.WDT`
also drives `esp_task_wdt`, so the fallback is functionally
equivalent for single-task use. It does not expose
`unsubscribe()`; callers depending on unsubscribe must use the
shim directly.

## 5. Log fan-out: `ops_log`

### 5.1 Why `esp_log_set_vprintf`

Spec §6.1 wants both MP `print()` and ESP-IDF `ESP_LOGx` mirrored
to UART0 and to the optional TCP log socket. Two routes were
considered:

- **VFS hook (`esp_vfs_dev_uart_register`)**: registers a
  fan-out file descriptor that intercepts every byte written to the
  console FD. Pros: catches stdio path uniformly. Cons: invasive,
  requires owning the UART0 driver registration, races with
  MicroPython's own UART setup.
- **`esp_log_set_vprintf`**: replaces the IDF log emitter with a
  custom vprintf-like that fans out. Pros: tightly scoped, IDF-blessed,
  preserves existing UART0 path by chaining to the previous hook.
  Cons: catches only ESP_LOGx output, not raw `printf`/`fwrite`.

Decision: `esp_log_set_vprintf` plus a chain to the saved previous
hook. MP `print()` goes to the existing MP REPL dupterm path
(WS-E's `os.dupterm` on UART0 + REPL TCP socket); IDF `ESP_LOGx`
goes through the shim. The TCP log socket therefore mirrors
ESP_LOGx and any caller that explicitly routes through the shim's
fan-out.

If a future requirement needs raw `printf` mirrored to the TCP log
socket too, the VFS-hook route can be added as a second backend
without changing the MP API surface.

### 5.2 Concurrency

One accept task pinned to the configured core (default: free
choice). On accept, the new client replaces any previous client;
the latest log viewer wins. This avoids piling sessions when a
host disconnects unceremoniously.

The vprintf hook runs in the caller's context (MP main task,
ESP-IDF task, scheduled callback). `send()` is `MSG_DONTWAIT`; if
the client TX buffer is full, bytes are dropped silently. Log
emission must not block.

If `send()` returns EPIPE/ECONNRESET/EBADF the hook clears the
client slot so the accept task can install the next client. Other
errno values (`EAGAIN`, `EWOULDBLOCK`) just drop the line and try
again on the next call.

### 5.3 IDF v5.5.1 API check

`esp_log_set_vprintf(vprintf_like_t)` is exposed in
`esp_log_write.h` in v5.5.1 with the same signature as in earlier
v5.x releases. `vprintf_like_t` is `int (*)(const char *, va_list)`.
No API drift.

## 6. Time sync: pure MicroPython

`annealage_pod.ops.time.sync()` calls `ntptime.settime()`. `ntptime` is
shipped as a frozen module in MP's esp32 port; it issues an NTP
query and calls `settimeofday()` (or, on the esp32 port,
`mp_hal_set_time()` mapped to the IDF RTC). Granularity: seconds.
Spec §6.3 accepts second-level granularity for SWO and INA228
sample stitching; the host stitches finer detail by recording the
first-synced-sample offset.

No C shim is provided. If millisecond NTP precision becomes a
requirement (none planned), the route is to add a small shim that
calls `sntp_set_sync_mode(SNTP_SYNC_MODE_IMMED)` and uses the IDF
LWIP-SNTP component directly.

## 7. Pure-MP fallback rules

Each MP-side wrapper follows the same import pattern:

```python
try:
    import ops_<name> as _shim
except ImportError:
    _shim = None

def public_call(...):
    if _shim is not None:
        return _shim.fn(...)
    # WS-E pure-MP fallback path.
    ...
```

Rules:

1. **Production target (ESP32-S3 board build)**: shim present;
   wrapper calls the shim. Behaviour matches spec.
2. **Unix port / non-ESP32 build**: shim absent; wrapper takes the
   pure-MP path. For OTA, this is the slow `esp32.Partition`
   block-write path (not available on Unix; raises clearly). For
   WDT, this is `machine.WDT` (also not on Unix; gracefully
   no-ops). For log, this is `os.dupterm` over a manual TCP socket
   and only catches MP `print()`, not ESP_LOGx.
3. **Public API surface is identical** across both paths. Tests
   under `test/unit/ops/` exercise the import + fallback selection
   on Unix; on-target functional tests are Phase 3.

## 8. Test coverage

`test/unit/ops/` adds Unix-port tests covering:

- All four ops submodules import.
- C shims are absent on Unix; the wrapper picks the fallback path.
- `ota.update(...)` raises `NotImplementedError` without
  `experimental_pure_mp=True` and the C shim absent.
- `wdt.subscribe()` returns None when neither C shim nor
  `machine.WDT` is available; calling `kick()` is safe.
- `log.start()` falls back to socket-listener on Unix; without a
  bound port it returns None.
- `log.client_count()` is consistent with the chosen backend.
- Monkeypatching the `_ops_<name>_c` module to a fake C shim drives
  the shim path through the wrapper.

On-target OTA / WDT / log / NTP smoke is Phase 3.

## 9. Build wiring

`src/c_modules/micropython.cmake` is append-only edited to
`include()` the three new module fragments:

```cmake
include(${CMAKE_CURRENT_LIST_DIR}/ops_ota/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/ops_wdt/micropython.cmake)
include(${CMAKE_CURRENT_LIST_DIR}/ops_log/micropython.cmake)
```

`ops_ota` appends `esp_https_ota`, `esp_http_client`, `app_update`
to `IDF_COMPONENTS` before `idf_component_register` runs (the
MicroPython esp32 port includes `usermod.cmake` before building
`IDF_COMPONENTS`, so the append propagates correctly). The other
two modules need only components already in the default list
(`esp_system` for `esp_task_wdt`, `log` for
`esp_log_set_vprintf`, `lwip` for BSD sockets).

No changes to `src/boards/`, `src/tools/setup-idf.sh`, or other
constraint-listed paths.
