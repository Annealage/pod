# R24 plan: finish TinyUSB host pivot in usbhost.c

`docs/esp32-s3/design/usbhost.md` §11 ("Phase 3 P3.0 pivot to TinyUSB host")
described a planned migration from the IDF `usb_host_*` component to
the TinyUSB host stack. The build infrastructure landed (the
MicroPython submodule is on `andrewleech/micropython` `machine-usbhost`
branch; `MICROPY_HW_USB_HOST=1` links TinyUSB host into the firmware
via `mp_usbh.c`), but `src/c_modules/usbhost/usbhost.c` still calls
the IDF `usb_host_*` API directly. The pivot was never finished.

R24 finishes it.

## Why

Two reasons, in priority order:

1. **Throughput diagnosis** (`r23-findings.md` corrected section).
   Firmware-side instrumentation showed pipeline depth 13-18 URBs
   sustained at intake but the IDF host stack on DWC2 ESP32-S3
   processes URBs on the wire at ~88/sec per pipe rather than the
   ~9000/sec the FS bulk wire would support. Per-URB IDF residence
   time is ~11 ms; wire time is ~110 us. The IDF host stack is the
   ceiling.

   TinyUSB host has different pipe scheduling and a different
   completion model. It may pipeline FS bulk on DWC2 better. We
   won't know without running the experiment.

