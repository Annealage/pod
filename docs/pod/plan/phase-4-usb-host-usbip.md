# Phase 4: DUT USB host + USB/IP

Workstreams F + D. Export the DUT's USB to a host PC over Wi-Fi. Independent of
the debug stack; sequenced after Phases 2-3 (decision 3).

Goal: a host PC enumerates the DUT as a local USB device via `usbip attach` over
Wi-Fi.

Current status (after Phase 1): the board firmware compiles `machine.USBHost`,
but **host mode is unverified**. One USB controller cannot be device and host at
once; at boot the controller is in device/CDC mode (the USB-CDC REPL enumerates),
and host is meant to engage on demand (`machine.USBHost()` -> `mp_usbh_init_tuh()`,
which deinits the device stack and inits the host stack on the same rhport).
Whether that switch and enumeration actually work on this silicon is unproven, no
DUT has been on the pod's USB port yet. "`machine.USBHost` imports" is not proof.

## Dependencies

- F1 (transport), native USB host validated in D1.2.

## Tasks

### F4.1 Native USB host bring-up and verification
- **Verify the device->host switch**: activating `machine.USBHost()` must take the
  controller out of device mode (the USB-CDC REPL drops, the host `/dev/ttyACM`
  disappears) and into host mode. Confirm over the Wi-Fi REPL (which is
  independent of USB), since USB-CDC is gone once host engages.
- **Standalone enumeration first**: enumerate a simple known USB device (CDC or
  HID) end to end as its own milestone, before any USB/IP forwarding, to prove
  host works at all on this hardware. If the switch or enumeration fails, this is
  the R2 trigger (fall back to Pico-PIO-USB host).
- Then expose raw URB submit/complete suitable for USB/IP forwarding; target the
  typical composite MicroPython-DUT shape (CDC + MSC).

### F4.2 USB/IP server over Wi-Fi
- A C user module on the rp2 port (decision 1), factoring the S3 `usbip` protocol
  logic away from ESP-IDF/lwIP onto rp2's lwIP and native TinyUSB host.
- TCP server on port 3240: OP_REQ_DEVLIST / OP_REQ_IMPORT, CMD_SUBMIT streaming
  to/from the host stack, with the S3 spec's validation (refuse proxied
  SET_ADDRESS, bounds-check ep/direction/blen).
- No CMSIS-DAP multiplexing here: the debug probe is on-pod, so busid 1 (DUT) is
  the only exported device.

### F4.3 Validate
- `usbip attach` from a host PC; confirm the DUT enumerates and basic class
  traffic (CDC echo, MSC mount) works. Record throughput; note the TinyUSB
  single-EP pipeline ceiling if it applies to the native host backend.

## Deliverables

- Native USB host enumeration of a DUT.
- USB/IP server on 3240 advertised in the mDNS TXT record.
- A host PC mounting the DUT over `usbip`.

## Exit gate

Host PC enumerates the DUT through `usbip attach` over Wi-Fi; CDC and MSC traffic
function.

## Risks

- R2 (native host maturity): fall back to Pico-PIO-USB host.
- USB/IP single-EP throughput ceiling (S3 spec §4.5.1) may recur on the native
  backend; characterise and document.

## References

- S3 `v1.6.0-native-flash-default:docs/esp32-s3/design/usbip-server.md`, `v1.6.0-native-flash-default:docs/esp32-s3/design/usbhost.md`
- `research/usbip-multiplexing-design.md` (protocol; multiplexing now unused)
- S3 spec §4.5, §5.5 (trust model carries over)

---

## RP2350 re-cut (all-C lwIP-RAW port) - 2026-06-09

The sections above were written target-agnostic. This is the concrete rp2 plan,
re-cut after hardware validation and a function-level read of the esp32 sources.

### Validated on hardware (2026-06-09), so retired as risks

- Native TinyUSB host on the RP2350 works: `machine.USBHost().active(True)`
  switches the single native USB controller device->host; an nRF52840 DUT
  (`f055:9802`, CDC composite) enumerates and a bidirectional bulk REPL
  round-trip succeeds over the host CDC. Retires **R2** (no Pico-PIO-USB
  fallback) and **R11** (the S3 "bulk completions don't flow back" stall) for
  the CDC case.

### Architecture decision

The pod is a **dumb raw USB/IP forwarder**: it must never run host class drivers
to terminate the DUT (see memory `pod-usbip-forwarder-not-classdriver`). The
build runs class drivers OFF (`CFG_TUH_CDC/MSC/HID=0`, `CFG_TUH_API_EDPT_XFER=1`)
and forwards raw URBs. The transport is **all-C on lwIP's RAW callback API** (not
a Python/asyncio transport and not the esp32 BSD-socket+FreeRTOS design, neither
of which exists on rp2). The esp32 lane/responder/pipelining machinery is **moot**
because the TinyUSB host is depth=1 (one transfer in flight per endpoint), so the
port is also a large simplification.

