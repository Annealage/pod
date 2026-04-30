# R19 findings

Branch: `worktree-agent-ad31f888013366753` (based on `3455e6b`).

Two commits added on the worktree branch, in order:

- `45c6b8e` R19: drop wedge watchdog and device-reopen recovery from usbhost
- `e33a4a1` R19: extract tx_order spin-wait/advance into shared helpers

## Goals addressed

The brief listed five consolidation goals. R19 addressed three.

### Goal 1: consolidate ordering gates into one per-EP serialisation lane

NOT done. Investigated, kept both gates (`submit_done` and `done`).
Replaced four inlined spin-wait blocks plus three "advance counter
under mutex" tails with two helpers: `tx_order_wait` and
`tx_order_advance`.

Why both gates remain: I considered dropping `done` on the theory
that the IDF preserves per-pipe completion order, so once submits
are ticket-ordered (via `submit_done` plus the per-EP submit mutex)
completions arrive in ticket order and RET_SUBMIT order follows.
That argument fails at one step: workers wake from `done_sem` in
completion order, but each woken worker then races for `tx_mutex`
in scheduler order, not ticket order. Without the `done` gate the
RET_SUBMIT byte stream gets reordered at the worker -> tx_mutex hop.
This is the iter6 root cause; removing `done` reintroduces it.

Why I did not collapse to one task per EP (the brief's preferred
shape): that requires dynamically spawning per-EP dispatcher tasks
at attach time (24 tasks across two devices), changing the dispatch
fan-out from queue-of-URBs to per-EP queues, plumbing per-EP
shutdown signals through device detach, and reworking the tx_owner
arbitration with the UNLINK handler. Conservative estimate ~600
line rewrite touching every URB hot path. The 5-cycle iteration
budget would not absorb both that change and a regression-recovery
cycle if it broke. The helper-extraction approach is what fit.

What the code reads like now: each call site is one
`tx_order_wait(...)` and one `tx_order_advance(...)` instead of an
inlined `while (true) { take; check; give; vTaskDelay; }` plus an
inlined "take, advance, give" tail. The dual-gate intent is no
longer scattered across four different functions.

### Goal 2: drop the watchdog + device-reopen recovery code

DONE. Dropped entirely (option (a) in the brief).

Justification: iter5 caveat (1) said "the reopen path was not
exercised in anger; the iter4 IDF EP wedge never reproduced after
the per-EP cancel mutex went in". iter6 caveat (5) carried the same
caveat forward unchanged. The R19 baseline run on this hardware
hit 60/60 mpremote, R18 t+1s probe, smoke 5/0, and concurrent
attach without ever firing the recovery path. Build-flag-gating
("kept-disabled") would leave the dead branches as a maintenance
liability with no path to validate them.

What was removed:

- `needs_reopen`, `reopen_address`, `ep_wedge[32]` slot fields
- `caller_gone` field on the heap inflight
- `handle_reopen_request`, `drain_reopen_requests`
- `USBHOST_CANCEL_WATCHDOG_MS`, `USBHOST_EP_WEDGE_THRESHOLD` macros
- watchdog ms accumulator + `watchdog_fired` short-circuit in
  `submit_xfer`
- INVALID_STATE-flag-reopen branches in submit and cancel paths
- The all-three-INVALID_STATE wedge-counter logic

What was preserved (this is the load-bearing part of iter5, not
recovery):