2. **Future growth.** Upstream MicroPython is moving the esp32 port's
   host story toward TinyUSB host (PR #7 / `machine-usbhost`).
   Aligning lets us stay close to upstream for IDF bumps, get the
   class-driver work for free if we want it, and avoid carrying a
   custom IDF-host integration that nobody else maintains.

If the throughput experiment fails (TinyUSB hits the same ~88/sec
ceiling on DWC2), R24 still pays for itself on (2). The migration is
load-bearing for future direction even if throughput stays at
11 KiB/s.

## What stays the same

The whole R20-R23 architecture. R24 swaps the **backend of**
`usbhost.c` only:

- `usbhost.h` public API stays bit-for-bit identical.
  `usbhost_submit_async`, `usbhost_cancel_ep`,
  `usbhost_get_device_by_busid`, `usbhost_is_interrupt_endpoint`,
  `usbhost_start`, the synchronous wrapper trio
  (`usbhost_bulk_transfer`, `usbhost_interrupt_transfer`,
  `usbhost_control_transfer`), and the verbose toggle stay.
- `usbip_server.c` is NOT touched. R20 lanes, R22 responder,
  R23 batching, lwip_writev, all preserved.
- The synthetic CMSIS-DAP fast path in `intake_submit` is
  unaffected (synthetic devices never enter `usbhost.c`).
- All R15-R20 invariants: per-EP submit serialisation around cancel,
  refcount-free single-owner inflight (R22 step 5),
  tx_owner arbitration with UNLINK (in usbip_server.c, untouched).

## Target architecture

### Init

Replace `usb_host_install` + `usb_host_lib_handle_events` daemon task
+ `usb_host_client_register` + client `usb_host_client_handle_events`
worker task with:

```c
int usbhost_start(void) {
    mp_usbh_init_tuh();              // boots TinyUSB host stack incl. DWC2 HCD
    // Spawn a task that runs mp_usbh_task() in a loop, pinned to APP_CPU.
    // The task is the sole owner of TinyUSB's host event handler.
    xTaskCreatePinnedToCoreWithCaps(usbhost_pump_task, "usbh_pump",
            USBHOST_PUMP_TASK_STACK, NULL,
            USBHOST_PUMP_TASK_PRIORITY, &s_state.pump_hdl,
            USBHOST_TASK_CORE,
            USBHOST_TASK_STACK_CAPS);
    return 0;
}

static void usbhost_pump_task(void *arg) {
    while (true) {
        mp_usbh_task();   // calls tuh_task() until queue empty
        // mp_usbh_task uses a notification mechanism in upstream PR #7;
        // if it returns when there is no work, vTaskDelay 1 tick is a
        // safe fallback.
    }
}
```

The single pump task replaces both the daemon and the worker. TinyUSB
events (URB completions, hot-plug) are routed through `tuh_task()`;
`mp_usbh_task` wraps it.

### Hot-plug

Implement `tuh_mount_cb(uint8_t dev_addr)` and `tuh_umount_cb(uint8_t
dev_addr)` (TinyUSB calls these when enumeration completes / device
unplugs). `mp_usbh.c` already defines weak-or-overridable versions for
its `machine.USBHost` Python API; we need to hook in alongside or
replace them depending on whether they're weak.

In `tuh_mount_cb`:
1. `tuh_descriptor_get_device_sync(dev_addr, &dev_desc, ...)` to populate
   `usbip_dev_record_t`.
2. `tuh_descriptor_get_configuration_sync(dev_addr, 0, cfg_buf, sizeof(cfg_buf))`
   to fetch the config descriptor blob.
3. Walk the config descriptor (preserve the existing `parse_config_desc`
   from `usbhost.c`; it's TinyUSB-agnostic data parsing).
4. Allocate a slot in `s_state.devices[]` (same as today).
5. For each non-zero endpoint, build a `tusb_desc_endpoint_t` and call
   `tuh_edpt_open(dev_addr, &desc_ep)` to claim the pipe. Store the
   `tuh_edpt_open` success/fail per endpoint.

`tuh_umount_cb` clears the slot. Hub class is filtered by checking
`bDeviceClass == TUSB_CLASS_HUB` per §11.4.

### Async submit

Replace `usbhost_submit_async`'s body. Each call:

1. Resolve `dev_addr` from `busid` (we map busid 1-N to dev_addr in
   the slot table).
2. Allocate an `inflight_t`-equivalent struct holding the user
   callback, context, and a small buffer for the URB payload.
3. Build a `tuh_xfer_t`:
   - `daddr`, `ep_addr`
   - `buffer`, `buflen`
   - `complete_cb` -> our wrapper that invokes the user callback
   - `user_data` -> our `inflight_t*`
4. Call `tuh_edpt_xfer(&xfer)` (or `tuh_control_xfer(&xfer)` for EP0).
   Both are non-blocking; completion is delivered via `complete_cb`.
5. Return 0 on success, `-EIO` on submit failure.

The completion callback runs in TinyUSB host task context. Same
priority constraint as the IDF callback shape: short, just push to
the responder queue and return.

### Cancel

Replace `usbhost_cancel_ep`'s halt+flush+clear with
`tuh_edpt_abort_xfer(daddr, ep_addr)`. TinyUSB delivers the cancelled
xfer via the same `complete_cb` with `result = XFER_RESULT_FAILED` or
similar; the responder picks it up and emits RET_SUBMIT with
-ECONNRESET via the existing override path.

### Tunables

Add `tusb_config_host_overrides.h` (location TBD; likely a new file
co-located with the IDF tinyusb_port config it overrides):

```c
// Disable TinyUSB's built-in class drivers so they don't claim
// our DUT's CDC/MSC/HID interfaces. We forward URBs raw via
// tuh_edpt_xfer.
#define CFG_TUH_CDC      0
#define CFG_TUH_MSC      0
#define CFG_TUH_HID      0
// Keep CFG_TUH_HUB at upstream default (1); we filter hubs in
// tuh_mount_cb.
// Keep CFG_TUH_VENDOR at upstream default (0).
// CFG_TUH_DWC2_DMA_ENABLE stays 0 (default) per §11.5; ESP32-S3 DMA
// path through TinyUSB is not validated.
```

The override needs to be picked up by the build before `tusb_config_host.h`
defaults set the values. Check whether the upstream PR #7 has an
overrides hook; if not, set the defines via cmake `target_compile_definitions`
in our board cmake or in a c_modules cmake fragment.

## Files touched

- `src/c_modules/usbhost/usbhost.c` (~600-800 line rewrite of the
  backend; comments and structure heavy)
- `src/c_modules/usbhost/usbhost.h` (no API change; possibly add
  include of tusb.h if needed by inline helpers)
- `src/c_modules/usbhost/micropython.cmake` (link/include paths for
  TinyUSB host)
- New file: `src/c_modules/usbhost/tusb_config_host_overrides.h`
  (or wherever the build picks up overrides cleanly)
- Possibly `src/boards/ESP32_S3_ANNEALAGE_POD/mpconfigboard.cmake` to wire
  the override file into the build.

NOT touched: `usbip_server.c`, `usbhost.h` API, board manifest, Python
package, IDF version pin.

## Implementation order

Five steps, one commit per step on a worktree branch. Build + flash +
smoke between every commit.

### Step 1: skeleton + class-driver disable + init

Add `tusb_config_host_overrides.h` disabling CFG_TUH_CDC/MSC/HID.
Wire it into the build. Replace `usbhost_start` to call
`mp_usbh_init_tuh` + spawn a pump task. Stub everything else: device
table empty, all transfer functions return -ENODEV.

Goal: firmware boots, TinyUSB host stack runs, no USB host activity
(USB/IP attach should fail with "no devices"). Smoke 5/0 should pass
because the synthetic-only path still works.

Build, flash, verify boot logs show TinyUSB host starting cleanly,
no IDF `usb_host_*` symbols still being called from usbhost.c.
Commit.

### Step 2: enumeration via tuh_mount_cb / tuh_umount_cb

Implement the hot-plug callbacks. Read device + config descriptors
synchronously, populate `s_state.devices[]`, open each non-zero
endpoint via `tuh_edpt_open`. On unmount, clean up.

Goal: `usbip list -r 192.168.0.166` shows the Pico CDC after attach.
USB/IP DEVLIST works. No URBs are forwardable yet (transfers still
stubbed).

Build, flash, validate enumeration. Smoke probably regresses (no URB
path), so partial validation: just check `usbip list` returns the
device. Commit.

### Step 3: control transfers via tuh_control_xfer

Implement EP0 path via `tuh_control_xfer`. The EP0 path is needed
for USB/IP attach handshake (kernel sends GET_DESCRIPTOR / SET_CONFIG
control transfers post-attach).

Goal: `sudo usbip attach -r ... -b 1-1` succeeds. `/dev/ttyACM*`
appears. mpremote `exec 'pass'` may or may not work depending on
bulk path.

Build, flash, validate attach. Commit.

### Step 4: bulk + interrupt transfers via tuh_edpt_xfer

Implement non-EP0 transfers. Cancel via `tuh_edpt_abort_xfer`.

Goal: full functional parity with R23. Smoke 5/0, mpremote 30/30,
R18 t+1s probe, concurrent attach all pass.

Build, flash, full validation matrix. Commit.

### Step 5: throughput probe + cleanup

Run `cdc_throughput.py` against the TinyUSB-backed firmware.
Compare with R23 baseline (~11 KiB/s) and direct-USB ceiling
(~700 KiB/s). Document outcome in `r24-findings.md`.

If throughput improves significantly, celebrate and finalise.
If throughput stays at 11 KiB/s, the FS bulk on DWC2 ceiling is a
hardware/stack constraint regardless of which stack drives it; no
additional firmware effort will move it.

Either way: clean up dead IDF `usb_host_*` includes, update doc
comments, update `docs/esp32-s3/design/usbhost.md` §11 to reflect that the
pivot is now complete.

Commit.

## Validation

R20 regression matrix at end of step 4:

| Check | What |
|-------|------|
| A | `bash test/integration/phase3/run.sh 192.168.0.166` -> 5 passed, 0 failed |
| B | mpremote 30/30 back-to-back, 0.5 s gap |
| C | `usbip detach -p 0; sleep 1; usbip list -r ... | grep -c` -> 2 |
| D | concurrent CDC + CMSIS-DAP attach |
| E | stress 60/60 |
| F | `cdc_throughput.py` (informational; the headline experiment) |

Plus build-side checks:

- Firmware binary boots cleanly on the ESP32-S3 (no IDF panic).
- No symbol clash between TinyUSB host (in MicroPython submodule) and
  IDF `usb_host_*` (which is also linked into the firmware via
  `IDF_COMPONENTS`). Per `docs/esp32-s3/design/usbhost.md` §11 the two stacks
  coexist in the build because only one calls into the DWC2 HCD at
  runtime; we need to verify TinyUSB takes ownership of the controller
  and IDF `usb_host_install` is NOT called anywhere on the boot path.
- mp_usbh's class-driver code compiles with CFG_TUH_CDC/MSC/HID=0.
  Some versions of TinyUSB host source require at least one class
  driver to be present; the override may need to keep CFG_TUH_VENDOR
  enabled or define a stub class.

## Caveats and risks

1. **TinyUSB host on DWC2 may not improve throughput.** The R23
   diagnosis pinned the bottleneck at ~88 URBs/sec/pipe inside the
   IDF host stack on DWC2. TinyUSB on DWC2 might hit the same wall
   (DMA disabled, PIO mode for FS bulk). If step 5 throughput stays
   at 11 KiB/s, that's the actual hardware/stack ceiling for FS bulk
   on this MCU and a board pivot (P4 with HS USB or Ethernet) is the
   only remaining lever. R24 still pays for the upstream-alignment
   reason regardless of throughput.

2. **The two stacks coexist in the build.** With `MICROPY_HW_USB_HOST=1`
   the IDF builds tinyusb host AND the IDF `usb_host` component is
   in `IDF_COMPONENTS`. Both call into the DWC2 controller. Today
   only the IDF stack runs because that's what `usbhost.c` calls.
   After R24 only TinyUSB runs because `usbhost.c` calls it. Verify
   the IDF `usb_host_*` symbols are linked but never executed at
   runtime (no init call). Long-term the IDF component should be
   removed from the build but that is `IDF_COMPONENTS` reordering and
   not strictly necessary for R24.

3. **mp_usbh.c registers tuh_mount_cb / tuh_umount_cb already** for
   the `machine.USBHost` Python API. Our `usbhost.c` needs to
   override those (they are likely declared with `__attribute__((weak))`
   in mp_usbh; if not, we may need to disable mp_usbh.c compilation
   or call into mp_usbh's hook from our hook). Verify by reading
   `shared/tinyusb/mp_usbh.c` carefully before step 2.

4. **Cancel via `tuh_edpt_abort_xfer` may not synthesise a callback
   in all TinyUSB versions.** Check the TinyUSB submodule pin's
   abort-then-callback behaviour. If the abort path doesn't fire
   `complete_cb`, the responder will never see the cancelled URB,
   the slot will leak, and the read loop will time out on the
   `cancel_done_sem` 250 ms ceiling. Follow-up timer would be needed.
   Test this specifically with mpremote interrupting a long-pending
   bulk-IN read.

5. **Composite-device handling.** For a CDC device the kernel
   exposes one bulk pair plus an interrupt-IN. We need to claim
   all three endpoints via `tuh_edpt_open` at mount time. Failure
   to open any one endpoint should NOT block the device from
   attaching for the other endpoints (the device may still work for
   what's actually used). Match the existing IDF backend's
   "lazy interface claim" semantics where possible.

6. **The IDF `usb_host` driver and TinyUSB host driver may both
   register interrupt handlers on the same DWC2 IRQ.** This caused
   the original P3.0 first-compile failure (the "two USB stacks
   compete for IRQ" problem). The PR #7 `--wrap=hcd_event_handler`
   link option is supposed to handle this. Verify the wrap is in
   place in our build before assuming it works.

7. **Stack budget**. The TinyUSB host pump task adds one TCB and a
   stack (~8 KB PSRAM). The pump replaces the IDF daemon + worker
   (two tasks, two stacks). Net should be roughly equal.

8. **The synchronous wrappers** (`usbhost_bulk_transfer`,
   `usbhost_interrupt_transfer`, `usbhost_control_transfer`) need to
   be ported too. They are used by the synthetic-device path? No;
   synthetic devices never enter `usbhost.c`. They might be used by
   tests. Rebuild them on top of `usbhost_submit_async` + a local
   sem (same shape as today). Trivial.

## Out of scope

- HZ change (still 100; we already know that's not the bottleneck
  per R23 verification).
- Removing the IDF `usb_host` component from the build entirely
  (separate cleanup PR).
- Removing the TCP coalescing / R23 changes (they're not the
  bottleneck but they're also not harmful; structural improvements).
- Implementing TinyUSB's `machine.USBHost` Python API in
  `annealage_pod.boot.up()` (mp_usbh exposes it but we don't need it).

## Findings file expectations

`test/integration/phase3/r24-findings.md` should match R20's findings
shape:

- branch name, commits added in order
- per-step build/flash/test results  
- regression matrix (A-F) at step 4 (the functional-parity step)
- throughput numbers from step 5: `cdc_throughput.py` table at
  bufsize=256..16384 vs R23 baseline AND vs the ~700 KiB/s
  direct-USB ceiling
- bisect notes if any step regressed
- caveats observed (esp. if any of caveats 1-7 above materialised)
- iteration budget used

If step 5 throughput stays at 11 KiB/s, document the conclusion that
FS bulk on DWC2 ESP32-S3 is the hardware ceiling regardless of host
stack, and recommend the spec.md ESP32-P4 + Ethernet pivot for
streaming workloads.
