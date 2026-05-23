# R27 TinyUSB usbipd example: phase status

## Phase 1: skeleton + host bring-up - VERIFIED on hardware

Branch: `r27-usbipd-example` in `lib/tinyusb` submodule (off upstream
master 3af1bec1a, no PR 3 / PR 4 applied).

Tip: `07d57bde9` examples/host/usbipd: Phase 1 skeleton.

Build: clean for stm32f439nucleo, ~22 KB FLASH, ~4 KB RAM.

Bench: STM32F429ZI Nucleo-144 (SN 066CFF495177514867213407,
registered as `nucleo-f429` in mpy-dev), Pico CMSIS-DAP probe
plugged into the OTG_FS host port (CN13 user USB).

UART log:
```
usbipd example, phase 1: host bring-up
  rhport=0  speed=FS
tusb_init ok, waiting for device...
MOUNT: daddr=1 rhport=0 speed=FS vid=0x2e8a pid=0x000c
```

Confirms: TinyUSB host stack on the F4 family DWC2 OTG_FS IP works
end-to-end. The Pico CMSIS-DAP enumerates without any class drivers
(CFG_TUH_CDC/HID/MSC/VENDOR all 0); Phase 4 onwards forwards URBs at
the endpoint level via tuh_edpt_xfer / tuh_control_xfer.

Lessons captured for the next phases:

* The deprecated `tuh_init(rhport)` wrapper is **not** a drop-in
  replacement for the current `tusb_init(rhport, &init)`. With the
  wrapper the F4 BSP's tuh_init() hung indefinitely; bare_api works
  because it uses the explicit form. Future phases keep
  `tusb_init` + `board_init_after_tusb()` (that hook enables the
  OTG VBUS power switch on PG6 of the Nucleo-144).

* tusb_config: `CFG_TUH_HUB=1` even though we don't need hub
  topology. Setting it to 0 led to a build that linked but the
  controller bring-up was nonfunctional in our test runs;
  matching bare_api's HUB=1 / DEVICE_MAX=4 avoided the path.
  Could be a pre-existing TinyUSB upstream bug worth a separate
  bisect, but out of scope for this example PR.

* `-Wno-type-limits` is needed in the example's CMakeLists when
  all class drivers are disabled (`BUILTIN_DRIVER_COUNT == 0`
  trips the always-false comparison `drv_id < 0` in
  src/host/usbh.c get_driver()). Suggests an upstream cleanup
  but again out of scope.

## Phase 2: lwIP DHCP + TCP echo on port 3240 - VERIFIED on hardware

Branch: `r27-usbipd-example` in `lib/tinyusb`. Tip: `94c7c6465`
"examples/host/usbipd: Phase 2 (lwIP DHCP + TCP echo on port
3240)."

Build: clean for stm32f439nucleo. 49 KB FLASH (2.3% of 2 MB),
38 KB RAM (19.5% of 192 KB). The Phase 1 to Phase 2 jump is
~26 KB FLASH and ~34 KB RAM, mostly lwIP core + the HAL ETH
DMA descriptor + RX scratch buffers.

UART log on first boot:
```
usbipd example, phase 2: host + lwip + dhcp + tcp echo
  rhport=0  speed=FS
tusb_init ok
MOUNT: daddr=1 rhport=0 speed=FS vid=0x2e8a pid=0x000c
eth: up, starting dhcp
dhcp: got 192.168.0.182
listening on tcp/3240 (echo)
```

Echo round-trip from the host:
```
$ echo "hello from host" | nc -q1 192.168.0.182 3240
hello from host
```

Implementation strategy: rather than write the lwIP <-> HAL_ETH
glue from scratch, vendor ST's reference. Files pulled in
verbatim from upstream:

* `lan8742.{c,h}` from
  `https://github.com/STMicroelectronics/stm32-lan8742` - the
  canonical PHY driver that the new HAL_ETH API plugs against
  via a function-pointer struct (`lan8742_IOCtx_t`).
* `ethernetif.{c,h}` from
  `STM32CubeF4/Projects/STM324x9I_EVAL/Applications/LwIP/`
  `LwIP_TCP_Echo_Server` - the canonical raw-API integration.
