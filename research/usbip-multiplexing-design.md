# USB/IP multiplexing design: DUT (TinyUSB) + synthetic CMSIS-DAP-v2 on one server

Phase 0.4 deliverable per `plan/phase-0-foundations.md`. Targets a single
ESP32-S3-hosted USB/IP server on TCP/3240 that exports two devices
simultaneously: busid `1-1` is the real DUT proxied through TinyUSB host,
busid `2-1` is a synthetic CMSIS-DAP-v2 probe handled internally by the
`dapprobe` C user module.

The reference single-device implementation is
`referencea/esp-usbip-bridge/`. Its protocol layer already supports a
"virtual device" abstraction (`virtual_device.{c,h}`, `virtual_cdc.{c,h}`),
and the multi-device groundwork in `usb_backend_get_devices` already
appends virtual devices to the list returned by the real-USB backend
(`referencea/esp-usbip-bridge/main/usb_backend.c:835-859`). The
multiplexing work is therefore mostly about (a) confirming the wire
format is honoured for two-device DEVLIST replies, (b) drafting the
synthetic CMSIS-DAP-v2 descriptor stack that hosts will accept, and (c)
specifying the URB dispatch matrix and the FreeRTOS objects that connect
the per-connection worker task to the CMSIS-DAP command interpreter and
the SWO drain.

