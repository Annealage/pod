# Plan: TinyUSB usbipd-passthrough example (STM32F429/F439 Nucleo)

Status: planning. Target output is a clean-room minimal usbipd
server inside `examples/host/` of TinyUSB upstream, runnable on
STM32F4 Nucleo-144 boards with built-in RMII Ethernet, suitable
for filing as an upstream PR alongside (or following) PR 3 + PR 4.

## Goals

1. **Demonstrator.** Give TinyUSB users a turnkey reference for
   USB-host-over-IP forwarding. Plug a USB device into the
   Nucleo, run `sudo usbip attach -r <ip> -b 1-1` from a Linux
   host, use the device as if it were attached locally.

2. **Test bed for PR 3 + PR 4.** The cdc-acm cleanup wedge and
   the control-xfer-no-timeout bug both reproduce on this
   example without our patches; they disappear when PR 3 + PR 4
   are applied. Reviewers can verify the patches resolve real
   regressions, not theoretical races.

3. **Independent of mpy-pod.** No FreeRTOS-on-ESP32 dependency,
   no usbip multi-device or virtual-device support, no lane
   pipelining beyond depth=1. Pure C, lwIP raw API, single
   physically-attached device. Anything more belongs in a
   downstream project, not an upstream example.

## Why STM32F429/F439 Nucleo

* **Same DWC2 host IP** as our ESP32-S3 setup. PR 3 fix lives in
  `hcd_dwc2.c` which is shared across the entire DWC2 host port
  family (F4 / F7 / H7 / ESP32-S3 / GD32 / various RP2350 host).
  An F4 Nucleo exercises the exact same code path as our bench.

* **Built-in RMII Ethernet.** LAN8742A PHY soldered to the
  board, no shield or external module needed. Reviewers can
  bench-test with one cable.

* **Built-in ST-Link.** No external programmer; flash via USB.

* **Mainstream availability.** F429ZI Nucleo is stocked at every
  distributor; same for F439ZI. F429 ~$AU40, F439 ~$AU45.

* **`stm32f439nucleo` BSP already exists** in TinyUSB. F429 and
  F439 are pin-compatible (F439 = F429 + crypto). F4 family
  member differences are not exercised by either USB OTG or
  Ethernet, so the same firmware binary runs on both. Either
  add a fresh `stm32f429nucleo` BSP (5-line copy of stm32f439
  with linker rename), or use stm32f439nucleo unchanged.

* **FullSpeed only on F429ZI's OTG_FS port.** 12 Mbps. Plenty
  for cdc-acm; not enough to make `USBIP_PIPELINE_DEPTH=1`
  throughput visibly painful. Considered an H743 Nucleo for
  HighSpeed but that requires an external ULPI PHY board
  (USB3300 dev board is the standard companion); adds a
  hardware dependency that hurts reproducibility.

## Branch strategy

A separate branch on the lib/tinyusb fork:
`r27-usbipd-example` based off upstream master (NOT off the
PR 3 / PR 4 branches). This lets the example be tested on stock
TinyUSB first - confirming the baseline failure modes - then
PR 3 + PR 4 cherry-picked on top to confirm the fix.

Test sequence:

1. Build on stock master + r27-usbipd-example. Bench: cdc-acm
   close storm should wedge or leak. Captures the **failing**
   behaviour PR 3 + PR 4 are claimed to fix.
2. Cherry-pick PR 3 + PR 4 onto the example branch.
3. Re-bench. Same scenario should pass cleanly.
4. The diff between the two states is the upstream pitch.

This validates that PR 3 + PR 4 are necessary (not just
sufficient) for the example to work robustly, which is the
strongest possible case for upstream merge.

The mpy-pod branch already has PR 3 + PR 4 verified at 60/60
PASS. Adding the example to that same branch would conflate
two concerns. Keep them separate.

## Architecture and file layout

Target tree under `examples/host/usbipd/`:

