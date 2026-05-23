# R27 deferred: fs cp 128 KB wedge

Status: deferred. Single-run reproduction during the cdc-acm
cleanup wedge verification on commit `d846d31`. Not currently
blocking; revisit if a second reproduction lands or if the
behaviour starts hitting CI/regression coverage.

## Symptom

```
$ timeout 60 mpremote connect $PICO resume fs cp /tmp/r27-test128k.bin :test128k.bin
... (transfers some bytes) ...
Traceback (most recent call last):
  ...
  File ".../mpremote/transport_serial.py", line 118, in close
    raise er
  File ".../mpremote/transport_serial.py", line 111, in close
    self.serial.rts = False
  File ".../serial/serialposix.py", line 708, in _update_rts_state
    fcntl.ioctl(self.fd, TIOCMBIC, TIOCM_RTS_str)
OSError: [Errno 116] Stale file handle
```

* Wall-clock: 34 s before mpremote raised.
* `dmesg | grep usb_poison_urb`: 0 entries (so this is NOT the
  cdc-acm cleanup wedge that `d846d31` resolved).
* Device-side UART log over the same window showed only normal
  enumerate_device + idle activity, watchdog `fires=0`.
* `ps -p <mpremote_pid>` after the failure: `STAT=D` for 3:35,
  cleared by `usbip detach -p 0`.

## Distinguishing characteristics

* Distinct from the cdc-acm cleanup wedge (no `usb_poison_urb`
  in the kernel stack).
* Distinct from R26 lwIP pbuf corruption (no pbuf-related
  warnings or device-side memory corruption).
* The OSError comes from `serial.Serial.rts = False` during
  close. The fd is reported stale before the close path
  reaches the actual file-descriptor close, which means the
  cdc-acm tty was already torn down by something earlier.

## Likely root causes (untriaged)

* Bulk-OUT pipeline back-pressure during the 128 KB transfer.
  USBIP_PIPELINE_DEPTH=1 (gotcha #2) and the TCP send queue
  could combine to stall the lane far enough that the kernel
  cdc-acm marks the tty as failed.
* A control transfer inside mpremote's fs cp protocol (e.g. a
  baud change before/after the transfer) hits the same
  `tuh_control_xfer` no-timeout pattern that CLEAR_FEATURE used
  to. Pre-PR 4 this is a structural risk for any control xfer
  that NAKs.
* Device-side reset/disconnect under sustained throughput. The
  cdc-acm tty disappearing mid-flight would explain the stale fd
  on close.

## Reproducer

```bash
PICO=/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_<sn>-if00
dd if=/dev/urandom of=/tmp/r27-test128k.bin bs=1024 count=128
sudo usbip attach -r 192.168.0.166 -b 1-1
timeout 60 mpremote connect "$PICO" resume fs cp /tmp/r27-test128k.bin :test128k.bin
echo "exit=$?"
sudo usbip detach -p 0   # only if the run hung
```

Reproduces immediately on the d846d31 firmware. Has not been
re-run more than once; could be flaky.

## Recovery

`sudo usbip detach -p 0` (<1 s) cleared the wedge each time. No
device cycle or host reboot needed.

## Re-investigation triggers

Pick up if any of:
* fs cp 128 KB fails twice in a row on a fresh attach.
* `dmesg` shows `usb_poison_urb` or hung-task warnings during fs cp.
* Bulk-OUT throughput regressions appear in the R23 throughput
  ceiling work.
* Anyone tries to wire fs cp into CI / smoke-test coverage.

When picking it up: re-flash with `s_urb_verbose=true`, capture
full UART + dmesg + mpremote stack via py-spy or similar. Inspect
mpremote `fs cp` protocol in `transport_serial.py` and the
injected raw-repl script.
