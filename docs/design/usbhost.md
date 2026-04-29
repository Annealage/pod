# USB host backend (WS-B) design notes

Phase 2 deliverable for workstream WS-B. Replaces the WS-A-installed
`-ENOSYS` stub at `src/c_modules/usbhost/usbhost_stub.c` with a
real backend driving the ESP32-S3 USB-OTG controller in host mode.
Targets one DUT device at a time; supports composite devices
(CDC + MSC simultaneously, the typical MicroPython-DUT shape).

## 1. Stack selection: `usb_host` component, not TinyUSB

`docs/spec.md` §4.5 and the open issue at §8 item 3 frame this as
"verify ESP-IDF v5.5 TinyUSB host stack supports the operations the
USB/IP server needs (raw URB submit on arbitrary endpoints,
non-canned class-driver flow). If insufficient, fall back to the
underlying `usb_host` IDF component directly."

Result: TinyUSB host is not part of IDF v5.5.1's bundled component
tree. The IDF ships a single bundled USB stack at
`components/usb/include/usb/usb_host.h`. TinyUSB's host stack exists
upstream at `tinyusb/src/host/usbh.h`, but Espressif distributes it
only via the `esp_tinyusb` managed component, and that wrapper
exposes the device-side API only; the host shim is not packaged.

The reference at `referencea/esp-usbip-bridge/main/usb_backend.c`
makes the same choice for the same reason. Following that path
lets us reuse a known-good slot/pipe topology.

The `usb_host` component delivers exactly what the multiplexer
needs:

- `usb_host_install` / `usb_host_lib_handle_events` for the
  library-wide pump (root-port enumeration, device free).
- `usb_host_client_register` plus an event callback for hot-plug
  notifications.
- `usb_host_device_open`, `usb_host_get_device_descriptor`,
  `usb_host_device_info`, `usb_host_get_active_config_descriptor`
  to populate the `usbip_dev_record_t` fields the USB/IP DEVLIST
  reply needs.
- `usb_host_interface_claim` to take ownership of every interface
  in the active configuration; this is what enables composite
  CDC + MSC simultaneous use.
- `usb_host_transfer_alloc` plus `usb_host_transfer_submit` /
  `usb_host_transfer_submit_control` for raw URB submit on
  arbitrary endpoints, no canned class-driver flow.

No firmware changes upstream of WS-B were required: MicroPython's
esp32 port already lists `usb` in its `IDF_COMPONENTS` set
(`src/micropython/ports/esp32/esp32_common.cmake` line 196), so the
component is linked into the firmware automatically.

## 2. Files

- `src/c_modules/usbhost/usbhost.h`: API contract installed by
  WS-A. Unchanged in this workstream.
- `src/c_modules/usbhost/usbhost.c`: real backend. New file, this
  workstream.
- `src/c_modules/usbhost/usbhost_stub.c`: retained on disk as the
  host-build fallback for the unit-test harness. Only the firmware
  build links `usbhost.c`; the host-side test build can include
  `usbhost_stub.c` directly to validate the API surface without a
  cross-compiler.
- `src/c_modules/usbhost/micropython.cmake`: switched from
  `usbhost_stub.c` to `usbhost.c`.
- `docs/design/usbhost.md`: this document.
- `test/unit/usbhost/`: host-side CTest harness.

## 3. Vendoring

The slot/pipe topology in `usbhost.c` is shape-equivalent to
`referencea/esp-usbip-bridge/main/usb_backend.c`. The reference
repository ships no LICENSE file; per the same analysis applied
in `docs/design/usbip-server.md`, that means we cannot copy the
file verbatim. The implementation here is a clean rewrite that
arrives at the same overall design (single-client async model,
pre-allocated pipe slots, per-pipe binary semaphore, malloc-per-URB)
because that design is the natural shape imposed by the
`usb_host` API. The wire-protocol structures consumed at the
boundary (`usbip_dev_record_t`, `usbip_setup_packet_t`) are
defined in the mpy-pod `usbip` module's headers, not the
reference.

## 4. Concurrency model

Two FreeRTOS tasks pinned to APP_CPU (per `architecture.md` §3):

| Task            | Priority | Stack | Role                                           |
|-----------------|----------|-------|------------------------------------------------|
| `usb_host_daemon` | 10     | 4 KB  | Pumps `usb_host_lib_handle_events` forever.    |
| `usbhost_worker`  | 9      | 8 KB  | Registers the client, drains the event queue (hot-plug), serializes URB submission through pre-filled pipe slots. |

