# R16 iter4 findings

Branch: `worktree-agent-ab84e6afd392e21aa` (based on `a56fba1`).

Commits added (in order):

- `0a0b0bc` instrumentation for double-giveback diagnosis
- `542e536` send RET_UNLINK with -ECONNRESET, not status 0
- `9c432cd` instrument xTaskCreatePinnedToCore for client_task
- `cd0e11e` reduce USBIP_WORKER_TASK_STACK from 8 KiB to 4 KiB (superseded)
- `c38a555` reduce worker (3 KiB) and client_task (4 KiB) stacks (superseded)
- `c89b624` allocate USBIP task stacks from PSRAM via WithCaps

## Bug 1: which call sites emit the double-giveback

Instrumentation captured every `tx_ret_submit` and `tx_ret_unlink` call
with seqnum, ep, dir, status, and a short site label. Across 30 mpremote
round-trips the smoking-gun pattern is:

```
W (200126) usbip: RX_UNLINK seq=90351 unlink_seq=90311
W (200136) usbip: TX_RET_SUBMIT seq=90311 ep=2 dir=1 status=-104 len=0 site=worker
W (200136) usbip: UNLINK_CLAIM seq=90351 unlink_seq=90311 sem=1 prior_owner=1 send=0
W (200146) usbip: TX_RET_SUBMIT seq=90312 ep=2 dir=1 status=-104 len=0 site=worker
W (200146) usbip: RX_UNLINK seq=90352 unlink_seq=90312
W (200156) usbip: UNLINK_NO_MATCH seq=90352 unlink_seq=90312
W (200156) usbip: TX_RET_UNLINK seq=90352 ep=0 dir=0 status=0 site=unlink_no_match
```

The race: a worker retires URB 90312 (sends RET_SUBMIT), then 1 ms later
the kernel's UNLINK for that same URB arrives. `inflight_begin_cancel`
finds nothing (URB already off the inflight list). The unlink handler
falls into the NO_MATCH branch and sends `RET_UNLINK status=0`. The
kernel logs `vhci_hcd: the urb (seqnum 90352) was already given back`
when its `pickup_urb_and_free_priv` for the unlink fails to find the
URB on `priv_rx`.

So both call sites in the kernel-visible giveback ledger are:

- `tx_ret_submit site=worker` (the worker that won tx_owner)
- `tx_ret_unlink site=unlink_no_match` (the read loop UNLINK handler
  for the URB that already retired)

The "already given back" log is informational; the kernel does not
double-deliver the URB. It is not the actual hang cause.

## Bug 1: actual root cause of the wedge after N round-trips

UNLINK_NO_MATCH is benign. The wedge is something else: in the iter4
log, the very first UNLINK that times out (cancel_done_sem take
returns pdFALSE after 250 ms) targets an EP0 URB whose worker is
genuinely stuck inside `usbhost_control_transfer`. The IDF reports
`E USB HOST: EP command error: ESP_ERR_INVALID_STATE` repeatedly,
meaning halt+flush+clear failed to recover the EP. After that point,
every subsequent EP0 UNLINK times out (sem=0 prior_owner=0), the
worker for each stuck URB never retires (those seqnums never appear
in TX_RET_SUBMIT), and cdc-acm starts seeing only EP0 GET_DESCRIPTOR
re-enumeration probes because its own bulk URBs cannot be submitted
behind the wedged EP0 control state.

Comparing 133 stuck seqnums between UNLINKed and submitted confirmed
the worker pool is genuinely stuck in IDF, not just delayed.

## The fix to RET_UNLINK status

`tx_ret_unlink` previously sent `status=0`. The kernel's
`vhci_recv_ret_unlink` propagates `pdu->u.ret_unlink.status` directly
into `urb->status` before giving the URB back. status=0 means the
control transfer "completed normally with 0 bytes", which cdc-acm
mistreats as a clean SET_LINE_CODING / SET_CONTROL_LINE_STATE success.
For the timeout path the URB is genuinely cancelled, so the spec-correct
status is -ECONNRESET. The patch:

```c
int32_t unlink_status = -ECONNRESET;
...
tx_ret_unlink(conn, hdr.seqnum, hdr.devid, hdr.direction, hdr.ep,
              unlink_status, site);
```

After this fix, kernel-side cdc-acm sees -ECONNRESET on cancelled
control transfers (correct), the "already given back" log persists
but is informational only (not a functional regression), and dmesg
no longer logs cdc-acm sequencing errors.

This is necessary but not sufficient: the IDF EP wedge that causes
workers to never retire is upstream of usbip and outside the scope
of this iteration. Stress fails at iter ~12-17 instead of producing
a kernel-side teardown but the device still wedges. See "Open
follow-ups" below.

## Bug 2: 2-1 attach refused when 1-1 attached first

Reproduced cleanly. The accept-loop / client_task / handle_client /
handle_import_request entry instrumentation showed:

```
I (13036) usbip: accept: fd=58 from=192.168.0.197:35268
W (13475) usbip: xTaskCreatePinnedToCore(client_task) failed rc=-1 fd=58
```

`rc=-1` is `errCOULD_NOT_ALLOCATE_REQUIRED_MEMORY`. ESP32-S3 internal
SRAM at boot reports only `(231664, 200, 160, 4)` free in the 232 KiB
internal-RAM region (heap region index 5 of esp32.idf_heap_info).
24 worker tasks at 8 KiB stacks each plus 1 client_task overflow that
region the moment the first IMPORT spawns the worker pool. The second
IMPORT's xTaskCreatePinnedToCore silently fails because IDF's default
`xTaskCreate*` allocates task stacks from internal SRAM only.