```
CMakeLists.txt           # build glue, lwip/eth driver wiring
Makefile                 # legacy non-cmake build (mirrors net_lwip_webserver)
skip.txt                 # exclude boards without USB host + Ethernet
src/
  main.c                 # bring-up: lwip init, dhcp, tcp listen, tuh_init, main loop
  usbipd_server.c        # accept loop, OP_REQ_DEVLIST/IMPORT, URB streaming
  usbipd_server.h
  usbipd_proto.h         # USBIP protocol types, byte-order packing
  usbipd_proto.c         # pack/unpack helpers
  usb_forward.c          # tinyusb host integration: mount/umount, urb pump,
                         #   abort/timeout. depth=1 lane semantics inline.
  usb_forward.h
  lwipopts.h             # lwip configuration
  tusb_config.h          # tinyusb host config
  eth_glue/              # subdirectory: minimal stm32f4 eth -> lwip netif glue
    eth_netif.c
    eth_netif.h
    stm32f4_eth_hal.c    # thin wrapper over HAL_ETH_*
```

Estimated total: ~1500-2000 lines of new C code.

## Component design

### main.c

```c
int main(void) {
  board_init();
  printf("usbipd example\n");

  eth_netif_init();              // brings up MAC + LAN8742A, registers lwip netif
  dhcp_start(eth_netif_get());   // lwip DHCP client
  tuh_init(BOARD_TUH_RHPORT);    // tinyusb host
  usbipd_server_init();          // tcp listen on 3240

  while (1) {
    tuh_task();
    sys_check_timeouts();        // lwip timer tick
    usbipd_server_poll();        // accept connections, drive URB pump
    HAL_ETH_Process();           // descriptor reclaim, RX dispatch
  }
}
```

No RTOS - polled main loop. lwIP raw API (no socket layer) so
no Netconn / FreeRTOS dependency. tuh_task() runs from the same
thread as lwIP TCP polling; serialisation via single-thread
discipline.

### usbipd_proto.{h,c}

Pure protocol definitions. Big-endian network byte order. Five
message types we need to support:

```c
typedef struct {
  uint16_t version;       // 0x0111
  uint16_t code;          // OP_REQ_DEVLIST, OP_REQ_IMPORT, etc.
  uint32_t status;
} __attribute__((packed)) usbip_op_header_t;

typedef struct {
  uint32_t cmd;           // CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK
  uint32_t seqnum;
  uint32_t devid;
  uint32_t direction;     // 0=OUT, 1=IN
  uint32_t ep;
} __attribute__((packed)) usbip_urb_header_t;
// ... transfer_flags, transfer_buffer_length, start_frame, number_of_packets,
// interval, setup[8] for SUBMIT; status, actual_length, error_count for RET.
```

Pack/unpack helpers handle htonl/ntohl + struct fill.

### usbipd_server.{h,c}

State machine driven by TCP recv. Three connection states:

* `STATE_OP_PHASE`: just accepted, waiting for OP_REQ_DEVLIST
  or OP_REQ_IMPORT.
* `STATE_IMPORTING`: OP_REQ_IMPORT received, looking up device
  descriptor cache, sending OP_REP_IMPORT.
* `STATE_URB_STREAMING`: post-import. read_exact pulls
  USBIP_CMD_SUBMIT or CMD_UNLINK headers, dispatches to the
  forwarder.

Per-connection state struct:

```c
typedef struct {
  struct tcp_pcb *pcb;
  enum conn_state state;
  uint32_t  imported_devid;       // set once IMPORT succeeds
  inflight_t *inflight_head;      // simple linked list, depth=1 means
                                  // typically 1-2 entries
  // recv buffer: pbufs accumulate; we pull headers + payloads via
  // read_exact-equivalent that drains pbufs lazily.
  pbuf *recv_q;
  uint32_t  recv_q_offset;
} conn_t;
```

Singleton: one conn_t (we accept one client at a time, hard-fail
attempts to import while another client is connected).

### usb_forward.{h,c}

TinyUSB integration, handling URB lifecycle.

Single-device cache populated by `tuh_mount_cb`:

```c
typedef struct {
  uint8_t daddr;
  bool present;
  tusb_desc_device_t        device_desc;
  uint8_t                    config_desc[256];
  uint16_t                   config_desc_len;
  uint8_t                    interface_count;
  uint8_t                    endpoint_count;
} forwarded_device_t;
```

Per-EP submission slot (PIPELINE_DEPTH=1):

```c
typedef struct {
  inflight_t   *current;          // URB in flight at TinyUSB level
  bool          busy;             // PR2-bug-equivalent: tuh_edpt_xfer rejects
                                  // concurrent submits
} ep_slot_t;
ep_slot_t ep_slots[USB_MAX_EP];
```

URB lifecycle:

1. `usbipd_server` parses CMD_SUBMIT, allocates `inflight_t`,
   calls `usb_forward_submit(conn, urb)`.
2. `usb_forward_submit` blocks if the EP slot is busy. With
   depth=1 this is a tight wait inside the main loop;
   `tuh_task()` and `sys_check_timeouts()` get called between
   each poll. Acceptable for an example.
3. Submit completes via `tuh_xfer_cb`. Result + actual_len
   stored in inflight, RET_SUBMIT queued for TCP send.
4. CMD_UNLINK: parse, look up inflight by seqnum, call
   `tuh_edpt_abort_xfer`. Wait for natural completion callback
   to fire (PR 3 makes this reliable). RET_UNLINK sent after
   the abort completion lands.

Timeout discipline:

* Control transfers from CMD_SUBMIT go through `tuh_control_xfer`
  with `xfer.timeout_ms = 1000` (1 second). A NAKky device gets
  `XFER_RESULT_FAILED` returned to the kernel via
  RET_SUBMIT(-EPROTO), bridge stays alive.
* Bulk/interrupt transfers go through `tuh_edpt_xfer` (no
  timeout API on that side; relies on the kernel cdc-acm /
  user-space process to give up). Watchdog reset of the
  endpoint considered as a follow-up.

### eth_glue/

Minimal STM32F4 Ethernet driver bridged to lwIP raw API.

```c
err_t eth_netif_init(void);
struct netif *eth_netif_get(void);
void eth_netif_link_check(void);   // poll PHY link status
void HAL_ETH_Process(void);         // RX descriptor reclaim + dispatch
```

Implementation outline:

* Enable the GPIO clocks for RMII pins (per board.h pinmap),
  configure as AF11 (ETH).
* Initialise HAL_ETH peripheral in RMII mode at 100 Mbit
  (`HAL_ETH_Init`).
* Start LAN8742A PHY: read MII registers via SMI, kick auto-
  negotiation, wait for link up.
* Allocate static TX/RX descriptor rings + buffers in DTCM RAM
  (faster than SRAM2 for DMA on F4).
* Register lwIP netif with `eth_input` for received frames,
  `low_level_output` for transmit.
* Hook the Ethernet IRQ to wake the main loop (or just poll
  HAL_ETH_Process from main).

Reference: STM32CubeF4's `LwIP_TCP_Echo_Server` example is
exactly this pattern. We borrow the structure but rewrite for
clarity (cube examples are heavy on conditional compilation).

CubeF4 is BSD 3-clause; compatible with TinyUSB MIT.

### Build system integration

Mirror `examples/device/net_lwip_webserver/CMakeLists.txt`. Key
sections:

* `set(LWIP ${TOP}/lib/lwip)` - lwIP submodule under TinyUSB tree.
* `target_sources` for our usbipd_server.c, usb_forward.c,
  eth_glue/*.c, plus lwIP core sources (`tcp.c`, `pbuf.c`,
  `dhcp.c`, `ip4.c`, `etharp.c`, `init.c`, `mem.c`, `memp.c`,
  `netif.c`, `udp.c` for DHCP, `def.c`, `inet_chksum.c`,
  `ip4_addr.c`, `ip4_frag.c`, `timeouts.c`).
* `target_compile_definitions` to enable `HAL_ETH_MODULE_ENABLED`
  in stm32f4xx_hal_conf.h (or override the conf with our own).
* `target_link_libraries` against board_libs as the existing F4
  examples do.

Skip-list (`skip.txt`): exclude every BSP that doesn't have
both DWC2 host AND a wired Ethernet path the example knows
about. For now: include only `stm32f439nucleo` (and maybe
`stm32h723nucleo` once the eth driver is generalised).

## Phased deliverables

### Phase 1: Build skeleton, no networking

Goal: get a "Hello, world" host example building for
stm32f439nucleo with our directory structure, using only stock
TinyUSB master.

* Directory + skeleton main.c that prints "usbipd example" over
  the ST-Link VCOM UART.
* `tuh_init` + tuh_task loop, no networking yet.
* Plug a USB device, verify `tuh_mount_cb` fires.

Deliverable: ~150 lines, builds clean on stm32f439nucleo. Run
on hardware with `flash-nucleo-f429` skill.

### Phase 2: lwIP DHCP + TCP echo

Goal: bring up the Ethernet stack, get a DHCP lease, listen on
port 3240 and echo bytes.

* Add `eth_glue/`.
* Add lwIP sources to CMakeLists.
* Add `tcp_listen` on port 3240, accept handler, recv handler
  that just echoes.
* Verify from Linux: `nc <ip> 3240` produces an echo loop.

Deliverable: ~500 lines, builds clean, DHCP gets a lease, echo
test passes.

### Phase 3: USBIP OP_REQ_DEVLIST + OP_REQ_IMPORT

Goal: respond to `usbip list -r <ip>` and `usbip attach -r <ip>
-b 1-1`. No URB streaming yet; the attach should succeed and
then the connection idles.

* USBIP protocol header parser/builder.
* Device descriptor cache populated from `tuh_mount_cb`.
* OP_REQ_DEVLIST handler walks the cache.
* OP_REQ_IMPORT handler claims the device.

Deliverable: `usbip list` shows the attached device, `usbip
attach` succeeds (creates a `/dev/sd*` or `/dev/ttyACM*`
post-attach, depending on device class). Linux kernel will
likely send a control xfer immediately and time out because we
don't handle URBs yet - acceptable failure mode for this phase.

### Phase 4: cdc-acm URB streaming + UNLINK handling

Goal: forward a cdc-acm device (Pico W on FullSpeed). Same
device class we test on ESP32-S3 mpy-pod. Combines what was
originally split as HID-then-cdc-acm; the user's available
hardware is Pico W only, so cdc-acm is both the simplest
available class workload and the target test bed in one phase.

cdc-acm exercises the full URB matrix:

* Control: `SET_LINE_CODING`, `SET_CONTROL_LINE_STATE`,
  `GET_LINE_CODING` issued by `cdc-acm.ko` at open time. Goes
  through `tuh_control_xfer` with `xfer.timeout_ms = 1000`.
  This is where PR 4's missing-timeout bug previously wedged
  the bridge against a NAKky device.
* Bulk-IN: `cdc-acm` anchors typically 16 read URBs. These go
  through `tuh_edpt_xfer` with depth=1 lane discipline.
* Bulk-OUT: write URBs from `mpremote` raw-repl exchanges.
* Interrupt-IN: cdc-acm modem-status notifications. Same pump
  path as bulk-IN.
* Cancel storm: `cdc_acm_close` issues `usb_kill_urb` on every
  anchored URB on tty close. Each becomes a `CMD_UNLINK` to
  the device. The bridge calls `tuh_edpt_abort_xfer` and
  expects the natural completion to fire so it can send
  `RET_SUBMIT(-ECONNRESET)` + `RET_UNLINK`. Without PR 3 the
  callback never fires; URBs leak on the device side and the
  kernel hangs in `usb_poison_urb` D-state.

Implementation:

* `CMD_SUBMIT` handler routes by EP type:
  * EP 0 control -> `tuh_control_xfer(.timeout_ms = 1000)`.
  * Bulk / Interrupt -> `tuh_edpt_xfer`.
  * ISO -> not supported in this example, return
    `RET_SUBMIT(-EXDEV)`.
* Per-EP slot tracking with depth=1 (`USBIP_PIPELINE_DEPTH=1`).
* `CMD_UNLINK` handler: look up inflight by seqnum, call
  `tuh_edpt_abort_xfer`, wait for natural completion (PR 3
  makes this reliable), send `RET_SUBMIT(-ECONNRESET)` then
  `RET_UNLINK` in the order the kernel expects.
* `tuh_xfer_cb` dispatch: copy result + payload into a
  `RET_SUBMIT` pbuf, queue TCP send.

**Deliverable on stock master** (no PR 3 / PR 4):
cdc-acm enumerates, basic read/write may work for a single
operation, but `mpremote ... resume exec` cycled-close storm
exposes:

* PR 4 missing: `tuh_control_xfer` for any control xfer the
  Pico NAKs (interrupt EP `CLEAR_FEATURE` is the classic case
  but also any control during cdc-acm open) wedges the
  bridge. The whole TCP connection becomes unresponsive.
* PR 3 missing: `tuh_edpt_abort_xfer` doesn't fire callback,
  inflight URBs leak on the bridge side, lane slot never
  frees. The kernel side hangs in `usb_poison_urb` D-state on
  the cdc_acm tty close. mpremote returns OSError stale-file-
  handle.

This is the **failure baseline**. Capture UART log + kernel
dmesg as evidence the bugs reproduce on stock TinyUSB on
different silicon (F429/F439) than the original ESP32-S3
discovery, ruling out any platform specificity.

### Phase 5: Cherry-pick PR 3 + PR 4 from r27 branch

Goal: confirm the same cdc-acm cycled-close test that wedges
in Phase 4 passes cleanly with the patches.

* `git cherry-pick r27-fix-txfifo-recheck~1..r27-fix-txfifo-recheck`
  (the two commits from our local PR 3 + PR 4 work).
* Rebuild, re-flash, re-run cdc-acm cycled test.
* Expected: 30/30 PASS, no wedge, no leak.

This is the **success baseline** that demonstrates PR 3 + PR 4
are necessary AND sufficient for the example.

### Phase 6: Documentation, README, upstream PR

Goal: file the example PR.

* README under `examples/host/usbipd/README.md` covering:
  * What the example does.
  * Hardware needed (Nucleo + USB device + Ethernet cable).
  * Quick start (build, flash, attach from Linux).
  * Throughput note: USBIP_PIPELINE_DEPTH=1 ceiling per
    docs/spec.md §4.5.1.
  * Reference to PR 3 + PR 4 as required prerequisites for
    cdc-acm class devices (cite issue/PR numbers once filed).
* Push to upstream fork.
* Open PR against `hathach/tinyusb` master.

## Test methodology

Bench setup:

* Nucleo F429ZI or F439ZI with USB-C to ST-Link USB.
* Ethernet cable from Nucleo RJ45 to LAN.
* USB device plugged into OTG_FS port (CN13 user USB header on
  the Nucleo, NOT the ST-Link USB).
* Linux host on the same LAN with `usbip` package installed
  (`apt install usbip` on Debian/Ubuntu).

Per-phase test:

```bash
# Build
cd examples/host/usbipd/build
cmake -DBOARD=stm32f439nucleo -DCMAKE_BUILD_TYPE=Debug ..
ninja

# Flash
ninja flash      # uses st-flash via TinyUSB BSP integration

# Open ST-Link UART for log
screen /dev/ttyACM0 115200

# Once DHCP lease comes up (visible in log), from Linux host:
usbip list -r <nucleo_ip>
sudo usbip attach -r <nucleo_ip> -b 1-1
ls /dev/serial/by-id/   # should show the forwarded device
mpremote connect /dev/ttyACM<n> exec "print('hello')"

# Stress test (Phase 5/6 only):
for i in $(seq 1 30); do
    timeout 30 mpremote connect /dev/ttyACM<n> resume exec "print('iter $i')"
done

# Recovery if wedge:
sudo usbip detach -p 0
```

Success criteria for the upstream PR:

* All 30 iterations PASS cleanly.
* No `usb_poison_urb` warnings in `dmesg`.
* mpremote returns within 1 s per iter (no protocol timeouts).
* This is the same bench rig we used to verify PR 3 + PR 4 on
  ESP32-S3 Annealage Pod, just on different hardware.

## Open questions / risks

1. **lwIP submodule size and licensing.** lwIP is BSD 3-clause,
   compatible with TinyUSB MIT. Already pulled in for
   net_lwip_webserver. No new submodule needed.

2. **STM32 Ethernet HAL licensing.** `stm32f4xx_hal_eth.c` is
   in the STM32CubeF4 firmware package, BSD 3-clause. We need
   it linked into the binary; either copy the file into the
   example tree (with proper attribution) or include via the
   existing TinyUSB BSP submodule path
   (`hw/mcu/st/stm32f4xx_hal_driver/`). Check if HAL_ETH is in
   the existing TinyUSB stm32cubef4 submodule pin.

3. **DTCM vs SRAM for DMA descriptors.** F4 ETH DMA can access
   any RAM but DTCM has lower contention. The Nucleo linker
   script needs a `.dma_buf` section. Cube examples set this
   up; we need to mirror.

4. **Maintainer reception of lwIP-dependent example.** The
   existing net_lwip_webserver example sets precedent. But that
   one is device-side (RNDIS NIC) and has a captive lwIP
   instance. Ours runs lwIP on physical Ethernet, more
   ambitious. Maintainers may push back on adding a real-
   Ethernet integration to TinyUSB upstream rather than to
   stm32cube examples.

5. **Single client only.** USBIP allows one importer at a time
   per device anyway (that's how the protocol works). But
   coding this properly requires we reject second connections
   cleanly. Depth=1 connection state simplifies this.

6. **Hot-plug.** If the user unplugs the USB device while a
   client is attached, we should send RET_SUBMIT(-ENODEV) for
   any in-flight URBs and close the TCP connection. `tuh_umount_cb`
   triggers this.

7. **MAC address.** Nucleo doesn't have a unique MAC. Need to
   derive one from the F4 unique ID register (96-bit serial),
   or hard-code with an OUI from the locally-administered
   range.

8. **Endianness.** F4 is little-endian; USBIP wire protocol is
   big-endian. All packing/unpacking goes through htonl/ntohl
   helpers. Easy to get wrong; pack helpers should have
   structured tests.

9. **Watchdog vs no-watchdog.** Our mpy-pod version has a
   watchdog task that catches stuck URBs. The example should
   skip the watchdog initially - if PR 3 + PR 4 are doing
   their job, no URB should get stuck. If reviewers ask "what
   if a URB does get stuck", we add a section in README about
   the trade-off.

10. **F429 vs F439 Nucleo BSP.** Decide before Phase 1: ship
    against existing stm32f439nucleo (works on both silicon),
    or add stm32f429nucleo BSP. Either is upstream-acceptable;
    F439 unchanged is less work.

## Deliverables for this plan

Just this document. Implementation gated on user approval.
After approval, Phase 1 lands first, each subsequent phase
gated on the previous one being verified on hardware.

## Cross-references

* `docs/spec.md` §4.5.1 - USBIP_PIPELINE_DEPTH=1 limitation
  reasoning, citable in upstream PR body.
* `test/integration/phase3/r27-pr34-shipped.md` - PR 3 + PR 4
  status and bisection history.
* `test/integration/phase3/r27-pr3-abort-callback.patch` and
  `r27-pr4-control-timeout.patch` - the exact patches to
  cherry-pick in Phase 6.
* `test/integration/phase3/r27-upstream-pr-draft.md` - drafted
  PR bodies for PR 1 / PR 2 / PR 3 / PR 4. Once filed, this
  example becomes a reproducer for PR 3 / PR 4 specifically.