Caller threads (the per-connection USB/IP `client_task` in WS-A)
fill a pre-allocated slot, set `pipe->active = true`, notify the
worker, then block on `pipe->done_sem`. The worker submits the
transfer via `usb_host_transfer_submit*`, drives
`usb_host_client_handle_events` until the IDF callback flips the
`done` flag, then signals the semaphore.

Important constraint: the IDF docs (`usb_host.h` line 283) say
"For a particular client, this function should never be called by
multiple threads simultaneously" of `usb_host_client_handle_events`.
With one client and one worker this is guaranteed. The worker's
top-of-loop also calls `usb_host_client_handle_events` with a zero
timeout to drain hot-plug callbacks before processing the active
slots; both calls run on the same task, so the restriction holds.

## 5. URB lifecycle and IDF API mapping

| API contract (`usbhost.h`)            | IDF call sequence                                                     |
|---------------------------------------|-----------------------------------------------------------------------|
| `usbhost_start()`                     | `usb_host_install` then spawn the daemon and worker tasks.            |
| `usbhost_get_devices` / `_by_busid`   | Snapshot the device slot table under the state mutex.                 |
| `usbhost_control_transfer(..)`        | `usb_host_transfer_alloc(8 + payload)` then `_submit_control`.        |
| `usbhost_bulk_transfer(..)`           | `usb_host_transfer_alloc(payload)` then `_submit`.                    |
| `usbhost_interrupt_transfer(..)`      | Same as bulk; the multiplexer routes via `usbhost_is_interrupt_endpoint` to choose the right path. |
| `usbhost_is_interrupt_endpoint`       | Pure lookup against the cached endpoint table.                        |

Direction is derived from the address bit (`0x80` -> IN) for non-zero
endpoints, and from `bmRequestType` bit 7 for control. IN buffers
are rounded up to a multiple of `wMaxPacketSize` before submission
to satisfy the IDF constraint that `num_bytes` be MPS-aligned for
non-control IN; the caller's expected payload size is preserved
and used to trim the returned data.

## 6. Hot-plug

`usb_host_client_event_cb` runs in the worker's `client_handle_events`
context and posts events into a 16-deep `xQueue`. The worker drains
it once per loop iteration (`drain_event_queue`) so the slow path
(open device, fetch descriptors, allocate pipe slots) does not
block in callback context.

Hub devices are detected (`bDeviceClass == 0x09` or any interface
class == 0x09) and not exported; the spec is one DUT at a time on
busid 1-N.

## 7. Composite-device handling

`ensure_interfaces_claimed_locked` walks the active configuration
descriptor's `bNumInterfaces` and calls `usb_host_interface_claim`
for each one with `bAlternateSetting=0`. For a CDC + MSC composite
this claims both interfaces on the same client, which is supported
by the IDF as long as the interfaces do not share endpoints (CDC
uses dedicated bulk pairs plus an interrupt notification endpoint;
MSC uses its own bulk pair; no overlap).

Interface claim is deferred until the first non-EP0 transfer to
avoid claiming interfaces on devices the host never imports
(reduces wear on hot-plugged stuff like USB sticks).

## 8. Cancellation

The `cancel` flag in the API is polled inside the
`client_handle_events` loop. When the host disconnects or the
USB/IP server signals UNLINK, the multiplexer flips `*cancel` and
the worker exits the wait loop with `-ECONNRESET`. The IDF's
own per-transfer 5 s timeout is a backstop.

## 9. Tunables

Compile-time constants at the top of `usbhost.c`:

| Macro                            | Default | Purpose                                                |
|----------------------------------|---------|--------------------------------------------------------|
| `USBHOST_NUM_PIPES`              | 16      | Total pre-allocated pipe slots across all devices.     |
| `USBHOST_MAX_DEVICES`            | 4       | Concurrent device slot count (rev1 needs 1, headroom). |
| `USBHOST_MAX_ENDPOINTS`          | 16      | Cached endpoints per device.                           |
| `USBHOST_MAX_TRANSFER`           | 64 KB   | Heap-clamp on a single URB.                            |
| `USBHOST_TRANSFER_TIMEOUT_MS`    | 5000    | Per-transfer IDF timeout.                              |
| `USBHOST_TASK_CORE`              | 1       | APP_CPU per `architecture.md` §3.                      |

Override at link time with `-DUSBHOST_NUM_PIPES=N` etc. if a
deployment has different needs.

## 10. Open items