References to upstream protocol facts in this document use:
- USB/IP wire format: kernel docs at `Documentation/usb/usbip_protocol.rst`
  (rendered at <https://docs.kernel.org/usb/usbip_protocol.html>).
- CMSIS-DAP-v2 descriptor expectations: ARM-software CMSIS-DAP "Configure
  USB peripheral" reference, plus the host-tool match logic in pyOCD and
  probe-rs (cited inline below).

## 1. USB/IP protocol layer

### 1.1 OP_REQ_DEVLIST and OP_REP_DEVLIST with two devices

Wire format reminder (all fields network byte order, kernel docs cited):

- `op_common` is 8 bytes: `version` (u16, 0x0111), `code` (u16,
  `OP_REP_DEVLIST = 0x0005`), `status` (u32, 0 on success).
- Then `n` (u32, exported device count).
- Then `n` device records concatenated. The kernel docs note explicitly
  that "the second exported USB device starts at i=1 with the path
  field"; there is no padding between records.

Each device record is laid out exactly as the C struct in
`referencea/esp-usbip-bridge/main/usbip_protocol.h:78-93`
(`usbip_device_desc_t`), 0x138 bytes followed by `bNumInterfaces`
4-byte interface descriptors (`usbip_interface_desc_t`, fields
`bInterfaceClass`, `bInterfaceSubClass`, `bInterfaceProtocol`, one byte
of pad). The existing reference already serialises this exactly in
`fill_wire_device_desc()` and `send_device_with_interfaces()`
(`referencea/esp-usbip-bridge/main/usbip_server.c:92-135`).

`handle_devlist_request` already iterates over the merged device list
returned by `usb_backend_get_devices` (`usbip_server.c:463-500`), and
`usb_backend_get_devices` already concatenates the real USB devices
with the virtual devices via `virtual_device_get_all`
(`usb_backend.c:835-859`). The protocol-level change required for the
multiplexer is therefore zero on the DEVLIST path; what needs to be
specified is the field-by-field content of the synthetic device record
and the busid namespace policy.

#### 1.1.1 Synthetic CMSIS-DAP-v2 device record (busid 2-1)

| Wire field | Value | Source / rationale |
|---|---|---|
| `path[256]` | "/sys/devices/platform/annealage_pod/usb2/2-1" (NUL-padded) | Free-form; Linux `usbip` userspace ignores the value but logs it. Mirror the kernel sysfs convention so it looks normal in `usbip list -r`. |
| `busid[32]` | "2-1" (NUL-padded) | Bus 2 (synthetic), device 1. Matches the bus assignment policy already used in `referencea/esp-usbip-bridge/main/virtual_device.c:23-31` (`VIRTUAL_DEVICE_BUSNUM = 2`). The DUT lives on bus 1 (`usb_backend.c:454`). |
| `busnum` (u32 BE) | 2 | Synthetic bus. |
| `devnum` (u32 BE) | 1 | First (and only) device on the synthetic bus. |
| `speed` (u32 BE) | 2 (`USB_SPEED_FULL`) | The synthetic device pretends to be FullSpeed. Matches the encoding in `usb_speed_to_usbip` (`usb_backend.c:132-144`). |
| `idVendor` (u16 BE) | 0xC251 | Keil/ARM Tools VID. The same VID windowsair uses (`referencea/wireless-esp32-dap/components/USBIP/usb_descriptor.h:7`). It appears in pyOCD's `KNOWN_CMSIS_DAP_IDS` table as `(KEIL_VID, 0x2750)`, which is logged-permission useful but not load-bearing. See §2.2 for why VID/PID is not the primary recognition path. |
| `idProduct` (u16 BE) | 0xF00A | windowsair's PID (`usb_descriptor.h:9`). Free for us to pick; the only host-tool effect is log lines. |
| `bcdDevice` (u16 BE) | 0x0100 | Mirrors windowsair (`usb_descriptor.h:11`). |
| `bDeviceClass` | 0xEF | Miscellaneous Device Class. Pairs with `bDeviceSubClass=0x02` and `bDeviceProtocol=0x01` to form the IAD-friendly composite-device triple, which is what CMSIS-DAP-v2 reference firmware ships when a probe is part of a composite USB device. For a single-interface synthetic device 0x00 / 0x00 / 0x00 ("class defined at interface level") is also valid; the host-tool code paths do not branch on it. We pick 0xEF/0x02/0x01 to leave room for a future composite that adds a CDC log alongside CMSIS-DAP. |
| `bDeviceSubClass` | 0x02 | See above. |
| `bDeviceProtocol` | 0x01 | See above. |
| `bConfigurationValue` | 1 | One-config device, configuration value 1. |
| `bNumConfigurations` | 1 | One configuration. |
| `bNumInterfaces` | 1 | Single CMSIS-DAP-v2 interface. |
| Interface 0: `bInterfaceClass` | 0xFF | Vendor-specific; this is the value pyOCD's `_match_cmsis_dap_v2_interface` requires (see §2.2). |
| Interface 0: `bInterfaceSubClass` | 0x00 | Required by pyOCD's check. |
| Interface 0: `bInterfaceProtocol` | 0x00 | No standard protocol. |

Encoding note: the "device class" / "device subclass" / "device
protocol" triple in the OP_REP_DEVLIST record corresponds to the
top-level USB device descriptor fields, not interface fields. It is
informational for the kernel; the kernel attaches `vhci-hcd` and runs
its own enumeration over the URB stream (`OP_REQ_IMPORT` followed by
control-transfer URBs to fetch every descriptor). What the kernel and
the host tools actually trust is the URB-level descriptor stream
described in §2.

#### 1.1.2 DUT (busid 1-1) device record

This is unchanged from the single-device server: `usb_backend.c:400-496`
(`export_new_device`) populates a `usbip_backend_device_t` from
TinyUSB's cached device descriptor (`usb_host_get_device_descriptor`)
and the active configuration descriptor (`usb_host_get_active_config_descriptor`).
The fields written are:

- `path` <- "/esp-usb-host/1-<devnum>" (line 455).
- `busid` <- "1-<devnum>" (line 454).
- `busnum=1`, `devnum=dev_info.dev_addr`, `speed` mapped from
  `dev_info.speed`.
- `id_vendor`, `id_product`, `bcd_device`, `bDeviceClass`,
  `bDeviceSubClass`, `bDeviceProtocol`, `bNumConfigurations`,
  `bConfigurationValue` <- copied directly from the TinyUSB-cached
  device descriptor and `usb_device_info_t` (lines 432-440).
- `bNumInterfaces`, plus the interface triple list, are parsed from the
  active configuration descriptor blob in `parse_descriptors`
  (`usb_backend.c:146-183`).

Hubs are detected and not exported (`is_hub_device`, lines 185-198).

#### 1.1.3 Busid namespace policy

- Real USB devices: bus `1`, device `<dev_info.dev_addr>` from TinyUSB.
- Synthetic devices: bus `2`, device `1` for the CMSIS-DAP-v2 probe.
  (The reference allows up to 4, `VIRTUAL_DEVICE_MAX = 4` in
  `virtual_device.h:11`. We keep that headroom but only register one in
  rev1; future rev could add a logic-analyser device on `2-2` etc.)
- Busid strings are NUL-padded to 32 bytes on the wire. `memcmp` on the
  full 32-byte field is the lookup key in both the real and virtual
  paths (`virtual_device.c:42-58`, `usb_backend.c:200-218`).

### 1.2 OP_REQ_IMPORT routing

Wire format: client sends `op_common` with `code = 0x8003` and `status = 0`,
followed by the 32-byte `busid`. Server replies with `op_common`
(`code = 0x0003`, `status = 0` for success or `1` for failure). On
success, the server appends the same `usbip_device_desc_t` (the 0x138-byte
device record) for the imported device, but **no interface records**. The
kernel's `usbip_attach.c` reads exactly `sizeof(struct usbip_usb_device)`
after the op_common header; any extra bytes get misinterpreted as URB
PDU data. This is documented inline in `usbip_server.c:526-537`.

Routing in the multiplexer:

```
handle_import_request(fd):
    read 32-byte busid
    device = usb_backend_get_device_by_busid(busid)   # checks both real and virtual
    if device not found:
        send_op_common(REP_IMPORT, status=1); return
    send_op_common(REP_IMPORT, status=0)
    write_all(usbip_device_desc_t)  # NO interface records
    handle_urb_stream(busid, devid)
```

`usb_backend_get_device_by_busid` already does the right thing: it tries
the real-device slot table first, and falls back to
`virtual_device_find_by_busid` (`usb_backend.c:861-886`). The synthetic
device's busid "2-1" only matches the virtual table, the DUT's "1-N"
only matches the real-device table. Two namespaces, no collisions.

`devid` returned to the URB-stream loop is computed by
`make_devid(device) = (busnum << 16) | (devnum & 0xFFFF)`
(`usbip_server.c:87-90`). For the synthetic device this is
`0x00020001`, for a DUT at devnum 5 it would be `0x00010005`. The
`handle_urb_stream` callsite enforces `request->base.devid ==
expected_devid` and answers `-ENODEV` otherwise (`usbip_server.c:246-254`).

The single TCP connection from one host can only have one device
imported at a time on the upstream `usbip` userspace tool; if the user
wants both the DUT and the probe attached they run `usbip attach` twice,
which opens two TCP/3240 connections. The server's accept loop already
spawns a per-connection task (`client_task` in `usbip_server.c:577-592`),
so two concurrent imports just become two parallel `client_task`
instances. This matches the kernel's `vhci-hcd` model: each `attach`
binds a single sysfs vhci port to a single TCP socket.

### 1.3 USBIP_CMD_SUBMIT routing

After import, the per-connection worker enters `handle_urb_stream`
(`usbip_server.c:410-461`). It reads 48-byte `usbip_header_t` records
in a loop (`usbip_protocol.h:68-76`, _Static_assert at line 102 nails
the size). Each record carries `command`, `seqnum`, `devid`,
`direction`, `ep`, plus a union for SUBMIT / RET_SUBMIT / UNLINK /
RET_UNLINK.

For `command = USBIP_CMD_SUBMIT` (0x00000001), the dispatch already
implemented in `handle_submit` (`usbip_server.c:203-374`) is the right
shape; the multiplexer just needs the per-busid backend chosen
correctly. The decision is made once at import time and cached:

```
is_virtual = (virtual_device_find_by_busid(imported_busid) != NULL)
```

(line 417 in the reference). This avoids a per-URB hash lookup. Then
inside `handle_submit`:

```
if endpoint == 0:
    # control transfer
    if is_virtual:
        vdev->ops->control_transfer(...)
    else:
        usb_backend_control_transfer(...)
else:
    if is_virtual:
        vdev->ops->data_transfer(...)
    else if usb_backend_is_interrupt_endpoint(...):
        usb_backend_interrupt_transfer(...)
    else:
        usb_backend_bulk_transfer(...)
```

This is exactly the structure already present in
`usbip_server.c:294-362`. The multiplexer reuses it unchanged.

#### 1.3.1 Per (busid, direction, endpoint) dispatch table

| busid | direction | ep | transfer-type | handler |
|---|---|---|---|---|
| 1-N (DUT) | OUT | 0 | control | `usb_backend_control_transfer` -> TinyUSB `usb_host_transfer_submit_control` |
| 1-N (DUT) | IN | 0 | control | `usb_backend_control_transfer` -> TinyUSB |
| 1-N (DUT) | OUT | bulk EP | bulk | `usb_backend_bulk_transfer` -> TinyUSB |
| 1-N (DUT) | IN | bulk EP | bulk | `usb_backend_bulk_transfer` -> TinyUSB |
| 1-N (DUT) | IN | interrupt EP | interrupt | `usb_backend_interrupt_transfer` -> TinyUSB |
| 2-1 (synthetic CMSIS-DAP) | OUT | 0 | control | `dapprobe_control_transfer` (handles GET_DESCRIPTOR, SET_CONFIGURATION, SET_INTERFACE, GET_STATUS, CLEAR/SET_FEATURE, BOS, MS-OS-2.0 vendor request) |
| 2-1 | IN | 0 | control | same `dapprobe_control_transfer` |
| 2-1 | OUT | EP1 | bulk | `dapprobe_data_transfer` -> push into DAP-cmd ring (StreamBuffer); the CMSIS-DAP interpreter task drains and posts a response into a per-device response ring |
| 2-1 | IN | EP2 | bulk | `dapprobe_data_transfer` -> pull from DAP-response ring (StreamBuffer); blocking receive with a small timeout (see §3) |
| 2-1 | IN | EP3 | bulk | `dapprobe_data_transfer` -> pull from SWO ring (PSRAM tier-2, see spec.md §4.7); zero-byte completion if empty so the host's URB cycles |

#### 1.3.2 Error paths in CMD_SUBMIT

Already-implemented error returns (each is a `USBIP_RET_SUBMIT` with
a negative errno in `status`):

- `req_len < 0` -> `-EINVAL` (`usbip_server.c:228-231`).
- `req_len > CONFIG_USBIP_MAX_TRANSFER` -> drain OUT data, return
  `-EMSGSIZE` (lines 232-240).
- bad `direction` (not 0 or 1) -> drop the connection (lines 241-244;
  this is unrecoverable from a wire-format perspective).
- `devid != expected_devid` -> drain OUT data, return `-ENODEV`
  (lines 246-254).
- `malloc` failure on OUT staging buffer -> drain, return `-ENOMEM`
  (lines 257-269).
- iso `number_of_packets > 0` and not 0xFFFFFFFF -> return
  `-EOPNOTSUPP` (lines 274-279). USB/IP isochronous is unsupported in
  this server (and not needed by either the DUT class drivers we care
  about or by CMSIS-DAP).

Multiplexer-specific additions:

- URB to a non-existent endpoint on the synthetic device. The synthetic
  data_transfer handler returns `-EPIPE` (STALL) on unknown ep_addr.
  Mirrors `referencea/esp-usbip-bridge/main/virtual_cdc.c:225-233`. The
  kernel maps `-EPIPE` to a host-side stall, which is the correct USB
  behaviour for an endpoint that does not exist or is not in the
  current alt-setting.
- URB to a synthetic endpoint that is currently halted (CMSIS-DAP
  reset-in-progress, see §3.4): return `-EPIPE` until the interpreter
  clears the halt.
- Setup-direction mismatch on EP0 (header's `direction` flag does not
  match `bmRequestType.dir`) -> `-EINVAL`. Already enforced at
  `usbip_server.c:320-325` for real devices; same check applies to the
  virtual EP0 path (the virtual control_transfer handler should
  validate the same condition).
- busid not imported: cannot occur on the URB-stream path because
  `handle_urb_stream` is entered only after a successful import. Stray
  CMD_SUBMIT before import (i.e. on the same connection without an
  IMPORT op_common) is impossible by construction; the protocol layer
  would have read it as the next op_common header bytes and rejected
  the version field.

### 1.4 USBIP_CMD_UNLINK and cancellation

Wire format: 48-byte `usbip_header_t` with `command = 0x00000002`,
followed by `unlink_seqnum` (the seqnum of the URB to cancel) and 24
bytes of pad. Server responds with `USBIP_RET_UNLINK` carrying
`status = -ECONNRESET` if cancellation succeeded, or `0` if the SUBMIT
had already completed.

The reference's current handling is degenerate: it always answers
`status = 0` (`usbip_server.c:451-455`) and does not actually attempt
to cancel an in-flight URB. The DUT path nominally supports
cancellation via the `cancel` flag plumbed into
`usb_host_client_handle_events`, but the host-side `vhci-hcd` only
issues UNLINK when a process aborts a transfer; in practice during
normal `pyOCD` / `usbip` use, UNLINK is rare.

Multiplexer policy:

- On synthetic devices, every transfer is satisfied locally with
  bounded latency (the CMSIS-DAP interpreter is bounded by SWD frame
  time, the SWO drain is bounded by the configured peek timeout). The
  worker thread that handles a CMD_SUBMIT runs entirely in the
  per-connection task context. Real cancellation is therefore
  unnecessary; reply `status = 0` (URB already complete by the time
  UNLINK arrives), which is what the reference already does.
- On real devices, set the `cancel` flag for the matching seqnum (need
  to add a small per-connection seqnum -> in-flight-pipe map; trivial
  because there is at most one in-flight URB per pipe slot in the
  reference's pipe-pool design at `usb_backend.c:50-67`). Reply
  `status = -ECONNRESET` once the worker observes the cancel and
  returns. If the worker had already returned a successful RET_SUBMIT,
  reply `status = 0`.
- On TCP RST or FIN, the per-connection worker's `recv` returns 0 and
  it tears down. The reference adds a `socket_watchdog_task` that
  polls for client disconnect via `select` + `MSG_PEEK` while a URB is
  outstanding, and sets the `cancel` flag if the socket is gone
  (`usbip_server.c:382-408`). This is the right shape; reuse it on the
  real-device path. Skip it on the synthetic path because synthetic
  transfers complete in microseconds-to-milliseconds and don't need a
  per-URB watchdog.

### 1.5 Wire-level concurrency

| Task | Purpose | Where |
|---|---|---|
| `usbip_server` | Owns the listener socket (TCP/3240 bind/listen/accept), spawns one `client_task` per connection. Pinned to APP_CPU per `architecture.md` §3. | `usbip_server.c:594-660`. One instance. |
| `client_task` | Per-connection worker: drives op_common -> import -> URB stream loop. Owns `fd`, owns inbound URB buffers. Calls back into `usb_backend_*` (DUT) or `vdev->ops->*` (synthetic). | `usbip_server.c:577-592`. One per active TCP connection (typically 1 or 2: one for the DUT `usbip attach`, one for the probe `usbip attach`). |
| `socket_watchdog_task` | Short-lived helper, spawned only for in-flight DUT URBs, polls `recv(MSG_PEEK)`; sets `cancel` flag if socket dies. | `usbip_server.c:381-408`. Created and torn down per CMD_SUBMIT on the DUT path. |
| `usb_backend` | Single FreeRTOS task that drains the pre-allocated pipe-request slot table. Wakes on `xTaskNotifyGive` from `client_task`, fans out into `usb_host_transfer_submit*`. Pinned APP_CPU. | `usb_backend.c:706-773`. |
| `usb_host_daemon` | IDF's `usb_host_lib_handle_events` pump; required by the IDF `usb_host` API. | `usb_backend.c:706-722`. |
| CMSIS-DAP interpreter | Drains EP1 OUT StreamBuffer, runs `DAP_ProcessCommand`, pushes response into EP2 IN StreamBuffer. Calls `swd_engine_*` for SWD I/O. Pinned APP_CPU. | New, in `c_modules/dapprobe/`. |
| SWO drain | Periodically copies tier-1 DRAM ring (UHCI target) into tier-2 PSRAM ring (Bulk-IN backing store). Pinned APP_CPU. | New, see spec.md §4.7. |

Buffer ownership lives in `client_task`. It reads OUT data into a
malloc'd `out_buf` (or short-circuits if the request is over the
`CONFIG_USBIP_MAX_TRANSFER` limit), allocates an `in_buf` of
`req_len` bytes for IN transfers, and frees both immediately after
`send_ret_submit` (`usbip_server.c:256-289, 364-372`). The synthetic
backends' `data_transfer` ops copy bytes in or out of these buffers and
do not keep references after they return. The per-pipe semaphore in
`usb_backend.c` lets the real-device backend block the `client_task`
until the underlying TinyUSB transfer completes; the synthetic backends
are purely synchronous from `client_task`'s perspective (they may
block briefly inside the StreamBuffer wait, but bounded).