* `tcp_echoserver.{c,h}` from the same project.

Surgical edits applied to the vendored ethernetif.c:

* Include path: `Components/dp83848/dp83848.h` ->
  `lan8742/lan8742.h`.
* `HAL_ETH_MII_MODE` -> `HAL_ETH_RMII_MODE`. F4x9I-EVAL is wired
  for full MII; the F429ZI Nucleo-144 routes only the 9 RMII
  signals from the LAN8742A.
* `dp83848_*` identifiers replaced by `lan8742_*`. Same
  `_Object_t` / `_IOCtx_t` / `_Init` / `_GetLinkState` API
  surface, mechanical swap.
* MspInit GPIO block replaced with the F429ZI Nucleo-144 RMII
  pinout (PA1/PA2/PA7, PB13, PC1/PC4/PC5, PG11/PG13).
* `ETH_TxPacketConfig` -> `ETH_TxPacketConfigTypeDef`. The
  vendored eval source still uses the older spelling; the HAL
  driver shipped in TinyUSB's submodule is the renamed version.
* `ETH_MAC_ADDR0..5` macros (board-specific in the eval
  project's main.h) replaced with a derivation from the F4
  96-bit unique device ID at `UID_BASE`. Locally-administered
  bit set so each board gets a stable distinct MAC without a
  board.h define.

Build glue:

* `HAL_ETH_MODULE_ENABLED` is added by the example's
  CMakeLists.txt because the F4 BSP's stm32f4xx_hal_conf.h
  leaves it commented out by default - it's USB-focused, has
  no reason to ship the ETH module.
* `stm32f4xx_hal_eth.c` is added to the target sources rather
  than the BSP library, again because it's example-specific.
* lwIP source list mirrors `examples/device/net_lwip_webserver`
  minus the device-side networking helpers.

Lessons (added to the upstream PR description when filed):

* The upstream `LwIP_TCP_Echo_Server` example for F4x9I-EVAL is
  the right starting point even for the F429ZI Nucleo, because
  it uses the modern HAL_ETH API and raw lwIP API (no
  FreeRTOS). The PHY swap is mechanical because both DP83848
  and LAN8742 component drivers expose the same IOCtx
  interface.
* The hand-written-from-memory glue I started Phase 2 with was
  about right structurally but had subtle issues around the
  TxConfig type name and TX descriptor wrap path. ST's
  reference is the safe choice.

## Phase 3: USB/IP DEVLIST + IMPORT framing - VERIFIED on hardware

Branch tip: `bbf9bc65a` "examples/host/usbipd: Phase 3 (USB/IP
DEVLIST + IMPORT framing)."

Build: 51 KB FLASH (+2 KB vs Phase 2), 38 KB RAM (no change -
the protocol header tables are static, no per-client allocation).

The Phase 2 TCP echo got replaced with a USB/IP protocol server
on the same port. Single-client at a time; second connections
get dropped on accept.

Verified end-to-end with the Linux usbip-utils:
```
$ usbip list -r 192.168.0.182
Exportable USB devices
======================
 - 192.168.0.182
        1-1: unknown vendor : unknown product (2e8a:000c)
           : /tinyusb/usb1/1-1
           : (Defined at Interface level) (00/00/00)

$ sudo usbip attach -r 192.168.0.182 -b 1-1
$ echo $?
0
$ sudo usbip detach -p 0
usbip: info: Port 0 is now detached!
```

UART log during the attach:
```
usbip: accept
usbip: IMPORT busid='1-1' daddr=1 (Phase 4: URB streaming TBD)
usbip: IMPORTED-state recv (48 bytes; Phase 4 will handle)
usbip: IMPORTED-state recv (48 bytes; Phase 4 will handle)
```

The 48 bytes is `usbip_header_t` exactly - the kernel's first
CMD_SUBMIT for the device descriptor read. Phase 4 picks these
up and routes through tuh_edpt_xfer / tuh_control_xfer.

Implementation notes:

* `tuh_descriptor_get_device_local(daddr, &desc)` returns the
  cached enumeration descriptor synchronously without bus
  access, so the lwIP TCP recv callback can populate the wire
  device entry safely. tuh_*_sync would block lwIP timers.