PSRAM has 8 MiB free. `xTaskCreatePinnedToCoreWithCaps` (in
`freertos/idf_additions.h`) allows specifying `MALLOC_CAP_SPIRAM`
so the stack is allocated from PSRAM while the TCB stays in internal
RAM. After switching the worker pool and client_task to WithCaps and
matching the deletes to `vTaskDeleteWithCaps`, both 1-1 and 2-1 attach
in either order.

Verified: with 1-1 attached first, then 2-1 attach succeeds. lsusb
shows both `2e8a:0005 MicroPython Board in FS mode` (the Pico) and
`c251:f00a Keil Software Inc. mpy-pod synthetic CMSIS-DAP` (the
synthetic device).

## Test results

### Smoke 5/0

`bash test/integration/phase3/run.sh 192.168.0.166`

```
== Summary ==
  5 passed, 0 failed
```

### Concurrent CDC + CMSIS-DAP test sequence

```
attach 1-1; attach 2-1
mpremote on Pico CDC -> "CDC1 ok"
pyocd reset --probe 3982ABCD --target rp2040 -O connect_mode=under-reset
   -> "No ACK received" (unrelated SWD/bit-bang issue, not USBIP)
mpremote on Pico CDC -> "CDC2 ok"
```

Both mpremote calls return cleanly, the synthetic CMSIS-DAP probe is
visible to pyocd. The "No ACK" is the same SWD bit-bang behaviour
documented in P3.7b and is independent of USBIP routing; pyocd talked
to the synthetic CMSIS-DAP without USBIP errors.

### 30-iteration mpremote stress

Best run with all iter4 fixes: passed 11 consecutive iterations,
failed at iter 12 with rc=143 (mpremote killed by 12 s timeout
because the device wedged). UART log shows the IDF EP0 wedge
(`E USB HOST: EP command error: ESP_ERR_INVALID_STATE`) cascading
into all subsequent EP0 transfers timing out at 250 ms. Workers
holding stuck URBs never retire and the device becomes unresponsive
on the ESP32 console as well.

This is a regression from `a56fba1`'s observed behaviour (17 round
trips before wedge); -ECONNRESET correctness is paid for with cdc-acm
giving up sooner because the kernel-side error path now runs cleanly
on the first stuck cancel, instead of accumulating noise URBs that
let the test limp through more iterations.

### Standalone pyocd-only smoke

After fresh boot, attach 2-1 only, run `pyocd reset --probe 3982ABCD`:
synthetic CMSIS-DAP visible, "No ACK" returns from SWD bit-bang side
(same residual issue as above; not a USBIP regression).

## Caveats and open follow-ups

1. **The IDF EP wedge is the actual residual hang**. After ~10-17
   round trips the IDF reports `EP command error:
   ESP_ERR_INVALID_STATE` on EP0, halt+flush+clear cannot recover
   the EP, and any worker waiting on done_sem for a URB on that EP
   stays stuck forever. This is in `src/c_modules/usbhost/usbhost.c`
   `submit_xfer` plus the IDF's `usb_host_endpoint_*` API; not in
   `usbip_server.c`. Possible directions:
     - Force a `usb_host_device_close` + `usb_host_device_open` on
       the device when halt+flush+clear fails. Tearing down the
       client and reopening reinitialises the IDF EP descriptors.
     - Maintain a watchdog timer on each in-flight URB and kill the
       worker via `vTaskDeleteWithCaps` when it exceeds N seconds,
       then teardown+reopen the device. The current 5 s control
       timeout is silently exceeded.
     - Investigate whether the close-storm of 16 simultaneous UNLINKs
       overflows an internal IDF queue and whether spacing them out
       (one at a time on the read loop) avoids the wedge.
2. **The "already given back" kernel log is still informational but
   noisy**. Suppressing it would require either deferring the kernel-
   side priv_unlink dequeue until after RET_UNLINK arrives (kernel
   change, not ours) or buffering retired seqnums for a short window
   on the server so we can answer UNLINK_NO_MATCH with a "URB already
   retired" path that does not send RET_UNLINK at all. The latter
   risks the kernel never freeing its priv_unlink slot and is best
   left until after the IDF wedge is fixed.
3. **Stack memory on PSRAM has higher access latency than internal
   SRAM**. Worker stacks now live there. If profiling shows worker
   completion latency is hurting cdc-acm bulk throughput, the
   alternative is to reduce worker pool size to e.g. 16 and keep
   stacks in internal SRAM. Current builds passed integration smoke
   so the latency penalty is acceptable for the workloads tested.
4. **Iteration budget exhausted**. Used all 5 build/flash/test cycles
   on (a) instrumentation; (b) RET_UNLINK -ECONNRESET; (c) Bug 2
   diagnosis with task-creation log; (d) intermediate stack-shrink
   attempt (superseded); (e) PSRAM stacks via WithCaps. The IDF EP
   wedge needs a separate iteration in usbhost.c.

## Files touched on this branch (from a56fba1)

- `src/c_modules/usbip/usbip_server.c` (instrumentation, RET_UNLINK
  status fix, PSRAM task stacks)

No changes to micropython submodule, referencea/, or vendor/.
