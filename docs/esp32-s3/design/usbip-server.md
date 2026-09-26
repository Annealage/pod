# USB/IP server (WS-A) design notes

Phase 2 deliverable for workstream WS-A. Implements the USB/IP
multiplexer specified in `docs/esp32-s3/spec.md` §4.4 and
`research/usbip-multiplexing-design.md`.

## 1. Files

- `src/c_modules/usbip/usbip_protocol.h`: USB/IP wire-format structs
  and constants. Host-portable (no IDF, FreeRTOS, lwIP).
- `src/c_modules/usbip/usbip_proto.{c,h}`: pure-byte protocol
  helpers (pack/unpack, devid, validation). Host-portable.
- `src/c_modules/usbip/virtual_device.{c,h}`: registration table and
  abstraction for synthetic devices. Host-portable.
- `src/c_modules/usbip/usbip_server.{c,h}`: TCP listener, per-
  connection workers, URB dispatch. Target-only (FreeRTOS + lwIP).
- `src/c_modules/usbip/modusbip.c`: MicroPython binding.
- `src/c_modules/usbhost/usbhost.{h,_stub.c,micropython.cmake}`:
  WS-B's USB host backend. Phase 2 is a stub returning -ENOSYS;
  WS-B replaces `usbhost_stub.c`.
- `test/unit/usbip/`: host-side CTest harness covering wire format
  and registry behaviour.

## 2. Vendoring