* Mount/umount callbacks call `usbip_server_on_mount` /
  `usbip_server_on_umount` to refresh the cached device table;
  this is the only data the lwIP path reads.
* Phase 3 hardcodes `num_interfaces = 0` in the device entry.
  Linux `usbip list` still shows the device fine; Phase 4 walks
  the configuration descriptor to populate per-interface
  classes when the URB pump goes in.
* Initial DEVLIST parsing bug: I had it consuming a phantom
  4-byte status word after op_common. The op_common struct
  itself is { version (2) + code (2) + status (4) } = 8 bytes
  total. After parsing op_common, DEVLIST has zero remaining
  payload. Fixed before merge.

## Phase 4: CMD_SUBMIT / CMD_UNLINK URB streaming - VERIFIED on hardware (failing baseline as designed)

Branch tip: `d639e3323` "examples/host/usbipd: Phase 4
(CMD_SUBMIT/UNLINK URB streaming)."

Build: 53 KB FLASH (+2 KB vs Phase 3), 54 KB RAM (+16 KB - the
new inflight slot pool of 8 x 1.5 KB buffers + per-device config
descriptor cache).

What landed:

* CMD_SUBMIT (control) routed through tuh_control_xfer with the
  setup packet copied out of the wire header.
* CMD_SUBMIT (bulk/interrupt, IN and OUT) routed through
  tuh_edpt_xfer. Endpoints opened lazily via tuh_edpt_open on
  first non-control submit; the descriptor is sniffed from the
  kernel's GET_DESCRIPTOR(CONFIG) reply as it flows through.
* CMD_UNLINK -> tuh_edpt_abort_xfer + RET_UNLINK. This is the
  PR3 territory: stock upstream's abort_xfer does not fire the
  natural completion callback, so the kernel's cancel path
  hangs.
* RET_SUBMIT built from xfer->result + actual_len in the
  completion callback, sent on the same TCP socket the kernel
  has handed to vhci_hcd via /sys/devices/.../attach.

Hardware verification (2026-05-09):

* `usbip list -r 192.168.0.182` -> the Pico shows up on 1-1
  (Phase 3 regression).
* `sudo usbip attach -r 192.168.0.182 -b 1-1` -> kernel
  attaches, dmesg shows "Device attached", full enumeration
  through our server (device descriptor, all string
  descriptors, config descriptor), cdc_acm subdriver claims
  IF1 and creates `/dev/ttyACM12`. Two real bugs found and
  squashed before this worked:

  1. tuh_control_xfer asserts `xfer->ep_addr == 0` even for
     IN-direction control transfers (the setup packet's
     bmRequestType.7 picks data direction). My initial code
     OR'd 0x80 into ep_addr for IN; switched to 0 for control
     unconditionally.
  2. tuh_control_xfer copies the xfer struct internally; the
     completion callback receives a pointer to the stack's own
     copy, not back to my `inflight->xfer`. Reading
     `inflight->xfer.actual_len` post-submit returned the
     pre-submit zero. Fixed by syncing
     `xfer->result`/`actual_len` back into the slot at the top
     of the completion callback. Guarded by an `xfer == &u->xfer`
     check that confirmed the divergence.

* First non-control SUBMIT (interrupt IN ep=0x83, the cdc-acm
  notification endpoint) goes silently in flight. Subsequent
  submits on the same EP are rejected by tuh_edpt_xfer because
  the EP is still claimed; tuh_edpt_abort_xfer (PR3 territory)
  returns true but never fires the natural completion callback,
  so the claim never releases. Phase 5 PR3 cherry-pick fixes
  this. Captured UART trace:

  ```
  usbip: SUBMIT seq=185 ep=0x83: tuh_*_xfer rejected
  usbip: SUBMIT seq=186 ep=0x83: tuh_*_xfer rejected
  ...
  usbip: UNLINK seq=201 (cancels seq=184 ep=0x83)
  usbip: SUBMIT seq=206 ep=0x83: tuh_*_xfer rejected
  ...
  ```