- USB phy initialisation: relies on IDF default
  (`skip_phy_setup = false`). On the actual annealage_pod PCB the OTG
  pins (GPIO19/20) carry the DUT VBUS line through the level
  translators; verify in Phase 3 hardware bring-up that the IDF's
  default phy config matches the board.
- Power management: `usb_host_install` is called with
  `intr_flags = 0`. The board variant sdkconfig does not yet
  enable any USB-host Kconfig knobs (e.g.
  `CONFIG_USB_HOST_HW_BUFFER_BIAS`), keeping the IDF defaults.
  Phase 3 hardware bring-up may want to bias toward the bulk
  endpoint case (CDC + MSC) once measured on real silicon.
- VBUS: drive of DUT-USB VBUS is on a TPS2595 switch controlled
  by a separate GPIO (Appendix A) and toggled by
  `annealage_pod.power.dut_usb` in WS-E. The USB host stack is brought
  up before VBUS is enabled; enumeration kicks off when VBUS goes
  high.
- IDF beta-API caveat: `usb_host.h` opens with the warning "The
  USB Host Library API is still a beta version and may be
  subject to change". The API has been stable across IDF v5.x;
  re-pin the IDF SHA after each minor bump per
  `spec.md` §4.1's versioning policy and re-run the WS-B unit
  tests.

## 11. Phase 3 P3.0 pivot to TinyUSB host

Sections 1-10 above describe the IDF `usb_host` backend that landed
in Phase 2 WS-B. Phase 3 firmware-level smoke (commit `13fd327` plus
the 2026-04-29 progress report) confirmed enumeration works end to
end, but `pyocd reset --target rp2040` and `mpremote` against the
forwarded Pico CDC both hung after enumeration: bulk URB completions
were not flowing back to the host. R11 in the risk register.

