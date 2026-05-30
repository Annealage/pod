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

- S3 `docs/design/usbip-server.md`, `docs/design/usbhost.md`
- `research/usbip-multiplexing-design.md` (protocol; multiplexing now unused)
- S3 spec §4.5, §5.5 (trust model carries over)