The ESP32-S3 server, `usbip_server.c`, started from the single-device
server in Scott Shawcroft's
[adafruit/esp-usbip-bridge](https://github.com/adafruit/esp-usbip-bridge)
(local copy in `referencea/esp-usbip-bridge/`). What it kept:

- The accept-loop topology: one accept task, one worker per
  connection, plus a per-URB socket watchdog. Rewritten, same shape.
- The `read_exact` / `write_all` / `discard_exact` helpers and the
  `handle_devlist_request` / `send_device_with_interfaces` split,
  some lines unchanged.
- `make_devid`: `(busnum << 16) | (devnum & 0xFFFF)`.
- The `virtual_device_t` ops table (`control_transfer`,
  `data_transfer`), with optional `on_attach` / `on_detach` hooks
  added.

`usbip_protocol.h` isn't from the reference, it's written from the
kernel spec,
[`usbip_protocol.rst`](https://docs.kernel.org/usb/usbip_protocol.html),
with the spec's field names and static asserts against its offset
tables.

### License

esp-usbip-bridge has no licence at all, so the parts of
`usbip_server.c` taken from it currently have no licence grant. The
RP2350 server, `usbip_server_rp2.c`, was written separately and only
shares common idioms with it.

If upstream adds a licence, note it here and add its copyright and
licence for `usbip_server.c` to `REUSE.toml`.

## 3. Deviations from the reference

1. **Protocol byte-shuffling split out of the server**. The
   reference inlines `htonl` / `htons` calls into the accept-loop
   translation unit, which means the parser cannot be unit-tested
   without lwIP. We split the byte work into `usbip_proto.c` so the
   host CTest harness can link it directly. The decode functions
   take a packed `usbip_header_t` and produce a host-byte-order
   `usbip_decoded_header_t`; the encode side does the reverse.

2. **Multiple-host serialisation**. The reference allows a busid
   to be re-imported by a second connection while the first holds
   it. The kernel's `vhci-hcd` does not check, but the protocol
   states one device per attach. We added a small attachment
   table (`s_state.slots[]`) protected by a FreeRTOS mutex that
   tracks `(busid, fd)` pairs. A second IMPORT for an already-held
   busid responds with `OP_REP_IMPORT.status = 1` and closes the
   connection, matching the design intent in §1.2 of the
   research doc and the brief.

3. **Stop / restart**. The reference exposes a one-shot
   `usbip_server_start()` that runs forever. The MP API needs
   `usbip.stop()`, so we added `usbip_server_stop()` driving an
   idempotent state machine: clearing `running`, shutting down the
   listen fd, and letting the task self-terminate. Re-`start()`
   re-spawns the task with a fresh listener.

4. **Configurable port**. The reference hardcodes 3240. We accept
   `usbip.start(port=3240)` so the host-side test harness can
   connect over a non-default port and CI can run multiple
   simultaneous instances on different ports. The default still
   matches the USB/IP standard.

5. **WS-B and WS-C decoupling**. The reference's `usb_backend.c`
   couples the URB dispatcher tightly to TinyUSB. We split the
   real-USB calls behind the WS-B `usbhost_*` entries and use
   `virtual_device_t.ops` for synthetic devices. WS-A does not
   include any TinyUSB header; WS-B owns that boundary.

6. **APP_CPU pinning**. The reference uses `xTaskCreate` (any
   core). Per `architecture.md` §3, both the accept-loop task and
   the per-connection workers are pinned to APP_CPU using
   `xTaskCreatePinnedToCore` and core ID 1. Wi-Fi and lwIP stay on
   PRO_CPU per the IDF default.

7. **CMD_UNLINK**. Phase 2 keeps the reference's degenerate
   handling: reply `status = 0` (URB already complete). The
   research doc §1.4 calls this acceptable for synthetic devices
   because every transfer completes locally with bounded latency.
   The real-device path will gain seqnum-tracking cancellation
   when WS-B replaces `usbhost_stub.c`. The `socket_watchdog_task`
   still runs on the real path so a TCP RST mid-URB sets the
   `cancel` flag.

## 4. URB dispatch matrix (Phase 2 implementation)

Matches `research/usbip-multiplexing-design.md` §3.

| (busid, dir, ep) | Path |
|---|---|
| 1-N (DUT) EP0     | `usbhost_control_transfer()` -> stub returns -ENOSYS until WS-B |
| 1-N (DUT) bulk    | `usbhost_bulk_transfer()` |
| 1-N (DUT) intr    | `usbhost_interrupt_transfer()` if `usbhost_is_interrupt_endpoint()` |
| 2-N (synthetic)   | `vdev->ops->control_transfer()` for EP0, `data_transfer()` otherwise |

The `is_virtual` flag is computed once at IMPORT time and cached
in `handle_urb_stream`'s local state; per-URB the dispatcher only
checks `endpoint == 0` to pick the control vs data path.

## 5. Concurrency model

| Task | Pinned | Stack | Priority |
|---|---|---|---|
| `usbip_server` accept loop | APP_CPU | 8 KB | 5 |
| `usbip_client` (per fd) | APP_CPU | 8 KB | 5 |
| `usbip_wd` (per real-USB URB) | APP_CPU | 3 KB | 5 |

Buffer ownership: each `client_task` allocates `out_buf` and
`in_buf` per CMD_SUBMIT and frees them after `send_ret_submit`. No
free-list, no per-pipe pool; the synthetic path's per-URB heap
footprint is negligible (DAP packets are 64 bytes), and the real-
USB path is bounded by `USBIP_MAX_TRANSFER_DEFAULT` (16 KiB).

Multiple-host serialisation table:

- `s_state.slots[USBIP_MAX_CLIENTS]` (currently 4): each slot
  stores `(in_use, busid[32], fd)`.
- `attachment_acquire(fd, busid)` returns false if any slot
  already holds that busid; otherwise it claims a free slot.
- `attachment_release(slot_idx)` is called from `client_task` on
  exit (success or failure).
- `usbip_server_attached_busids()` snapshots the table for
  `usbip.attached_devices()`.

## 6. MicroPython API

```python
import usbip
usbip.start()                  # default port 3240
usbip.start(port=3241)         # alternate port (CI)
usbip.stop()
usbip.is_running()             # bool
usbip.attached_devices()       # list[str]   busids currently held
```

Synthetic-device registration is a C-level entry only:

```c
int usbip_server_register_virtual_device(virtual_device_t *dev);
```

The dapprobe module (WS-C) calls this from its own `start()` to
register busid `2-1`. There is no MP-level register call by
design: the lifetime of a synthetic device is the firmware
lifetime.

## 7. WS-B usbhost stub contract

`src/c_modules/usbhost/usbhost.h` declares the API the multiplexer
calls into for real-USB transfers. The Phase 2 implementation in
`usbhost_stub.c` returns:

- `usbhost_start()` -> 0 (no-op).
- `usbhost_get_devices()` -> 0 devices.
- `usbhost_get_device_by_busid()` -> false.
- All transfer entries -> -ENOSYS, with `*in_len = 0`.
- `usbhost_is_interrupt_endpoint()` -> false.

The multiplexer surfaces the -ENOSYS as USBIP_RET_SUBMIT.status,
so a host hitting the DUT busid before WS-B lands sees an honest
error rather than a silent zero-length completion.

WS-B's job is to replace `usbhost_stub.c` with the real TinyUSB
host integration. The header in `usbhost.h` is the contract; do
not change function signatures without updating the multiplexer.

## 8. Host-test harness

`test/unit/usbip/` is a CMake + CTest project that builds
`usbip_proto.c` and `virtual_device.c` against a small test runner.
It covers:

- Wire-format encode/decode (op_common, device descriptor,
  interface descriptor, URB header pack/unpack, RET_SUBMIT and
  RET_UNLINK byte order).
- Validation rules from `usbip_proto_validate_submit` (negative
  length -> EINVAL, too-large -> EMSGSIZE, iso -> EOPNOTSUPP, bad
  direction -> EINVAL, the kernel's NON_ISO_PACKETS tag is
  accepted, plain `0` is also accepted).
- Virtual-device registry: busid assignment, second-device
  numbering, find-by-busid with NUL-padded buffers, NULL-ops
  rejection, slot exhaustion, snapshot.

Run with:

```
test/unit/usbip/run.sh
```

The runner is a plain C program; no Unity, no Cmocka. Failures
print `[FAIL]` lines and the binary exits with the failure count
so CTest captures the right status.

## 9. Resolved open issues (from research/usbip-multiplexing-design.md §5.1)

The design doc named six open implementation issues. WS-A
resolutions for those that fall in scope:

1. **OP_REP_IMPORT carrying interface descriptors**. Confirmed
   not to. `handle_import_request` writes only
   `sizeof(usbip_usb_device_t)`; the unit test
   `test_device_desc_size` pins the layout to 0x138 bytes.

2. **CMSIS-DAP-v2 `bDeviceClass` requirement**. WS-A's job is the
   transport; the synthetic device's descriptor stack belongs in
   WS-C. The synthetic device record we emit in DEVLIST uses
   class 0xEF / 0x02 / 0x01 by default per the design doc; WS-C
   may override before calling `usbip_register_virtual_device()`.

3. **idVendor/idProduct collision**. Same: WS-C decision. WS-A
   forwards whatever the registered virtual device declares.

4. **CMSIS-DAP-v2 ZLP / probe-rs #448**. The transport layer
   already handles the implicit short-packet case: a RET_SUBMIT
   with `actual_length < requested` ends the host's read. WS-C's
   data_transfer handler caps EP3 SWO completions to <64 bytes
   to dodge the boundary trap; WS-A makes no special exception.

5. **DEVLIST when both backends are not ready**. The
   multiplexer's `collect_all_devices()` queries `usbhost_*`
   first (Phase 2 stub returns 0) then `usbip_get_virtual_devices()`.
   The synthetic device is reported as soon as WS-C registers
   it; the DUT is reported when WS-B says it has enumerated.
   No special "wait for backends" gate.

6. **Linux usbip-utils version sensitivity**. We hard-require
   USBIP_VERSION = 0x0111. Older clients (< 2.0) sending 0x0100
   are rejected at the op_common version check. This matches the
   reference and the kernel.

Open work moved to Phase 3 / WS-B:

- Real CMD_UNLINK cancellation on the DUT path (per-fd seqnum
  tracking).
- mDNS announcement of the USB/IP service is in WS-E's `boot.py`,
  not here.
- USB/IP detach sequence on Wi-Fi disconnect: covered by the
  per-connection worker's `recv` returning 0 when the TCP socket
  drops; nothing else needed.

## 10. Build verification

`src/tools/build.sh` produces `firmware.bin` cleanly with the
multiplexer. The qstr generator on the esp32 port is sensitive to
new MP_QSTR_* references; if you add a kw arg whose qstr already
appears in a frozen module, wipe `build-ESP32_S3_ANNEALAGE_POD/genhdr/qstr*`
and rebuild. This is an MP esp32 port quirk, not a server bug.