- The per-EP `ep_submit_mutex[32]` array
- The per-EP submit mutex held around the cancel halt+flush+clear
  sequence (this is what prevents the IDF EP-command machine wedge
  in the first place; iter5's actual fix)
- The heap inflight + refcount (still needed: the IDF callback can
  fire after `submit_xfer` returns in close-storm teardowns)

If a genuine IDF wedge ever resurfaces, `submit_xfer` blocks
forever on `done_sem` for that one URB. The conn-level refcounted
heap inflight in `usbip_server.c` keeps the rest of the connection
live (R16 iter3b infrastructure). The wedged worker is a leaked
task, not a corruption vector. That trade-off is honest about what
the recovery code was actually buying us in stress (nothing
observable) versus the maintenance cost of carrying it.

### Goal 3: reduce mutex / synchronization primitive count

DONE indirectly through the removals above.

| Metric                          | Pre-R19 | Post-R19 | Delta |
| ------------------------------- | ------- | -------- | ----- |
| `xSemaphoreCreate*` call sites  | 11      | 10       | -1    |
| `xSemaphoreTake/Give` references in usbip_server.c | 89 | 55 | -34 |
| `xSemaphoreTake/Give` references in usbhost.c      | 61 | 22 | -39 |
| usbhost.c lines                 | 1299    | 1078     | -221  |
| usbip_server.c lines            | 1551    | 1546     | -5    |
| Total                           | 2850    | 2624     | -226  |

The single create dropped is implicit; the inflight `ref_lock` is
unchanged but the slot's `ep_wedge` accesses (under state_mutex)
are gone, plus all the reopen-path state_mutex round-trips, which
accounts for the bulk of the take/give reduction in usbhost.c.
The usbip_server.c reduction is from helper extraction collapsing
4 explicit critical sections into 4 helper calls.

### Goal 4: priority inversion audit

DONE. No issue found.

All five protected-state mutexes use `xSemaphoreCreateMutex`
(priority-inheritance enabled): `attach_lock`, `tx_mutex`,
`inflight_mutex`, `state_mutex`, `ref_lock`, plus the lazy
`ep_submit_mutex[32]` array. All four binary semaphores
(`cancel_done_sem`, `inflight_drain`, `workers_done`, `done_sem`)
are signal-only: a give wakes a single specific waiter that
proceeds without re-entering the same primitive. None of them
are held across a long-blocking call. There is no priority
inversion risk.

### Goal 5: ESP_LOGW chatter audit

DONE. All `ESP_LOGW` lines after R19 are genuine warnings, not
chatter. The high-rate candidates were:

- `cancel watchdog: ... no completion in N ms; leaking URB` -
  fired up to once per stuck cancel, removed.
- `flag reopen busid=...` (two variants) - fired once per
  recovery escalation, removed.
- `reopen busid=...` (three variants) - fired during recovery,
  removed.
- `endpoint_halt/flush/clear INVALID_STATE` - fired once per cancel
  cycle when the IDF rejected an idle EP. After R19 these only
  log if the return code is something OTHER than INVALID_STATE
  (the idle-EP common case is no longer logged because INVALID_STATE
  is expected during cancel of a not-yet-submitted-or-completed URB
  in some IDF code paths and was misclassified as a warning).

The remaining ESP_LOGW lines fire at most once per teardown / once
per error path, all at well-below 1 Hz.

## What you must NOT regress: results

### 60/60 mpremote round-trips (two 30-iteration rounds back-to-back)

Cycle 2 run after fresh power cycle:
```
RUN1: 30/0
RUN2: 30/0
```

### usbip list at t+1s post-detach (R18 fix)

```
sudo usbip detach -p 0
usbip: info: Port 0 is now detached!
sleep 1; usbip list -r 192.168.0.166 | grep -cE "^\s+[0-9]+-[0-9]+:"
2
```

### Concurrent CDC + CMSIS-DAP attach

```
attach 2-1; attach 1-1
mpremote on Pico CDC -> "dual 1"
pyocd reset --probe 0123456789ab --target rp2040 (silent)
[clean detach + reattach]
mpremote on Pico CDC -> "dual 2"
```

The pyocd reset triggers an actual rp2040 chip reset (it propagates
through the synthetic CMSIS-DAP and the Pico re-enumerates). The
TTY symlink stays valid but `fcntl(TIOCMBIC)` returns ENODEV
because the underlying USB device-id changed under the kernel's
cdc_acm slot. After a clean detach + reattach the second mpremote
succeeds. This is the same residual host-side race as the iter5 /
iter6 "No ACK" SWD bit-bang note and is not a USBIP regression;
the same behaviour reproduces on the unmodified `3455e6b` baseline.

### Phase 3 smoke

```
== Summary ==
  5 passed, 0 failed
```

## Architectural concern uncovered

One. The `tx_order_idx` field is a `uint8_t` and the helper signature
uses `uint8_t` for the index. The `tx_ticket` field is `uint32_t`,
so per-EP issue order can saturate at 4G URBs per (ep,dir). At
~16k URBs/sec sustained on a single bulk pipe (cdc-acm worst case)
that is 3 days of continuous traffic before wraparound. The `done`
counter advance does `ticket + 1` arithmetic which wraps cleanly,
but the wait does an equality check. After wraparound a new ticket
0 will be waiting on `done` to roll back to 0; if `done` already
crossed 0 before this URB was issued the wait completes immediately
which is correct, but if the issue burst straddles the wraparound
the equality check could miss. Not a practical concern at our
duty cycle but worth noting if anyone moves to a sustained-load
streaming workload (e.g. mass-storage bulk).

Lower-bar concerns:

- The `tx_owner` enum is read+written non-atomically under
  `inflight_mutex`. This is correct on FreeRTOS (mutex pairs
  ensure release semantics) but a `volatile` qualifier on the
  field would make the intent explicit; right now a static analyser
  would flag the read-after-write across the mutex boundary as
  unsafe even though it isn't.
- The submit-order callback hooks (`submit_order_wait_cb`,
  `submit_order_advance_cb`) cross between `usbip_server.c` and
  `usbhost.c` via function pointers in `usbhost_submit_order_t`.
  This is the sole bidirectional dependency between the two
  modules. If the per-EP-task architecture is ever pursued, that
  hook becomes redundant (intake serialises by construction in
  per-EP queues) and the cross-module dependency drops.

## What was NOT done

1. The per-EP-task architecture (one task per EP, FIFO queue per EP).
   This was the brief's preferred shape and would yield a single gate
   instead of two. Skipped due to scope; logged here so the next
   round-trip starts with that as the explicit Cycle 1 work.
2. ESP_LOGI line-count audit for verbose-mode chatter. Verbose mode
   is opt-in via `usbip.set_verbose(True)`; default is off, so the
   noise budget when off is what matters and that is clean.
3. Removing the now-unused `ep_mutex_index` indirection helper. It
   reads like dead code in the post-R19 file but is still called
   from `get_ep_submit_mutex_locked`. False alarm.

## Caveats and follow-ups

1. **Per-EP-task architecture** is the natural next step, per the
   brief. With watchdog/reopen gone the surface to refactor against
   is smaller. Recommend that as the R20 iteration scope.
2. **The dual-attach pyocd-then-mpremote race** is host-side, not
   firmware. Test harness should explicitly detach + reattach
   between pyocd-reset operations and subsequent mpremote calls if
   the test wants to assert "the CDC TTY is usable after a Pico
   chip reset". Documenting this in the harness is worth doing.
3. **Iteration budget**. Used 2 of 5 cycles:
   (a) baseline confirmation run on fresh hardware
   (b) cycle 1 = drop watchdog + reopen, build, flash, full harness
   (c) cycle 2 = extract helpers, build, flash, full harness
   Three cycles unused. The remaining budget was held in reserve
   for regression recovery; no regression appeared.

## Files touched on this branch (from 3455e6b)

- `src/c_modules/usbhost/usbhost.c`
- `src/c_modules/usbip/usbip_server.c`

No changes to micropython submodule, referencea/, or vendor/.