Cancellation on TCP RST/FIN:

- `client_task` recv returns <= 0 in `read_exact`, which propagates as
  false out of `handle_urb_stream`. `client_task` then closes the fd
  and exits.
- If a CMD_SUBMIT was in-flight, the `socket_watchdog_task` (real
  device) or the natural completion (synthetic device) wraps up. The
  RET_SUBMIT write fails on the dead socket and is silently dropped;
  no leak because `out_buf` and `in_buf` are still freed.
- Per-pipe semaphore is binary; the `usb_backend` task gives it on
  completion regardless of whether the requester is still reading.

## 2. Synthetic CMSIS-DAP-v2 USB device shape

The host's `vhci-hcd` runs a full enumeration over the URB stream after
`OP_REP_IMPORT`. The synthetic device must answer GET_DESCRIPTOR for
device, configuration, BOS, string, and (optionally) the
Microsoft-OS-2.0-platform-capability descriptor blob, and must then
present a vendor-specific interface with two or three Bulk endpoints.

### 2.1 Device descriptor (18 bytes)

```
bLength            = 0x12
bDescriptorType    = 0x01 (DEVICE)
bcdUSB             = 0x0210                  (2.1, required for BOS / MS-OS-2.0)
bDeviceClass       = 0xEF
bDeviceSubClass    = 0x02
bDeviceProtocol    = 0x01
bMaxPacketSize0    = 0x40                    (64 bytes, FullSpeed EP0)
idVendor           = 0xC251                  (Keil/ARM Tools VID)
idProduct          = 0xF00A                  (free pick; matches windowsair)
bcdDevice          = 0x0100
iManufacturer      = 0x01
iProduct           = 0x02
iSerialNumber      = 0x03
bNumConfigurations = 0x01
```

