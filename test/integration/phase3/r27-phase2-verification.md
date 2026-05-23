# R27 Phase 2 verification: cdc-acm cleanup wedge fixed

Bench run on the post-CLEAR_FEATURE-removal commit confirms the
cdc-acm cleanup wedge from r27-phase2-audit.md is resolved.

## Test environment

* Branch tip: `r27-tinyusb-migration` HEAD with the CLEAR_FEATURE
  removal in `usbhost.c` (this commit).
* Host: x86_64 Linux 6.17.0-23-generic, vhci_hcd module.
* DUT: Pico W (2e8a:0005) running stock MicroPython, attached as
  `/esp-usb-host/1-1` via the Annealage Pod's exported usbip endpoint.
* Annealage Pod: ESP32-S3-WROOM-1-N16R8, firmware 1857168 B.

## 30/30 single-call cycled mpremote

```bash
PICO=/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_a5a2229740635c53-if00
PASS=0; FAIL=0
for i in $(seq 1 30); do
    if timeout 30 mpremote connect "$PICO" resume exec "print('cf $i')" \
        | grep -q "cf $i"; then
        PASS=$((PASS+1))
    else
        FAIL=$((FAIL+1))
    fi
done
echo "PASS=$PASS FAIL=$FAIL"
```

Result: `PASS=30 FAIL=0`. Per-iteration timing 4-9 seconds (the
synth/recovery path on close adds 3-7 s vs a fresh-attach pass).

* `dmesg | grep usb_poison_urb` post-run: 0 entries.
* UART watchdog: `fires=0` across the run window.
* Synth WIN: 395 events (~13 per close cycle, matching cdc-acm's
  16 read URBs minus already-retired URBs).
* Synth LOST_TO_NATURAL: 1 (CAS race resolved cleanly).
* Synth NO_INFLIGHT: 0 (inflight tracking matched every UNLINK).

## Re-verification with verbose disabled

Same loop but with `s_urb_verbose=false` (the diagnostic flag
flipped back off after the 30/30 above):

```
iter 1 ok 4s; iter 2 ok 5s; iter 3 ok 3s; iter 4 ok 5s; iter 5 ok 4s
PASS=5 FAIL=0
```

`dmesg | grep usb_poison_urb`: 0 entries. UART `watchdog: synth`
events: 0. Confirms the fix is structural (not timing-dependent on
verbose logging).

## fs cp 128 KB still wedges

Separate from the cdc-acm cleanup wedge: a 20-iteration loop of
`mpremote ... fs cp /tmp/r27-test128k.bin :test128k.bin` failed
on iteration 1. mpremote raised `OSError: [Errno 116] Stale file
handle` from `serial.Serial.close` after the fs cp transfer. The
device side reported no watchdog fires, the kernel had no
`usb_poison_urb` entries; the failure mode is different from the
cdc-acm cleanup wedge that this fix addresses.

This is a separate issue. Likely candidates: fs cp's interleaved
control + bulk-OUT pattern triggers a different wedge path, or
mpremote's serial close reacts badly to a transient cdc-acm ACM
re-enumeration during the fs cp transfer. Out of scope for the
cdc-acm cleanup wedge fix; deferred to a follow-up investigation.

## Recovery primitive

`sudo usbip detach -p 0` cleared every wedge encountered during
this verification round in <1 second. Force-reboot was not used.
The previous overnight session's claim that detach itself wedged
did not reproduce.

## Files touched

* `src/c_modules/usbhost/usbhost.c`: drop CLEAR_FEATURE block in
  both `usbhost_cancel_ep` and `usbhost_watchdog_recover`; update
  gotcha #6 in the file docstring.
* `test/integration/phase3/r27-upstream-pr-draft.md`: add PR 4
  (TinyUSB `tuh_control_xfer` honour `timeout_ms`) and update the
  filing-order section.
* `test/integration/phase3/r27-phase2-verification.md` (this file).
