# Phase 4: TinyUSB host-stack regression bench

Regression coverage for the 4 host-stack fixes pulled in via the
MicroPython submodule from upstream TinyUSB PR
[hathach/tinyusb#3637](https://github.com/hathach/tinyusb/pull/3637).
Each fix gets one runtime test that exercises the symptom in real
cdc-acm traffic over the USB/IP bridge. If a future MicroPython
submodule bump silently regresses any of them, the matching test
fails.

## Bug map

| Test script | Fix in TinyUSB | Symptom without fix |
|---|---|---|
| `test_bulk_out_large.py` | `hcd/dwc2: fix txfifo full check` | Multi-packet bulk-OUT corrupts bytes mid-transfer; cdc-acm rx side sees garbled data. |
| `test_in_pid_after_short.py` | `hcd/dwc2: save post-transfer PID in DMA-mode IN handler` | After a short IN packet ending a multi-packet transfer early, the next IN URB hits `DATATOGGLE_ERR`. Reads drop the device's first packet or coalesce duplicates. |
| `test_abort_releases_ep.py` | `hcd/dwc2: fire xfer_complete callback after hcd_edpt_abort_xfer` | `usbip detach` mid-IN leaves the EP claim held in TinyUSB. Re-attaching the same DUT produces a `tuh_*_xfer rejected` rejected-cascade until next umount. |
| `test_control_timeout.py` | `host: honour tuh_xfer_t.timeout_ms in tuh_control_xfer` | A device that NAKs a control transfer indefinitely (e.g. cdc-acm on `CLEAR_FEATURE(ENDPOINT_HALT)`) wedges `tuh_control_xfer` permanently with no recovery. |

## Bench setup

Each test assumes:

* A annealage_pod running the usbip server at `$USBIPD_IP`, default
  `192.168.0.182`. Override via env var.
* A MicroPython DUT attached to the annealage_pod's OTG host port,
  exposed as `1-1` over USB/IP.
* The local Linux host has `usbip-utils` installed and `vhci_hcd`
  available; the test invokes `usbip attach` and `echo 0 >
  /sys/devices/platform/vhci_hcd.0/detach` directly.
* `mpremote` reachable via the Python environment.

Run individually:

```sh
USBIPD_IP=192.168.0.182 ./test_bulk_out_large.py
```

Or sweep all four:

```sh
USBIPD_IP=192.168.0.182 ./run_all.sh
```

Each test exits 0 on success, non-zero on first failed assertion.
Failures print the relevant annealage_pod log lines and the unexpected
client-side state.

## Adding new tests

The pattern is: attach, exercise the scenario, assert observable
behaviour, detach. Keep each test scoped to one fix. Cross-cutting
tests live under `test/integration/` (parent dir), not here.
