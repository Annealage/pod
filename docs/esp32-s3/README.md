# Annealage Pod - ESP32-S3 variant (prior, superseded design)

These documents describe the **ESP32-S3** build of the Annealage Pod: a single
ESP32-S3 + ESP-IDF + MicroPython, with C user modules for USB/IP, a synthetic
CMSIS-DAP-v2 probe, a UART bridge, and I2C/SPI slave personalities. It mates with
the Octoprobe DUT carrier over a 2x20 connector.

This is **not** the board in active bring-up. The live target is the **RP2350
(Pico 2 W)** variant under [`../pod/`](../pod/), which inverts the
debug-probe decision (the pod runs the debugger itself over PIO SWD) and uses the
native USB controller as the DUT host port. If you are wiring up hardware or
following a recipe, use the RP2350 docs - in particular
[`../pod/hardware-setup.md`](../pod/hardware-setup.md) for the DUT-to-pod
wiring. **The pin numbers in these ESP32-S3 documents (GPIO assignments up to
GPIO48, USB on GPIO19/20, SWD on GPIO10/11, etc.) do not apply to the RP2350 and
will mis-wire a Pico 2 W.**

Contents:

- `spec.md` - the ESP32-S3 design spec (what that variant of the pod did).
- `architecture.md` - how the ESP32-S3 parts compose (companion to the spec).
- `spec-appendix-A-pinmap.md` - ESP32-S3 GPIO assignment and the Octoprobe v0.7
  DUT-carrier pin map.
- `spec-appendix-B-rp_infra-api.md` - RP_INFRA API surface the pod mimics.
- `runbook.md` - ESP32-S3 bring-up and DUT-exercise recipe.
- `design/` - per-workstream design notes (USB host, USB/IP server, CMSIS-DAP,
  SWD/SWO engine, UART bridge, slave IO, ops, package layout).
- `progress-2026-04-29.md` - an ESP32-S3-era progress snapshot.
