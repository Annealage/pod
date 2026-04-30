# R16 iter6 findings

Branch: `worktree-agent-a94097d08e90beb11` (based on `f13a8b3`).

Single commit on this branch:

- `9101746` ticket-ordered backend submit gate

## Hypothesis confirmed

A variant of hypothesis (1): concurrent OUT + IN does not race at
submit time, but multiple IN workers do. The 24-task worker pool
services bulk/interrupt IN URBs. Each worker calls
`usbhost_bulk_transfer` -> `submit_xfer` which acquires the per-EP
submit mutex and calls `usb_host_transfer_submit`. The mutex
guarantees one submit at a time on a given EP, but the order in
which workers WIN the mutex is not the order in which their URBs
were intaken from the USB/IP socket. So the IDF receives submits
in worker-race order. The IDF preserves submit order on the wire
and routes incoming data accordingly; the bytes on the wire are
delivered to URBs whose `tx_ticket` does not match the byte
position on the wire.

The RET_SUBMIT side has a tx_order gate that emits RET_SUBMITs
in `tx_ticket` order. Combined with the scrambled wire-side
delivery, the kernel sees seqnum-ordered RET_SUBMITs but with
payload bytes that no longer line up. cdc-acm reassembly drops or
reorders bytes; the raw REPL handshake sees the symptom
`b'\x01R\x01'` and fails with "could not enter raw repl".

