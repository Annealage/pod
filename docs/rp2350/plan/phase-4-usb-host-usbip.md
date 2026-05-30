# Phase 4: DUT USB host + USB/IP

Workstreams F + D. Export the DUT's USB to a host PC over Wi-Fi. Independent of
the debug stack; sequenced after Phases 2-3 (decision 3).

Goal: a host PC enumerates the DUT as a local USB device via `usbip attach` over
Wi-Fi.

## Dependencies

- F1 (transport), native USB host validated in D1.2.

## Tasks

### F4.1 Native USB host enumeration
- Bring up the native USB controller in host mode (machine-usbhost branch),
  enumerate a DUT (target the typical composite MicroPython-DUT shape: CDC + MSC),
  expose raw URB submit/complete suitable for USB/IP forwarding.

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