Notes:

- `bcdUSB = 0x0210` is required to advertise BOS and a USB-2.1 capable
  device. Without it Windows hosts won't probe for the
  Microsoft-OS-2.0 descriptor and will not auto-bind WinUSB. Linux
  hosts via `vhci-hcd` and pyOCD/probe-rs do not strictly require it,
  but it costs nothing to set and matches windowsair's working build
  (`referencea/wireless-esp32-dap/components/USBIP/usb_descriptor.c:32-42`).
- `bDeviceClass = 0xEF / 0x02 / 0x01` is the IAD-ready
  miscellaneous-device triple. `0x00 / 0x00 / 0x00` ("class defined at
  interface level") is also valid for a single-interface device; both
  are accepted by pyOCD and probe-rs because their match logic is on
  the interface descriptor, not the device descriptor (see §2.6).
- `idVendor=0xC251` does not appear in pyOCD's `KNOWN_CMSIS_DAP_IDS`
  list other than via Keil ULINKplus `(0xC251, 0x2750)`. We pick a
  different PID (`0xF00A`) so we don't pretend to be a ULINKplus.
  pyOCD's known-VID/PID list is used only for permission-error logging
  on Linux (`pyusb_v2_backend.py`'s `is_known_cmsis_dap_vid_pid`); the
  real recognition happens via the iInterface string match.

### 2.2 Configuration descriptor (39 bytes total: 9 + 9 + 7 + 7 + 7 with SWO; 32 bytes = 9 + 9 + 7 + 7 if SWO endpoint suppressed)

We always emit EP3, even when SWO is disabled at the DAP layer. This
lets the host attach pyOCD/probe-rs once and start/stop SWO at runtime
via DAP commands without a re-enumerate. The endpoint just stays empty
when SWO is off.

Configuration header:

```
bLength             = 0x09
bDescriptorType     = 0x02 (CONFIGURATION)
wTotalLength        = 39 (0x27): 9 + 9 + 7 + 7 + 7
bNumInterfaces      = 0x01
bConfigurationValue = 0x01
iConfiguration      = 0x00
bmAttributes        = 0x80                    (bus-powered, no remote-wakeup)
bMaxPower           = 0xFA (500 mA)           (synthetic, value is informational)
```

### 2.3 Interface descriptor (9 bytes)

```
bLength            = 0x09
bDescriptorType    = 0x04 (INTERFACE)
bInterfaceNumber   = 0x00
bAlternateSetting  = 0x00
bNumEndpoints      = 0x03                  (or 0x02 if SWO endpoint omitted)
bInterfaceClass    = 0xFF                  (vendor-specific)
bInterfaceSubClass = 0x00
bInterfaceProtocol = 0x00
iInterface         = 0x04                  (string index, see §2.5)
```

The triple `bInterfaceClass=0xFF, bInterfaceSubClass=0x00,
bInterfaceProtocol=0x00` is the exact filter pyOCD applies in
`_match_cmsis_dap_v2_interface` ("`bInterfaceClass != 0xff` or
`bInterfaceSubClass != 0` disqualifies"). probe-rs does not check the
class triple but does check the endpoint layout (see §2.4) and the
iInterface string.

### 2.4 Endpoint descriptors

CMSIS-DAP-v2 requires the exact endpoint order: EP1 Bulk-OUT first,
EP2 Bulk-IN second, optional EP3 Bulk-IN third. Both pyOCD and probe-rs
pick endpoints by index (eps[0] / eps[1] / eps[2]), not by address; in
particular probe-rs's `open_v2_device` requires "eps[0] = bulk OUT,
eps[1] = bulk IN, eps[2] (optional) = bulk IN".

```
EP1 Bulk-OUT (DAP commands):
  bLength          = 0x07
  bDescriptorType  = 0x05 (ENDPOINT)
  bEndpointAddress = 0x01                    (OUT, EP 1)
  bmAttributes     = 0x02                    (Bulk)
  wMaxPacketSize   = 0x0040                  (64 bytes, FullSpeed)
  bInterval        = 0x00                    (Bulk: ignored)

EP2 Bulk-IN (DAP responses):
  bLength          = 0x07
  bDescriptorType  = 0x05
  bEndpointAddress = 0x82                    (IN,  EP 2)
  bmAttributes     = 0x02
  wMaxPacketSize   = 0x0040
  bInterval        = 0x00

EP3 Bulk-IN (SWO trace):
  bLength          = 0x07
  bDescriptorType  = 0x05
  bEndpointAddress = 0x83                    (IN,  EP 3)
  bmAttributes     = 0x02
  wMaxPacketSize   = 0x0040                  (64 bytes; see ZLP note in spec.md §4.7)
  bInterval        = 0x00
```

ZLP note: spec.md §4.7 calls out probe-rs #448. At FullSpeed
`wMaxPacketSize=64`, every SWO completion that is exactly a multiple
of 64 bytes must be followed by a zero-length packet; otherwise some
hosts hang. The natural way to express this in USB/IP is to send a
RET_SUBMIT with `actual_length=N` where N is < the URB's requested
length, which is implicitly a "short packet" and ends the host's
read. The dapprobe data_transfer handler must therefore size each
SWO Bulk-IN completion to deliberately be a short packet (e.g. cap at
60 bytes) when at risk of hitting a 64-byte boundary, or alternately
emit a zero-byte completion immediately after a 64-byte one.

Endpoint count: spec is 2 if no SWO, 3 if SWO. We build a single
config blob with 3 endpoints and let the DAP_SWO_Control command at
the protocol layer toggle whether tier-2 ring is producing data; that
is exactly the DAPLink behaviour and is what pyOCD/probe-rs assume.

CMSIS-DAP-v2 reference says EP3 is the third bulk-IN on the same
interface (single-interface CMSIS-DAP-v2). It is not a separate
interface. Confirmed via:
- ARM-software CMSIS-DAP "Configure USB peripheral" page: "Endpoint
  3: Bulk In (optional) - used for streaming SWO trace".
- windowsair `kUSBd0InterfaceDescriptor[]` carries three endpoints on
  one interface (`referencea/wireless-esp32-dap/components/USBIP/usb_descriptor.c:75-165`).

### 2.5 String descriptors

| Index | Content | Encoding |
|---|---|---|
| 0 | LANGID list, just `0x0409` (English-US) | 4 bytes: 04 03 09 04 |
| 1 | Manufacturer "mpy-pod" | UTF-16LE in standard string descriptor |
| 2 | Product "mpy-pod synthetic CMSIS-DAP" | UTF-16LE |
| 3 | iSerial = lower 12 hex chars of `esp_efuse_mac_get_default()` | 12 ASCII hex digits, encoded UTF-16LE |
| 4 | iInterface "CMSIS-DAP" | UTF-16LE |

Critical: the iInterface string at index 4 MUST contain the substring
`CMSIS-DAP`. Both pyOCD's `_match_cmsis_dap_v2_interface`
(`pyocd/probe/pydapaccess/interface/pyusb_v2_backend.py`, returns
False if `"CMSIS-DAP" not in interface_name`) and probe-rs's
`is_cmsis_dap` (`probe-rs/src/probe/cmsisdap/tools.rs`,
`id.contains("CMSIS-DAP") || id.contains("CMSIS_DAP")`) recognise
devices via this exact string. If the synthetic device fails to set
this iInterface string the host tools will silently skip it.

windowsair currently uses iProduct = "LPC-Link-II" and iInterface =
"LPC-Link-II CMSIS-DAP" (`usb_descriptor.c:319-345`). The `LPC-Link-II`
prefix is harmless and helps Windows OEM driver auto-installation;
"mpy-pod synthetic CMSIS-DAP" is fine for Linux hosts.

iSerial lifted from `esp_efuse_mac_get_default()` (lower 6 bytes of MAC
formatted as 12 hex digits) gives every annealage_pod a unique serial that
pyOCD's `--target-id` and probe-rs's `--probe` filters can pin to.

### 2.6 BOS + Microsoft OS 2.0 descriptors (Windows-only)

Strictly optional for Linux hosts (pyOCD on Linux uses libusb directly,
no WinUSB). For Windows hosts they trigger automatic WinUSB binding.
Reuse the windowsair blob unchanged
(`referencea/wireless-esp32-dap/components/USBIP/MSOS20_descriptor.c`,
`bosDescriptor[33]` and `msOs20DescriptorSetHeader[162]`).

The dapprobe control_transfer handler answers:

- GET_DESCRIPTOR for type 0x0F (BOS): return `bosDescriptor`.
- Vendor request `bRequest = bMS_VendorCode` (0x01) with
  `wIndex = 0x07`: return `msOs20DescriptorSetHeader`.

Set `bcdUSB = 0x0210` in the device descriptor (already noted in §2.1)
to advertise that BOS exists.

### 2.7 Host-tool match-path validation

| Tool | Recognition criterion | Source |
|---|---|---|
| pyOCD CMSIS-DAP-v2 | iInterface contains "CMSIS-DAP" AND `bInterfaceClass=0xFF` AND `bInterfaceSubClass=0` AND endpoint layout {Bulk OUT, Bulk IN} or {Bulk OUT, Bulk IN, Bulk IN} | `pyocd/probe/pydapaccess/interface/pyusb_v2_backend.py::_match_cmsis_dap_v2_interface` |
| probe-rs | iInterface contains "CMSIS-DAP" or "CMSIS_DAP"; eps[0]=Bulk OUT, eps[1]=Bulk IN, optional eps[2]=Bulk IN; OR (vid,pid) in `KNOWN_DAPS = [(0x1a86, 0x8012)]` | `probe-rs/src/probe/cmsisdap/tools.rs::is_cmsis_dap`, `open_v2_device` |
| OpenOCD `cmsis_dap_usb_bulk` | iInterface contains "CMSIS-DAP" AND vendor-specific class AND Bulk OUT + Bulk IN endpoints; optional VID/PID filter via `cmsis_dap_vid_pid` config | `openocd/src/jtag/drivers/cmsis_dap_usb_bulk.c` (search-result citation, not on this filesystem) |

Conclusion: the load-bearing recognition path is the iInterface string
match, plus the endpoint layout. VID/PID is informational only on
non-Windows. Setting `(0xC251, 0xF00A)` is fine.

## 3. URB dispatch matrix for the synthetic device

Naming: handlers live in `c_modules/dapprobe/`. The dapprobe context
struct mirrors `referencea/esp-usbip-bridge/main/virtual_cdc.h` and
plugs into the `virtual_device_t.ops` table.

| (ep_addr, dir, type) | Handler | Synchronisation primitive | Completion path |
|---|---|---|---|
| EP0, OUT/IN, control | `dapprobe_control_transfer` | None (synchronous, runs on `client_task` stack) | Fills `in_data`/`in_len` directly; returns 0 / -EPIPE / -EINVAL. Caller sends RET_SUBMIT. |
| 0x01, OUT, bulk | `dapprobe_data_transfer` (ep == EP1) | `xStreamBufferSend(dap_cmd_in, out_data, out_len, 0)` to a 4 KB DRAM stream buffer. Non-blocking; if full, return -EAGAIN. | RET_SUBMIT with `actual_length = out_len` immediately on send-success. The DAP interpreter task drains async. |
| 0x82, IN, bulk | `dapprobe_data_transfer` (ep == EP2) | `xStreamBufferReceive(dap_resp_out, in_data, in_capacity, pdMS_TO_TICKS(50))`. Blocks up to 50 ms. | RET_SUBMIT with `actual_length = N`. If timeout, return success with `actual_length = 0` and let the host re-submit (matches DAPLink's flow-control pattern). |
| 0x83, IN, bulk | `dapprobe_data_transfer` (ep == EP3) | Read directly from PSRAM tier-2 ring under a mutex. Bounded by the lesser of `available_bytes` and `in_capacity`, capped to a value < 64 to avoid the wMaxPacketSize boundary trap (see §2.4 ZLP note). | RET_SUBMIT with `actual_length = bytes_copied`. If 0 bytes available, return success with 0 (host re-submits). |

Tasks driven by these primitives:

- `dap_interpreter_task` (APP_CPU, priority 7, 4 KB stack): blocks on
  `xStreamBufferReceive(dap_cmd_in, ..., portMAX_DELAY)`. On a full
  command, calls `DAP_ProcessCommand` (ARM-software CMSIS-DAP
  reference, vendored under `c_modules/dapprobe/vendor/CMSIS-DAP/`).
  Pushes the response into `dap_resp_out`. Handles the SWD I/O
  dispatch via `swd_engine_*` (SPI2 + GDMA backend per spec.md §4.6).

- `swo_drain_task` (APP_CPU, priority 7, 4 KB stack): polls the
  UHCI tier-1 DRAM ring; memcpy's into the tier-2 PSRAM ring. Bounded
  by available DRAM and by the PSRAM cache miss rate (see §4 below
  for backpressure).

The dapprobe context struct adds three handles to the
`virtual_device_t.ctx`:

```
struct dapprobe_ctx {
    StreamBufferHandle_t dap_cmd_in;     /* 4 KB, host-to-device DAP commands */
    StreamBufferHandle_t dap_resp_out;   /* 4 KB, device-to-host DAP responses */
    swo_ring_t *swo_ring;                /* tier-2 PSRAM ring, see spec.md 4.7 */
    uint8_t  current_config;
    uint8_t  current_alt;
    /* descriptor blobs (§2.1-2.6) live in .rodata */
    const uint8_t *device_desc;
    const uint8_t *config_desc;
    size_t config_desc_len;
    const uint8_t *bos_desc;
    size_t bos_desc_len;
    const uint8_t *msos20_desc;
    size_t msos20_desc_len;
    const char *str_manufacturer;
    const char *str_product;
    const char *str_serial;
    const char *str_interface;          /* "CMSIS-DAP" */
    uint8_t   ms_vendor_code;           /* for MS-OS-2.0 vendor request */
};
```

CMSIS-DAP command framing: each USB Bulk-OUT packet is one DAP
command (max 64 bytes for FullSpeed, fits inside one 64-byte BulkOUT).
`dapprobe_data_transfer` with `out_len > 0` pushes the entire `out_data`
into `dap_cmd_in` and tags it as one command boundary. Use
StreamBuffer's "trigger level = 1" so the interpreter wakes per
packet. (CMSIS-DAP does not span commands across multiple Bulk-OUT
packets; this matters because StreamBuffer is byte-oriented, not
message-oriented. If a future change wanted multi-packet commands we'd
swap to MessageBuffer.)

## 4. Concurrency / threading

### 4.1 Listener task

`usbip_server_task` accepts on TCP/3240 (`usbip_server.c:594-660`).
Pinned to APP_CPU per `architecture.md` §3 / `spec.md` §2. Spawns one
`client_task` per accepted connection; never blocks the accept loop on
a slow URB.

### 4.2 Per-connection vs multiplexed

Per-connection. The reference's design (`client_task` per accepted fd)
is the right shape and we keep it. The host runs `usbip attach` once
per device; if the user attaches both the DUT and the probe, two
parallel `client_task`s are alive, each holding its own fd, its own
in-flight URB buffers, and its own `is_virtual` flag. The two paths do
not share state at the transport layer.

Shared state behind `client_task`:

- `usb_backend.s_state.devices[]`: one slot per real DUT, accessed
  under `state_mutex` (`usb_backend.c:84-92`).
- `usb_backend.s_state.pipes[]`: one transfer-pending slot per pipe,
  accessed only by the single `usb_backend_task` (each `client_task`
  pokes a slot then waits on a binary semaphore).
- `s_devices[]` in `virtual_device.c`: one slot per virtual device.
  Lookup is read-only after `register`; no mutex needed.
- `dapprobe_ctx.dap_cmd_in` / `.dap_resp_out`: thread-safe FreeRTOS
  StreamBuffers. Not a contention concern; the only writer for each
  is one task each.
- `dapprobe_ctx.swo_ring`: protected by a small spinlock or a
  PSRAM-aware FreeRTOS mutex. Producer is `swo_drain_task`; consumer
  is `client_task` calling `dapprobe_data_transfer` for EP3.

### 4.3 Buffer pool sizing for inbound URBs

Reuse the reference's "malloc per URB" model on `client_task`. It
allocates `out_buf` and `in_buf` once per CMD_SUBMIT and frees both
after sending the RET_SUBMIT (`usbip_server.c:256-289, 364-372`). Worst
case is `CONFIG_USBIP_MAX_TRANSFER` bytes per direction per in-flight
URB. With one in-flight URB per pipe slot per connection, two
connections, and `CONFIG_USBIP_MAX_TRANSFER = 64 KiB` (a sensible
default for a CMSIS-DAP transfer block), peak heap is ~512 KiB
worst-case. ESP32-S3 with 8 MB octal PSRAM is fine; if we want hard
guarantees we set `CONFIG_USBIP_MAX_TRANSFER = 16 KiB` (one
`DAP_TransferBlock` of 4096 32-bit words) and accept that the kernel
will fragment larger transfers.

CMSIS-DAP packet sizes are all <= 512 bytes (DAP_PACKET_SIZE in
windowsair, `referencea/wireless-esp32-dap/main/dap_configuration.h:37`),
typically 64 bytes per command. So the synthetic path's per-URB heap
is tiny.

### 4.4 Backpressure

Cases where the host attaches but does not drain a Bulk-IN promptly:

- EP2 (DAP responses): `dap_resp_out` is a 4 KB StreamBuffer. If the
  host is slow, the interpreter blocks on `xStreamBufferSend(...,
  portMAX_DELAY)`. This stalls the next CMSIS-DAP command, which is
  the natural USB flow-control behaviour. No URB completes for EP2
  until the host issues another Bulk-IN, at which point bytes flow
  again. No data loss.
- EP3 (SWO): the drain side. Tier-2 PSRAM ring is sized at 8 MB.
  Producer is `swo_drain_task`. Consumer is `client_task` reading on
  EP3 Bulk-IN. If the consumer is slow and the producer fills the
  ring, the producer **drops oldest** and sets the
  `DAP_SWO_BUFFER_OVERRUN` flag in the DAP layer. This matches
  DAPLink's overflow policy (spec.md §4.7) and is reported back to the
  host on the next `DAP_SWO_Status` query. This is replicated on the
  URB-completion side as: when the host's EP3 Bulk-IN URB arrives, it
  reads from whatever bytes are now at the tail of the ring; gaps in
  the trace are visible to the host as the host's expected SWO
  packet-count vs received-count delta, and the overrun-flag query
  tells it the gap is real, not a pyOCD bug.

## 5. Open issues and tests

### 5.1 Open issues

1. **OP_REP_IMPORT interface descriptors**. Confirmed: the kernel
   reads only the device descriptor after the op_common header, no
   interface records. The ESP-USBIP bridge code documents this inline
   (`usbip_server.c:526-537`) and the kernel docs say "the reply ends
   with the status field" if status != 0 and otherwise "details of the
   imported device" follow but do not enumerate interfaces. **No
   ambiguity**, but flag a regression test: send a malformed reply
   with extra interface bytes and verify `vhci-hcd` rejects with
   ECOMM.

2. **CMSIS-DAP-v2 `bDeviceClass` requirement**. Spec is silent on
   whether `bDeviceClass = 0xEF / 0x02 / 0x01` is required or whether
   `0x00 / 0x00 / 0x00` is acceptable. **Test resolves it**: build
   both variants, run pyOCD `pyocd list --info` and probe-rs
   `probe-rs list` against each over `usbip attach`. If both
   recognise both, leave as 0xEF (composite-friendly); otherwise pick
   the variant that works.

3. **idVendor/idProduct collision**. `(0xC251, 0xF00A)` is what
   windowsair ships and is not on the IF-issued ULINKplus PID. There
   is some risk a future Keil product picks F00A. **Test**: lsusb on
   a Keil ULINKplus-equipped lab host verifies no clash today; if
   ever there is, switch to a placeholder pair like `(0x16C0, 0x05DC)`
   (V-USB allocated test PIDs).

4. **CMSIS-DAP-v2 ZLP / probe-rs #448**. The dapprobe SWO data_transfer
   handler caps Bulk-IN payloads to < 64 bytes whenever the natural
   payload length is a multiple of 64. Already replicated in spec.md
   §4.7 footguns. **Test**: run pyOCD with `--trace-output -`
   and OpenOCD with `tpiu config internal :3443` and verify trace
   bytes flow without USB stalls under sustained 6 Mbps SWO.

5. **OP_REQ_DEVLIST when both backends are not ready**. TinyUSB host
   may not have enumerated the DUT yet by the time the user runs
   `usbip list -r annealage_pod.local`. The current
   `usb_backend_get_devices` returns whatever real devices are
   present plus the synthetic one. This is fine: a re-run of
   `usbip list -r` after DUT plug shows both. **Test**: scripted
   plug-replug cycle confirming the synthetic device's busid never
   moves and the DUT's busid is stable across plug events.

6. **Linux usbip-utils version sensitivity**. Older `usbip-utils`
   (< 4.x) parsed OP_REP_IMPORT slightly differently and accepted
   trailing interface bytes. Newer ones (5.4+) match the kernel-side
   strictness documented in the bridge. **Test**: bring-up against
   Ubuntu 22.04 usbip-utils 2.0 AND a Fedora 40 usbip-utils 6.x.
   Both should `attach` cleanly. If one fails, log the byte-stream
   delta against the wire spec.

### 5.2 Bring-up tests

1. **Wire-format smoke test**. Drive a Python USB/IP client at the
   server and assert byte-for-byte against the kernel docs:
   - DEVLIST: 8-byte op_common header, 4-byte device count = 2,
     2 * (0x138 + bNumInterfaces * 4) bytes of device records.
   - IMPORT for "1-1": op_common status=0, 0x138 bytes of device
     descriptor, no extra bytes.
   - IMPORT for "2-1": same shape, fields per §1.1.1.

2. **vhci-hcd attach + lsusb expected output**:

   ```
   $ usbip attach -r annealage_pod.local -b 2-1
   $ lsusb -d c251:f00a
   Bus 003 Device 042: ID c251:f00a Keil Software, Inc. mpy-pod synthetic CMSIS-DAP
   $ lsusb -v -d c251:f00a | grep -E '(iInterface|bInterfaceClass|bInterfaceSubClass|wMaxPacketSize)'
       bInterfaceClass        255 Vendor Specific Class
       bInterfaceSubClass       0
       iInterface              4 CMSIS-DAP
       wMaxPacketSize     0x0040  1x 64 bytes
       wMaxPacketSize     0x0040  1x 64 bytes
       wMaxPacketSize     0x0040  1x 64 bytes
   ```

3. **pyOCD probe enumeration**:

   ```
   $ pyocd list --info
   #  Probe/Board                   Unique ID                  Target(s)
   -- ----------------------------- -------------------------- ----------
    0 mpy-pod synthetic CMSIS-DAP  <12-hex-mac>               <none>
   ```

   pyOCD's enumeration calls `_match_cmsis_dap_v2_interface` and
   should return True. If it returns False, the iInterface string or
   class triple is wrong; bisect by reading
   `/sys/bus/usb/devices/<id>/<intf>/interface`.

4. **probe-rs --list-probes**:

   ```
   $ probe-rs list
   The following debug probes were found:
   [0]: mpy-pod synthetic CMSIS-DAP -- c251:f00a:<serial> (CMSIS-DAP)
   ```

5. **DUT enumeration**:

   ```
   $ usbip attach -r annealage_pod.local -b 1-2
   $ lsusb | grep '1-2'
   ```
   Confirms the same TCP server can also serve the DUT to a parallel
   session.

6. **Two-attach concurrency**: run pyOCD against busid 2-1 while
   `usbip attach` for 1-2 is alive in another terminal. Confirms the
   per-connection task model in §1.5.

### 5.3 Pitfall list

- `vhci-hcd` requires the kernel module loaded (`modprobe vhci-hcd`).
  Some distros do not ship it; document the dependency.
- `usbip` userspace tool path varies: `/usr/lib/linux-tools/<ver>/usbip`
  on Debian/Ubuntu, `/usr/sbin/usbip` on Fedora. mDNS-aware wrapper
  in `src/tools/` should resolve this.
- Older `usbip-utils` (< 2.0, e.g. CentOS 7 era) shipped a v0.1.x
  protocol byte-incompatible with the modern v0.0.1.1 (i.e. 0x0111).
  We hard-require 0x0111 (`USBIP_VERSION` in
  `usbip_protocol.h:7`); reject anything else.
- TCP_NODELAY is set on accepted sockets (`usbip_server.c:639-640`).
  Without it, Nagle batches the RET_SUBMIT header with the next read,
  leading to host-side latency spikes. Keep it.
- The kernel sometimes sends `number_of_packets = 0xFFFFFFFF` for
  non-iso URBs; some clients send 0. Accept both, reject only
  positive counts (already handled at `usbip_server.c:274-279`).
- `setsockopt(..., SO_LINGER, ..., {1, 0})` on the listener after a
  hot shutdown can prevent EADDRINUSE on `bind`; not currently set,
  works fine because we never restart the listener at runtime.
- Linux `vhci-hcd` is FullSpeed-only across USB/IP unless the user
  has a recent `vhci-hcd` patch for HighSpeed. The synthetic device
  declares `speed = 2` (FullSpeed). DUT speed is whatever TinyUSB
  enumerated (FullSpeed on S3 USB-OTG); both fit.
- `usbip detach` on the host issues a TCP FIN. The
  `socket_watchdog_task` model in the reference handles this; do not
  remove it on the real-device path.
- Windows hosts via the experimental `usbip-win` userland sometimes
  re-issue OP_REQ_DEVLIST mid-session. The accept-loop model copes
  because each request opens a fresh fd, but log-level should be
  `INFO` not `WARN` on the second DEVLIST so logs don't spam.

## 6. Cross-references

- USB/IP wire format: `Documentation/usb/usbip_protocol.rst`
  (<https://docs.kernel.org/usb/usbip_protocol.html>).
- ESP-USBIP-bridge protocol layer:
  `referencea/esp-usbip-bridge/main/usbip_protocol.h:1-104`,
  `usbip_server.c:1-674`, `usb_backend.c:1-1043`,
  `virtual_device.{c,h}`, `virtual_cdc.{c,h}`.
- windowsair CMSIS-DAP-v2 descriptor blob:
  `referencea/wireless-esp32-dap/components/USBIP/usb_descriptor.c:1-354`,
  `usb_descriptor.h:1-71`, `MSOS20_descriptor.{c,h}`.
- ARM-software CMSIS-DAP reference: <https://github.com/ARM-software/CMSIS-DAP>
  (used as protocol-layer source per spec.md §4.4 and survey §a).
- pyOCD CMSIS-DAP-v2 match path:
  `pyocd/probe/pydapaccess/interface/pyusb_v2_backend.py`
  (`_match_cmsis_dap_v2_interface`, `HasCmsisDapv2Interface`).
- probe-rs CMSIS-DAP-v2 match path:
  `probe-rs/src/probe/cmsisdap/tools.rs`
  (`is_cmsis_dap`, `open_v2_device`, `KNOWN_DAPS`).
- spec.md §4.4 (multiplexed USB/IP), §4.5 (TinyUSB host), §4.6 (SWD
  backend), §4.7 (SWO pipeline).
- architecture.md §3 (concurrency model), §4.1-4.2 (data flow).
- research/cmsis-dap-survey.md (firmware base, license analysis).
- research/spi2-swd-benchmark.md (SWD I/O numbers; informs the
  interpreter task's per-frame budget).
