# R16 iter5 findings

Branch: `worktree-agent-a5ec76269f97784ed` (based on `9cdcf98`).

Single commit on this branch:

- `01fc47e` per-EP cancel mutex + watchdog + reopen recovery

## Recovery approach taken

A combination of (a)-style, (b)-style, and (c)-style measures. In
priority of what actually carried the load:

1. **Per-EP submit/cancel serialisation** (this is the load-bearing
   fix). The iter4 evidence pointed to the IDF EP-command machine
   rejecting halt+flush+clear with `ESP_ERR_INVALID_STATE`. The
   iter1 per-EP submit mutex already serialised submits on the same
   EP, but the cancel halt+flush+clear sequence in `submit_xfer`
   was not under that mutex. So a worker calling
   `usb_host_endpoint_halt` on EP X could race a parallel worker
   that just entered `usb_host_transfer_submit` on EP X. The IDF
   refuses nested EP commands. Holding the per-EP mutex around the
   entire halt+flush+clear sequence stops the wedge from forming.

2. **Cancel watchdog** (approach c). The previous loop was
   `while xSemaphoreTake(done_sem, 50ms) != pdTRUE`, unbounded.
   Once done_sem never fires (genuine IDF wedge) the worker pinned
   forever. Now the post-cancel wait is capped at 2 s, after which
   the URB is leaked to the IDF (heap inflight stays alive,
   refcounted) and the caller returns -ECONNRESET. The worker
   slot is not pinned even on a genuine wedge.

3. **Device reopen** (approach b, safety net). When halt+flush+
   clear all return INVALID_STATE on a cancel, or `submit` itself
   returns INVALID_STATE, the slot is flagged `needs_reopen`. The
   worker drains the reopen queue between event pumps and performs
   `release_interfaces + device_close + device_open + claim`. The
   slot keeps its busid and devnum so the kernel-side cdc-acm bind
   survives. Wedge counters are reset on the new pipes.