Phase 3 P3.0 pivots WS-B from the IDF `usb_host` component to the
TinyUSB host stack via andrewleech/micropython#7 (branch
`machine-usbhost`, head `dfa0adc44` as of 2026-04-29). The choice is
deliberate: the project owner has a working, tested PR that adds
`machine.USBHost()` on TinyUSB host with a shared `mp_usbh.{c,h}`
infra layer, pinned TinyUSB submodule at 0.20.0, and a tested
`USBHOST` variant of `ESP32_GENERIC_S3`. Section 1 above ("TinyUSB
host is not part of IDF v5.5.1's bundled component tree") was a
narrow framing: TinyUSB host has been upstream since 0.15+; PR #7
brings the host stack in via the espressif/tinyusb component plus a
hand-built source list for the host class drivers and the DWC2
controller driver (see `ports/esp32/esp32_common.cmake` block guarded
by `if(MICROPY_HW_USB_HOST)`).

### 11.1 Submodule pin and build flags

- `.gitmodules` `src/micropython` URL is now
  `https://github.com/andrewleech/micropython.git`, branch
  `machine-usbhost`. Re-checkout the submodule to fetch the new tree.
- `src/VERSIONS` records the new commit
  `dfa0adc44662f377ef7ce3561b32004f481909e2` and the rationale.
- `src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.cmake` sets
  `MICROPY_HW_USB_HOST 1` and appends `MICROPY_HW_USB_HOST=1` to
  `MICROPY_DEF_BOARD`, mirroring how the upstream `USBHOST` variant
  of `ESP32_GENERIC_S3` does it.
- `src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.h` defines
  `MICROPY_HW_USB_HOST (1)` so header-side guards in `main.c` and
  the tusb_config chain see it. USB-CDC and USB-Serial-JTAG remain
  disabled (`MICROPY_HW_ENABLE_USBDEV=0`, `MICROPY_HW_USB_CDC=0`,
  `MICROPY_HW_ESP_USB_SERIAL_JTAG=0`); host-mode commitment for the
  USB-OTG peripheral.

### 11.2 mp_usbh surface vs WS-A's contract

`shared/tinyusb/mp_usbh.{c,h}` is class-driver oriented: it wraps
TinyUSB's host CDC, MSC, and HID class drivers and exposes them as
`machine.USBHost`, `USBH_CDC`, `USBH_MSC`, `USBH_HID` Python objects.
It does not expose a "raw URB submit on arbitrary endpoint" entry.

WS-A's `usbhost.h` contract is the opposite shape: per-busid raw
control / bulk / interrupt transfer functions, no class awareness,
because the USB/IP server's job is to forward URBs verbatim from
the remote host's `vhci-hcd` to the device.

Resolution: usbhost.c uses mp_usbh for init only
(`mp_usbh_init_tuh()` + `mp_usbh_task()` event pump) and reaches
through to TinyUSB's host primitives directly:

- `tuh_descriptor_get_device_sync` / `tuh_descriptor_get_configuration_sync`
  to populate the `usbip_dev_record_t` for DEVLIST.
- `tuh_edpt_open` to claim a pipe on a non-zero endpoint.
- `tuh_edpt_xfer` to push a transfer, with completion delivered
  via the `tuh_xfer_t.complete_cb` hook.
- `tuh_control_xfer` for EP0.

To stop the TinyUSB CDC class driver from absorbing the Pico's
CDC interface (which would prevent us from reaching the bulk
endpoints raw), WS-B turns off the host class drivers in the
TinyUSB host config: see `tusb_config_host_overrides.h` for the
`CFG_TUH_CDC=0`, `CFG_TUH_MSC=0`, `CFG_TUH_HID=0` overrides. The
mp_usbh.c class-driver code is still compiled but its mount
callbacks never fire because the drivers are disabled.

### 11.3 Concurrency model under the new backend

- `usbhost_start` calls `mp_usbh_init_tuh()` (which boots the
  TinyUSB host stack, including the DWC2 HCD on ESP32-S3) and
  spawns a dedicated `tuh_task` pump on APP_CPU. The pump runs
  `mp_usbh_task()` (which calls `tuh_task()` until it returns 0)
  in a tight loop.
- A second worker task on APP_CPU drains the pre-allocated pipe
  slot table the same way the IDF backend did. The slot still
  carries the same shape (`busid`, `endpoint_addr`, `setup`,
  `out_data`, `in_data`, `cancel`), but the URB call dispatches
  through `tuh_edpt_xfer` / `tuh_control_xfer` instead of the IDF
  `usb_host_transfer_submit*`.
- TinyUSB's host events are interrupt-driven; the
  `--wrap=hcd_event_handler` link option in the upstream PR moves
  hcd events into a queue that the task pump drains. This is
  load-bearing: without the wrap the IRQ context can race with
  the FreeRTOS scheduler.

### 11.4 Hot-plug and device enumeration

TinyUSB's `tuh_mount_cb(uint8_t dev_addr)` fires when a device
finishes enumeration. usbhost.c hooks it (alongside mp_usbh.c's
own hook) to populate the device slot:

- `tuh_descriptor_get_device_sync(addr, &dev_desc, sizeof(dev_desc))`
  for the 18-byte device descriptor.
- `tuh_descriptor_get_configuration_sync(addr, 0, cfg_buf, sizeof(cfg_buf))`
  for the active configuration descriptor blob.
- Walk the config descriptor for interface and endpoint records,
  exactly the same parsing as the IDF backend (preserved verbatim
  in `parse_config_desc`).
- Allocate per-endpoint pipe slots, but defer `tuh_edpt_open` until
  the first non-zero endpoint URB arrives (lazy claim, matches the
  IDF backend's `ensure_interfaces_claimed_locked`).
- Fill busid as "1-N" where N is `dev_addr`, path as
  `/esp-usb-host/1-N`. Multiplexer-side busid namespace policy
  unchanged.

`tuh_umount_cb(addr)` clears the slot. Hub devices come back with
`bDeviceClass == TUSB_CLASS_HUB` and are not exported (rev1: one
DUT at a time on busid 1-N).

### 11.5 Open items for the pivot

- Class-driver fallback: if disabling CFG_TUH_CDC etc. breaks the
  TinyUSB enumeration path (some TinyUSB versions assert a class
  driver claim on every interface), revisit whether to keep the
  class drivers and instead reroute their bulk-endpoint reads
  through usbhost.c. Track via Phase 3 bring-up logs.
- The `--wrap=hcd_event_handler` link option from PR #7 routes
  hcd events into the TinyUSB task. Verify the wrap symbol
  resolves cleanly under the IDF build; the IDF's
  espressif/tinyusb component supplies its own
  `hcd_event_handler` weak symbol.
- DWC2 DMA is disabled on ESP32-S3 (`CFG_TUH_DWC2_DMA_ENABLE 0`)
  because the S3's L1 cache handling for DMA is not in the
  TinyUSB DWC2 driver. PIO mode bulk transfers are sufficient for
  FullSpeed CDC + a CMSIS-DAP synthetic; revisit if the SWO
  pipeline needs HighSpeed bandwidth.