### Reuse vs rewrite (from the function-level port map)

- **Reuse verbatim**: `usbip_protocol.h`, `usbip_proto.c`, `virtual_device.{h,c}`
  (pure byte-shuffling + the device-record type; host CTest-covered). Wire format,
  devid math, RET_SUBMIT/RET_UNLINK packing, `usbip_proto_validate_submit` all
  carry over.
- **New rp2 source files** (the esp32 `usbhost.c`/`usbip_server.c` stay untouched
  for the esp32 board): `usbhost_rp2.c` and `usbip_server_rp2.c`, selected per board
  in each module's `micropython.cmake`. The modules are pulled into the build by
  `c_module()` entries in the board `manifest.py` (micropython PR #18229), not a
  `USER_C_MODULES` aggregator. The rp2 build includes only `usbip` + `usbhost` (no
  `dapprobe`/synthetic device); `modusbip.c`/`modusbhost.c` get the dapprobe and
  esp32-only-diagnostic surfaces trimmed for the rp2 build.
- **Drop**: all FreeRTOS tasks (pump/watchdog/lane/responder/client/accept), per-EP
  submit mutexes, `done_sem`, `enum_queue`, the natural-vs-cancel CAS, the
  inflight pipelining + counting semaphores, `tx_owner` arbitration, the
  `cancel_done_sem` handshake, conn refcounting, BSD sockets + `lwip_writev`, the
  CMSIS-DAP/busid-2 multiplexing and the `virtual_device` registry at runtime, and
  the ESP32 DWC2 diagnostics (`hprt`, `hprt_trace`, `ep_stats`, `ep0_errors`).

### Enumeration without a submodule patch (no mp_usbh.c change)

The esp32 `usbhost.c` documents a `tuh_mount_hook`/`tuh_umount_hook` mechanism
(comment at `usbhost.c:64-68`), but no `mp_usbh.c` variant actually calls those
hooks - not the current `machine-usbhost` branch, not the esp32-era head
`dfa0adc44`. The hooks are effectively dead/defensive code on esp32. The real
enumeration path is `usbhost_start()`'s seed loop (`usbhost.c:1466-1473`): it scans
`tuh_mounted(addr)` for `addr` in `1..CFG_TUH_DEVICE_MAX` and enumerates each
already-mounted device. The comment there even notes the mount hook "fires with
`enum_queue==NULL`, `enumerate_device` fails silently" at boot, which is why the
seed loop exists. So the esp32 worked with the DUT present before `usbip.start()`,
via the seed loop, not the hook.

The rp2 port does the same and needs **no** `mp_usbh.c` patch:
- `usbhost_rp2.c` overrides the weak `tuh_enum_descriptor_device_cb` /
  `tuh_enum_descriptor_configuration_cb` (weak in `usbh.c`, **not** defined by
  `mp_usbh.c`, so no link collision) to cache descriptors during TinyUSB
  enumeration and return `false` from the config cb to suppress SET_CONFIGURATION
  (keeps the DUT in Address state for the USB/IP host to configure).
- `usbhost_rp2.c` seeds its slot table from `tuh_mounted()` in `usbhost_start()`,
  and **re-scans `tuh_mounted()` lazily** on each `OP_REQ_DEVLIST` / `OP_REQ_IMPORT`
  (and prunes detached addrs), which covers hotplug-after-start without the mount
  hook. `tuh_mounted` is generic and present on rp2.

(If instant hotplug notification is ever wanted instead of lazy rescan, the proper
home is the `machine-usbhost` PR adding the hook calls, not a local tessera patch.)

### Pump dependency (startup orchestration)

`mp_usbh_task` (`mp_usbh.c:246`) early-returns unless a `machine.USBHost` Python
object exists, is initialised, and is active. So the forwarder startup must
`machine.USBHost().active(True)` to drive the cooperative `tuh_task` pump (class
drivers off means its `cdc_devices()`/etc. pools stay empty, which is fine), then
`usbip.start(3240)` brings up the lwIP-RAW server. Do this in the frozen
`annealage_pod` pod startup (alongside `netboot`).

### Concurrency model (the central hazard)

lwIP RAW callbacks fire in the cyw43 async_context under the lwIP lock
(`MICROPY_PY_LWIP_ENTER/EXIT`, `ports/rp2/mphalport.h:51`); TinyUSB runs only in
`tuh_task` on the main MicroPython thread (`mp_usbh_task`, scheduled via
`__wrap_hcd_event_handler` + `mp_sched_schedule_node`). Therefore: every URB
submit/cancel is **marshalled to the tuh_task context**; every reply `tcp_write`
runs **under the lwIP lock**. Replacement for refcounting on
completion-vs-teardown: a per-conn epoch/token in the URB, re-checked in the
completion handler (or NULL `cur_urb->conn` on teardown). Flow control replaces
the esp32 inflight busy-wait: with depth=1 the rx state machine withholds
`tcp_recved` while a URB is in flight.

### Port order (each step ends at a hardware-validated gate)

1. **Build integration.** Declare the usbip + usbhost C modules in the board
   `manifest.py` via the `c_module()` directive from micropython PR #18229
   (integrated into `tessera` with `mbm`); each module dir carries its own
   `micropython.cmake`. Board cmake flags (`CFG_TUH_CDC/MSC/HID=0`,
   `CFG_TUH_API_EDPT_XFER=1`), trimmed module bindings (no dapprobe / no esp32-only
   diagnostics), and skeleton `usbhost_rp2.c`/`usbip_server_rp2.c`. No
   `USER_C_MODULES` aggregator and no `mp_usbh.c` patch. **Gate**: pod firmware
   builds + links + flashes; `import usbip; usbip.start()` returns 0 (no listener yet).
2. **Backend enumeration path.** Slot table + helpers + the weak enum-descriptor cb
   overrides (cache descriptors, suppress SET_CONFIGURATION) + `enumerate_device` +
   seed-from-`tuh_mounted()` in `usbhost_start` + lazy re-scan + device-query
   accessors + cached descriptors. **Gate**: the wired dongle shows up in the slot
   table with cached descriptors (after `USBHost().active(True)` + `usbip.start()`).
3. **lwIP-RAW listener + DEVLIST.** tcp listener/accept, rx-accumulate state
   machine, tx helper with ERR_MEM/`tcp_sent` backpressure, `handle_devlist`
   (= `usbhost_get_devices`). **Gate**: `usbip list -r <pod>` over Wi-Fi returns the DUT.
4. **IMPORT + URB read SM + validation (errors only).** RX_IMPORT_BUSID,
   attachment table, REP_IMPORT, `expected_devid`, RX_HEADER/OUT_PAYLOAD/DISCARD,
   the validation/policy logic (EMSGSIZE/EINVAL/ENODEV, setup-dir sanity,
   SET_ADDRESS refusal, EP0 GET_DESCRIPTOR cache intercept). **Gate**: `usbip attach`
   succeeds; kernel enumerates the DUT from cached descriptors.
5. **Backend submit + completion (the marshalling seam).** `submit_xfer` +
   `xfer_complete_cb` (single completion path) + `usbhost_submit_async`; intake ->
   marshalled submit -> completion -> `tcp_write(RET_SUBMIT)`. **Gate**: real
   control/bulk/interrupt round-trip (DUT CDC enumerates + echoes on the host PC).
6. **CMD_UNLINK + cancellation.** Simplified `usbhost_cancel_ep` (abort + clear +
   deliver -ECONNRESET + close/open recovery); UNLINK handler that defers
   RET_UNLINK until the cancelled URB's RET_SUBMIT is sent. **Gate**: detach
   mid-transfer + open/close storms stay clean.
7. **Teardown + lifecycle.** Use-after-free guard, `tcp_err`/close path,
   `usbip_server_stop` (tcp_close listener + tcp_abort conns), half-open detection
   via `tcp_poll`. **Gate**: abrupt disconnect, Wi-Fi drop mid-transfer, and
   stop/restart all reclaim the import slot and allow immediate re-attach.
8. **mDNS + host tooling.** Advertise `usbip-port=3240` in the TXT record; implement
   `Pod.usbip_attach()` (shells out to the system `usbip` binary, `vhci-hcd`
   loaded) + `pod usbip` CLI + MCP tool. **Gate**: the host `pod`/MCP loop attaches
   the DUT end-to-end.

### Risks specific to the rp2 port

- Cross-context marshalling (step 5) is the highest-risk item; test control,
  bulk-IN, bulk-OUT, interrupt-IN separately.
- DWC2-on-rp2350 abort/recovery (`tuh_edpt_abort_xfer` + close/open) is unproven on
  this HCD (the esp32 close+open dance is DWC2-generic but untested here).
- Depth=1 single-EP throughput ceiling persists (S3 spec §4.5.1); measure and
  document in step 5.
- Trust model: TCP/3240 is unauthenticated; the pod is for a trusted lab network.