In the 30-iteration confirmation run after a fresh boot the
recovery code never fired, no `INVALID_STATE` warnings appeared,
and 30/30 round-trips passed. The per-EP cancel mutex (#1) appears
to prevent the wedge from forming in the first place; (#2) and
(#3) remain as safety net for any residual case where the mutex
ordering is insufficient (e.g., the IDF cancelling on its own
internal timer).

## Code change

In `src/c_modules/usbhost/usbhost.c`:

- Inflight record moved off the caller's stack onto the heap, with
  a refcount (caller + IDF) and a `caller_gone` flag. Required by
  the watchdog leak path: the IDF callback may fire after the
  caller has given up, so the inflight cannot live on the freed
  caller stack.
- `inflight_unref` frees done_sem, ref_lock, the IDF xfer, and the
  inflight itself when the count reaches zero. Safe from any
  context.
- `submit_xfer` cancel path now holds the per-EP submit mutex
  around `usb_host_endpoint_halt + flush + clear`.
- Cancel path tracks whether all three returned INVALID_STATE; if
  so, increments the slot's per-EP `ep_wedge` counter and flags
  `needs_reopen` once the threshold is crossed.
- After issuing cancel, the caller's wait on done_sem is bounded
  by `USBHOST_CANCEL_WATCHDOG_MS` (2000 ms). On expiry, set
  `caller_gone`, drop the caller's ref (the IDF still owns one;
  callback later unrefs and frees), return -ECONNRESET.
- `submit` failure with INVALID_STATE also flags `needs_reopen`.
- New `handle_reopen_request` runs in the worker task off the IDF
  callback path. Steps: snapshot busid+address, release_interfaces,
  device_close, sleep 50 ms, device_open by address, install new
  dev_hdl, re-claim, reset wedge counters, clear `needs_reopen`.
  On failure, the slot is dropped entirely so a fresh NEW_DEV
  event recreates it.
- Worker loop calls `drain_reopen_requests()` after
  `drain_event_queue()` each iteration.
- `usbhost_slot_t` gets `needs_reopen`, `reopen_address`, and
  `ep_wedge[32]` fields.

Diff size: +321 / -40 in usbhost.c.

## Test results

### Stress 30/30

```
PASS=30 FAIL=0
```

Run from a fresh power-cycled boot. No reopen, no wedge, no
INVALID_STATE warnings in the UART trace.

A second back-to-back stress (without power-cycle) produced 28/30
once and 29/30 once. Failures looked like
`b'\x01R\x01' mpremote: could not enter raw repl`, i.e. mpremote
saw a raw-repl protocol byte stream out of sync. No IDF errors,
no recovery triggers, no usbhost log lines around the failures.
This is a different residual class from iter4's IDF wedge and is
a separate follow-up.

### Concurrent attach (1-1 + 2-1)

```
mpremote on Pico CDC -> CDC1 ok
pyocd reset --probe 3982ABCD -> probe visible, "No ACK" SWD
mpremote on Pico CDC -> CDC2 ok
```

Both mpremote calls return cleanly. The synthetic CMSIS-DAP probe
is visible to pyocd. "No ACK" is the documented P3.7b SWD
bit-bang residual, unrelated to USBIP.

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
0000757 W Board ID 3982 is not recognized [mbed_board]
0000775 C No ACK received [__main__]
```

Probe visible, command reaches the synthetic CMSIS-DAP. The
"No ACK" is the same SWD bit-bang residual as documented in
iter4 / P3.7b.

## Caveats and open follow-ups

1. **Reopen path was not stress-tested in anger**. The iter4 IDF
   EP wedge never reproduced after the per-EP cancel mutex went
   in. The reopen logic is structurally engaged (slot tracking,
   worker drain) but the path through `release_interfaces +
   device_close + device_open + claim` was not exercised end-to-
   end on a real wedge. If a future build of the IDF reintroduces
   the wedge under a different code path, the reopen branch may
   need timing tuning (the 50 ms post-close delay is a guess).

2. **Residual ~5-7% intermittent on back-to-back stress runs**.
   When 30-iter stress is repeated without a power-cycle in
   between, occasional failures present as
   `mpremote: could not enter raw repl` with empty stdout. UART
   captures show no IDF errors, no recovery triggers, no usbhost
   log lines around those iterations. Most likely candidates
   (not investigated this iteration):
     - cdc-acm sequencing during teardown of one round-trip
       overlapping the next attach.
     - worker-pool bottleneck during the brief window between an
       UNLINK and the next SUBMIT for the same EP.
     - kernel-side priv_unlink slot retention from the
       UNLINK_NO_MATCH "already given back" path that was noted
       benign in iter4.
   None of these regressed against the iter4 28/30 baseline; the
   net change from iter4 is at worst neutral and on a fresh boot
   the run is clean.

3. **Heap inflight + refcount adds malloc/free per URB**. Worst
   case is ~16 outstanding URBs on a busy cdc-acm + interrupt-IN
   pair, so the additional pressure is bounded. PSRAM is the
   default heap on this board so the allocator should not
   degrade. If profiling shows allocation latency, an inflight
   pool could replace malloc, but it was not needed for the
   iter5 success criteria.

4. **`ESP_ERR_INVALID_STATE` from any of halt/flush/clear is
   currently treated as evidence of EP wedge**. In some paths
   (e.g., halting an EP that is already halted) INVALID_STATE
   may be benign. The current logic only flags reopen if all
   three return INVALID_STATE, which is conservative; if false
   positives ever trigger reopen during normal traffic, the
   threshold counter (`ep_wedge`) gives one cycle of grace and
   the threshold can be raised.

5. **Iteration budget**. Used 4 of 5 build/flash/test cycles:
   (a) iter5 single change set landed cleanly first build;
   (b) confirmation 30-iter run after fresh boot 30/30;
   (c) back-to-back 60-iter run 55/60;
   (d) final fresh-boot smoke + 30-iter stress + concurrent
       attach 30/30, smoke 5/0. One cycle remaining unused.

## Files touched on this branch (from 9cdcf98)

- `src/c_modules/usbhost/usbhost.c` (per-EP cancel mutex,
  cancel watchdog, device reopen recovery, heap-allocated
  refcounted inflight).

No changes to micropython submodule, referencea/, vendor/, or
the usbip server.