This is also the same root cause the iter5 commit hinted at in the
"per-EP cancel mutex" comment ("with multiple worker tasks racing
to call _submit on the same EP, the actual submit order is non-
deterministic; cdc-acm bulk-IN reassembly then sees scrambled
bytes"). The submit mutex stops parallel submits but does not
enforce ticket order.

OUT URBs and EP0 control transfers are not affected because they
run inline in the read loop, which is single-threaded; their
submits go to the IDF in the order the read loop processes them.
Synthetic devices are not affected for the same reason.

## Code change

In `src/c_modules/usbhost/usbhost.h`:

- New `usbhost_submit_order_t` struct with `wait_fn`, `advance_fn`,
  `ctx`. Caller supplies callbacks that the backend invokes
  immediately before and after `usb_host_transfer_submit`.
- New `usbhost_bulk_transfer_ordered` and
  `usbhost_interrupt_transfer_ordered` entry points that take an
  optional submit-order hook. The non-ordered entry points keep
  their signatures unchanged and pass NULL through.

In `src/c_modules/usbhost/usbhost.c`:

- `submit_xfer` takes a new `const usbhost_submit_order_t *order`
  argument. Wait callback is invoked OUTSIDE the per-EP submit
  mutex (otherwise a later-ticket worker that grabbed the mutex
  first would block the earlier-ticket worker forever from
  satisfying its wait). Advance callback runs inside the mutex
  immediately after the IDF submit returns, so the next-ticket
  worker can begin its submit as soon as ours has been handed to
  the IDF; URBs still complete concurrently end-to-end.
- New `_ordered` entry points wrap `submit_xfer` with the hook.

In `src/c_modules/usbip/usbip_server.c`:

- `tx_order_slot_t` gains `submit_done` counter alongside `issued`
  and `done`. The `done` counter still gates RET_SUBMIT order on
  the tx side; `submit_done` gates the actual IDF submit on the
  rx-into-IDF side. Both are advanced in tx_ticket order.
- `inflight_urb_t` gains a `submit_done_advanced` flag. Set by the
  submit-order advance callback (workers); used by `run_inflight`
  to advance `submit_done` unconditionally on inline paths
  (synthetic, EP0, OUT, queue-saturated) so subsequent URBs on
  the same `(ep,dir)` are not stranded.
- `run_inflight` wraps the bulk/interrupt call with a
  `submit_order_ctx_t` and passes the hook into
  `usbhost_*_transfer_ordered`.
- The queue-saturated path in `intake_submit` advances
  `submit_done` along with `done` if the URB never reached the
  backend.
- The end of `run_inflight` advances `submit_done` if no callback
  ran.

Diff size: +158 / -18 across the three files.

## Test results

### Stress 90/90 across three back-to-back runs

Run 1 (after fresh power cycle):
```
PASS=30 FAIL=0
```

Run 2 (immediately after, no power cycle):
```
PASS=30 FAIL=0 (run 2)
```

Run 3 (immediately after, no power cycle):
```
PASS=30 FAIL=0 (run 3)
```

Total 90/90 with no failures and no resets between runs. Compare
iter5's back-to-back run which produced 28/30 once and 29/30 once
on the same hardware after the same fresh boot.

### Concurrent attach (1-1 + 2-1)

```
mpremote on Pico CDC -> CDC1 ok
pyocd reset --probe 3982ABCD -> probe visible, mbed-board warning only
mpremote on Pico CDC -> CDC2 ok
```

Both mpremote calls return cleanly with the synthetic CMSIS-DAP
probe simultaneously attached.

### Smoke 5/0

```
== Summary ==
  5 passed, 0 failed
```

All Phase 3 smoke checks pass: usbip list, REPL, UART bridge,
pyOCD-recognises-CMSIS-DAP, REPL cleanup hook.

### Standalone pyocd-only

After fresh boot, attach 2-1 only, `pyocd reset --probe 3982ABCD`:

```
0000766 W Board ID 3982 is not recognized [mbed_board]
0000766 W Generic 'cortex_m' target type is selected by default ...
```

Probe visible; warnings only, no command failure. The "No ACK"
SWD bit-bang residual that surfaced in iter5 did not reproduce
this run; that residual is documented under P3.7b and is
unrelated to the USBIP plumbing.

## Caveats and open follow-ups

1. **Submit-order spin uses vTaskDelay(1)**. Same pattern as the
   existing tx_order gate. Spin is bounded by the IDF submit
   latency on the prior URB (microseconds in the steady state);
   if a prior URB blocks for a long time inside `submit_xfer`
   (e.g., waiting on a wedged EP), the next-ticket worker will
   spin until either the prior worker advances submit_done or the
   prior worker's cancel watchdog expires (2 s). The watchdog
   path correctly advances submit_done because the advance
   callback runs after the IDF submit returns, and the watchdog
   only kicks in AFTER the submit succeeded; the wait on
   done_sem is what watchdogs, not the submit itself. Net: a
   wedge cannot pin the submit-order gate longer than it would
   pin the existing tx_order gate.

2. **Submit-order advance is bound to the IDF submit returning,
   not the URB completing**. This is intentional: it keeps the
   wire pipelined. But it means if URB N's submit succeeds and
   URB N+1's submit then fails (e.g., INVALID_STATE flagging
   reopen), URB N+1's advance still runs (the submit returned,
   even with error). URB N+2 then proceeds. Behaviour is
   indistinguishable from the pre-fix path on errors; only the
   ordering on the success path is changed.

3. **EP0 control transfers do not use the ordered API**. They run
   inline in the read loop, single-threaded, so they cannot race.
   Reopen-recovery control transfers (issued from the worker that
   pumps client events) also do not use the ordered API; they
   touch a slot in transition where no other URBs are submitting.
   No change required.

4. **Iteration budget**. Used 1 of 5 build/flash/test cycles:
   (a) iter6 single change set landed first build. The hypothesis
   was identifiable from static reading of the iter5 code; no
   instrumentation pass was required. Four cycles remain unused.

5. **iter5 caveat (1) is no longer an open follow-up for this
   class of failure**. The reopen path was not exercised in
   iter6 either; that caveat carries forward unchanged.

6. **iter5 caveat (2) (residual ~5-7%) is closed by this fix**.
   The iter5 stress flake is the symptom this iteration roots
   out.

## Files touched on this branch (from f13a8b3)

- `src/c_modules/usbhost/usbhost.h`
- `src/c_modules/usbhost/usbhost.c`
- `src/c_modules/usbip/usbip_server.c`

No changes to the micropython submodule, referencea/, or vendor/.