* Kernel side (dmesg): the cascade ends with vhci_hcd seeing
  `urb->status -104` (ECONNRESET) on the unlinked URB and the
  connection eventually closes. Detach via
  `echo 0 > /sys/devices/platform/vhci_hcd.0/detach` is clean.

This is the expected failing-baseline scenario. Phase 5 lands
the PR3+PR4 cherry-picks and the same code path passes
cleanly.

## Phase 5: PR1+PR2+PR3+PR4 cherry-picks + per-EP queue - VERIFIED

Branch tip: `6ff452183` "examples/host/usbipd: per-EP submit
queue (Phase 5 makes cdc-acm clean)."

Cherry-picks landed on top of Phase 4:
* `129372647` PR2: hcd/dwc2 re-read txsts inside per-packet FIFO
* `a152347bc` PR1: hcd/dwc2 save post-transfer PID in DMA-mode IN
* `bd54145c2` PR4: host - honour timeout_ms in tuh_control_xfer
* `e3675eaa0` PR3: hcd/dwc2 - hcd_edpt_abort_xfer fires the
  natural xfer_complete callback (applied via git am from the
  saved patch file)

Phase 5 reran the same attach test and exposed a second
bottleneck: cdc-acm queues 16 read-ahead URBs on its bulk-IN,
and TinyUSB only allows one URB per EP in flight, so URBs 2..16
came back rejected. PR3 alone fires the abort-callback for
the in-flight one but the kernel had already given up on the
storm of rejections.

Per-EP submit queue added: rejected URBs stay in the inflight
pool flagged `queued`; the completion callback's drain loop
submits the next FIFO entry when the EP frees. UNLINK against
a queued slot retires it immediately without touching the bus.
MAX_INFLIGHT bumped 8 -> 32 for the 16-deep read queue plus
headroom.

Live verification:
```
$ sudo usbip attach -r 192.168.0.182 -b 1-1
$ ls /dev/ttyACM12       # appeared
$ timeout 4 sudo cat /dev/ttyACM12 > /tmp/cdc.log
$ wc -c /tmp/cdc.log
46 /tmp/cdc.log
```

UART trace during detach is just three RET_UNLINK status=0
messages - no more rejected-cascade. dmesg shows clean
disconnect (one benign "cannot find a urb of seqnum N" from a
late UNLINK racing against the abort-callback's RET_SUBMIT;
functional behaviour is correct).

Build: 53.8 KB FLASH, 93 KB RAM (RAM +39 KB vs Phase 4 - that's
MAX_INFLIGHT 32 * 1.5 KB buffer pool).

## Phase 6: README + further testing

README landed (commit `558e5a36b` + amend `bfabae15b`):
`examples/host/usbipd/README.md`. Covers wiring, build, the
attach/detach flow, what each PR is for, and the failing-baseline-
vs-fixed UART contrast. Also documents two real gotchas hit during
bring-up:

1. mpremote end-to-end VERIFIED with a Pico W flashed with
   MicroPython firmware (vid 2e8a:0005) on OTG_FS. One chained
   mpremote process ran `exec sysinfo + fs cp + fs ls + exec
   script + fs rm`; every step succeeded, including a multi-line
   script copied from the host and executed on the Pico, with
   the Pico's stdout streaming back. Earlier negative result was
   the Pico Debugprobe's UART pass-through having no REPL on the
   far side - the bridged transport itself was always fine.

   Between separate mpremote invocations the kernel's cdc-acm
   hangup grace period intermittently returns "device in use".
   Chaining commands with `+` in a single mpremote process
   sidesteps that.

2. `/dev/ttyACM<N>` devtmpfs footgun: writing to that path
   while the kernel hasn't finished creating the character
   device (e.g. between attach and cdc_acm bind) creates a
   regular file at that inode. Subsequent attaches can't put
   the char device there, and pyserial open returns
   "Inappropriate ioctl for device". Cure: `sudo rm
   /dev/ttyACM<N>` before re-attaching.

## Phase 7 (deferred): upstream PRs

PR1, PR2, PR3, PR4 against hathach/tinyusb plus the example
itself as a separate PR. Holding off on this per user request -
want more testing across device types (HID, MSC, MIDI) and
across F7/H7 DWC2 ports before submitting.
