# R20 plan: per-EP task architecture

Replace the per-connection 24-task worker pool with one task per active
(endpoint, direction) lane. Goal: cut per-IN-URB latency by removing
the queue hop and the `done` ordering gate, and drop the
`submit_order_t` cross-module callback dependency between
`usbip_server.c` and `usbhost.c`.

This is the natural follow-up to R19 (which kept both gates and only
extracted helpers) and was named in `r19-findings.md` §"What was NOT
done" item 1 as the next-iteration scope.

## Why

Today's hot path for one bulk-IN URB looks like this:

1. read loop unpacks header, allocates `inflight_urb_t`, assigns
   `tx_ticket` under `inflight_mutex`, calls `xQueueSend` into
   `submit_queue`.
2. one of 24 worker tasks wakes from `xQueueReceive`, runs
   `run_inflight`.
3. `run_inflight` calls `usbhost_bulk_transfer_ordered` which invokes
   `submit_order_wait_cb` (spin-wait for `submit_done` on this
   `(ep,dir)`), takes the per-EP `ep_submit_mutex`, calls
   `usb_host_transfer_submit`, invokes `submit_order_advance_cb`,
   releases the mutex.
4. IDF callback fires on the host worker task, gives `done_sem`.
5. submit_xfer wakes, returns to `run_inflight`.
6. `run_inflight` calls `tx_order_wait` for the `done` gate, claims
   `tx_owner`, calls `tx_ret_submit` under `tx_mutex`, advances both
   gates.

Per-URB cost we can measure today (rough, from earlier instrumentation):
queue hop ~1-2 ms, `done`-gate spin ~1 ms when the EP is busy,
`tx_mutex` contention up to ~1 ms when 16 IN URBs all complete
together. End-to-end ~3-5 ms per URB device-side. At 64-byte bulk MPS
that floors streaming at ~150-200 KB/s.

If intake serialises by construction (one task per `(ep,dir)`, FIFO
queue), submit order matches arrival order without any ticket gate;
completions on a single pipe arrive in submit order from IDF; the
single task issues `tx_ret_submit` next, so RET_SUBMIT order matches
arrival order without the `done` gate either. Two spin-wait passes go
away, the queue hop goes away, and 23 of 24 task-wake context-switches
per IN URB go away.

## What stays the same

The `tx_owner` arbitration with the UNLINK handler still matters. Two
tasks (the per-EP lane task running `run_inflight`, and the read loop
running `inflight_begin_cancel` -> `tx_ret_unlink`) still race for who
gives the URB back to the kernel. R20 does NOT change this. The
mutex-protected enum claim in `run_inflight` and `handle_urb_stream`'s
UNLINK branch is preserved as-is.

The per-EP `ep_submit_mutex[32]` array in `usbhost.c` also stays. With
one lane task per EP it is no longer load-bearing for submit ordering,
but it still serialises submit vs. cancel halt+flush+clear on the same
EP (R16 iter5 fix; without it the IDF's EP-command machine wedges with
INVALID_STATE). Different invariant, same mutex.

The heap-allocated refcounted `conn_state_t` (R16 iter3b) and the
heap-allocated refcounted `usbhost_inflight_t` (R16 iter5) both stay.
Wedged-task safety is independent of the dispatch shape.

## Target architecture

### Per-connection state

Replace `submit_queue`, `workers_done`, `workers_alive`, and the
`tx_order[32]` array with a `lane_table[32]`:

```c
typedef struct {
    QueueHandle_t queue;       /* inflight_urb_t* drained by lane task */
    TaskHandle_t  task;        /* NULL if lane not yet spawned */
    bool          alive;       /* false after sentinel processed */
} per_ep_lane_t;
```

`tx_order_idx` on `inflight_urb_t` stays (5-bit fold of `ep|dir`); it
indexes `lane_table[]` now instead of `tx_order[]`. `tx_ticket`,
`submit_done_advanced`, and the entire submit-order callback wiring
go away.

### Lane lifecycle

Lazy spawn on first URB to a given `(ep,dir)`. Most devices use 4-6
lanes (cdc-acm: EP0, bulk-IN, bulk-OUT, interrupt-IN; CMSIS-DAP-v2
synthetic: EP0 plus one bulk pair). 32 is the upper bound.

```c
static bool lane_dispatch(conn_state_t *conn, inflight_urb_t *u) {
    uint8_t idx = u->tx_order_idx;
    xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
    per_ep_lane_t *lane = &conn->lanes[idx];
    if (lane->task == NULL) {
        lane->queue = xQueueCreate(USBIP_INFLIGHT_MAX,
                                   sizeof(inflight_urb_t *));
        if (lane->queue == NULL) { /* unlock, fail */ }
        conn->refcount++;          /* lane task holds one ref */
        lane->alive = true;
        if (xTaskCreatePinnedToCoreWithCaps(lane_task, "usbip_l",
                USBIP_LANE_TASK_STACK, lane, USBIP_WORKER_TASK_PRIORITY,
                &lane->task, USBIP_TASK_CORE,
                USBIP_TASK_STACK_CAPS) != pdPASS) {
            /* unwind: free queue, decrement refcount, unlock, fail */
        }
    }
    xSemaphoreGive(conn->inflight_mutex);
    return xQueueSend(lane->queue, &u, 0) == pdTRUE;
}
```

Lane task loops on `xQueueReceive(lane->queue, ...)` calling
`run_inflight` until it pulls a NULL sentinel. Same shape as today's
`submit_worker_task` but bound to one EP.

### Teardown

`handle_import_request`'s teardown path becomes:

1. set `conn->shutdown = true`, mark all inflight URBs cancel = true
   (unchanged).
2. release the attachment slot (R18, unchanged).
3. drain inflight count to 0 with timeout (unchanged).
4. iterate `conn->lanes[]`; for each `alive` lane post a NULL
   sentinel and wait on a per-lane done sem (or one shared sem with a
   counter, same shape as `workers_done` but counted against
   `lanes_alive`). Same 8 s ceiling.
5. release our refcount.

The `workers_done` semaphore stays but is given when the lane-alive
count hits zero rather than worker-alive count.

### Removals

- `submit_queue`, `workers_alive`, `submit_worker_task` -> gone.
- `USBIP_SUBMIT_POOL_SIZE` macro -> gone.
- `tx_order_slot_t`, `conn->tx_order[32]`, `tx_order_wait`,
  `tx_order_advance` -> gone.
- `submit_order_ctx_t`, `submit_order_wait_cb`,
  `submit_order_advance_cb` -> gone.
- `usbhost_submit_order_t` parameter on
  `usbhost_bulk_transfer_ordered` and
  `usbhost_interrupt_transfer_ordered` -> remove the `_ordered`
  variants entirely, fold back into the plain
  `usbhost_bulk_transfer` / `usbhost_interrupt_transfer` calls in
  `run_inflight`.
- `inflight_urb_t::tx_ticket`, `tx_order_idx`,
  `submit_done_advanced` -> gone (idx folds inline at intake).
- `intake_submit`'s "queue saturated" EBUSY branch + tx_order
  spin-and-advance fallback in that branch -> gone.

The bidirectional dependency between `usbip_server.c` and `usbhost.c`
on `usbhost_submit_order_t` (called out as the sole bidirectional
coupling in `r19-findings.md` §"Lower-bar concerns") drops out as a
side effect.

### Inline-vs-task decision

Today EP0 control and any OUT URB run inline in `intake_submit`
because workers were occupied by pending IN reads. With per-EP lanes
EP0 has its own task and OUT has its own task per direction-folded
slot, so the head-of-line argument no longer applies.

Recommendation: route everything through lanes. Simpler dispatch,
single code path. Synthetic devices keep the inline fast-path at the
top of `intake_submit` (their `data_transfer` callbacks are
microsecond latency; the queue + task hop is pure overhead).

If a Sonnet agent finds keeping EP0 inline gives measurably lower
mpremote raw-REPL handshake latency, that is a defensible deviation.
Do not deviate without numbers.

## Files touched

- `src/c_modules/usbip/usbip_server.c` (primary, ~300 line delta)
- `src/c_modules/usbhost/usbhost.c` (remove `_ordered` variants and
  the `submit_order_t` plumbing in `submit_xfer`; ~50 line delta)
- `src/c_modules/usbhost/usbhost.h` (drop `usbhost_submit_order_t`
  and `_ordered` declarations)

No board files, no manifest, no Python touched.

## Implementation order

Sonnet agent should commit per step on a worktree branch, build and
flash and smoke-test between commits, so a regression bisects to one
commit.

1. **Step 1: replumb dispatch.** Add `per_ep_lane_t conn->lanes[32]`,
   `lane_dispatch`, `lane_task`. In `intake_submit` replace the
   `xQueueSend(conn->submit_queue, ...)` and the
   inline-EP0-and-OUT branches with `lane_dispatch(conn, u)` for the
   real-host case. Keep the synthetic inline path as-is. Keep
   `tx_order_*` and `submit_order_t` plumbing live for now.
   Build, flash, smoke (5/0 pass + 5 mpremote round-trips). Commit.
2. **Step 2: drop the done gate.** Remove `tx_order_wait(... false)`
   call in `run_inflight`, remove `tx_order_advance(... done=true)`
   side effects. Remove the `tx_order[32]` `done` field. Build,
   flash, validate (mpremote 30/30, smoke 5/0, concurrent attach).
   Commit.
3. **Step 3: drop the submit gate.** Remove
   `submit_order_wait_cb`/`advance_cb`, the
   `usbhost_submit_order_t` argument, the submit-order callback
   invocations in `submit_xfer`, the `_ordered` API variants. Update
   `run_inflight` to call plain
   `usbhost_bulk_transfer`/`usbhost_interrupt_transfer`. Remove the
   `submit_done` field, the `tx_order_idx` field on inflight (fold
   inline at lane_dispatch time). Build, flash, validate. Commit.
4. **Step 4: drop the worker pool.** Remove `submit_queue`,
   `workers_alive`, `workers_done` (or rename `workers_done` to
   `lanes_done` if reused), `USBIP_SUBMIT_POOL_SIZE`,
   `submit_worker_task`. Update teardown to walk
   `conn->lanes[]`. Build, flash, validate. Commit.
5. **Step 5: cleanup.** Audit comments referencing the old worker
   pool. Update `tx_order_index` to `lane_index`. Remove now-dead
   `inflight_urb_t::tx_ticket`. Build, flash, full validation. Commit.

After each commit log the build size delta (`make BOARD=...
BUILD_VERBOSE=1 size`) and report any IRAM/DRAM movement. The PSRAM
task stack pool moves but internal RAM should not grow.

## Validation: what must NOT regress

Non-negotiable. R20 must clear all of these, on real hardware,
before the branch lands. Commands assume the existing test harness
under `test/integration/phase3/`.

### A. Smoke

```bash
bash test/integration/phase3/run.sh 192.168.0.166
# expect: 5 passed, 0 failed
```

### B. mpremote 30/30 back-to-back

After fresh power cycle:

```bash
mpremote connect /dev/serial/by-id/<pico-cdc> resume \
  exec "import sys; print('hello')"
# repeat 30 times in a script, then 30 more times, all rc=0
```

### C. R18 t+1s post-detach probe

```bash
sudo usbip detach -p 0
sleep 1
usbip list -r 192.168.0.166 | grep -cE "^\s+[0-9]+-[0-9]+:"
# expect: 2
```

### D. Concurrent CDC + CMSIS-DAP attach

```
attach 2-1; attach 1-1
mpremote on the Pico CDC succeeds
pyocd reset --probe 0123456789ab --target rp2040 succeeds
[clean detach + reattach]
mpremote on the Pico CDC succeeds again
```

### E. Stress: 60/60 round-trips, two 30-iteration batches

Same script as R19's "60/60 mpremote round-trips" baseline. Output
should match.

### F. Throughput sanity check (new for R20)

This is the metric R20 is meant to move. Take a baseline reading on
the pre-R20 branch and the R20 branch and report both numbers in the
findings file.

```bash
# device-side: a 64 KiB random buffer in /flash/blob.bin
mpremote connect /dev/serial/by-id/<pico-cdc> resume \
  exec 'import os; f=open("/blob.bin","rb"); \
        import time; t0=time.ticks_ms(); \
        n=0
        while True:
          d=f.read(512)
          if not d: break
          n+=len(d)
        print(n, time.ticks_diff(time.ticks_ms(), t0))'
```

Expected: ~2-3x improvement in bytes/sec on the read side. Absolute
target depends on Pico FS bulk peak (~1 MB/s wire); a credible
post-R20 number is 400-700 KB/s. Numbers below pre-R20 baseline are
a regression.

## Iteration budget

Five build/flash/test cycles, same as R16 -> R19. Five steps above
maps neatly to that cap if no step regresses; hold one cycle in
reserve. If step 2 (drop done gate) regresses, that's the highest-risk
step (the iter6 fix covered exactly this gate); revert and reframe.

## Caveats and known risks

1. **The done gate was load-bearing in R16 iter6.** Read
   `r16-iter6-findings.md` before deleting it. The exact failure was
   workers waking from `done_sem` in completion order then racing for
   `tx_mutex` in scheduler order, scrambling the RET_SUBMIT byte
   stream. Per-EP lanes serialise that race by construction (one
   task per EP -> one `tx_ret_submit` call at a time on each pipe).
   If R20 reproduces iter6's "scrambled cdc-acm IN bytes" symptom,
   either the lane mapping is wrong (two EPs sharing a lane) or the
   lane task is not strictly serial. Bisect to step 2.

2. **Lazy spawn under `inflight_mutex`.** `xTaskCreatePinnedToCoreWithCaps`
   is not lightweight; running it under the connection's hot mutex on
   first URB to each EP adds ~1-5 ms latency on those specific URBs.
   Acceptable (one-time cost per EP per attach). If an agent finds it
   measurable, a fix is to spawn lanes eagerly at attach for the EPs
   listed in the device's interface descriptors; `usbhost.c` already
   walks them in `read_endpoint_descriptors`. Enumerate -> pre-spawn
   loop in `handle_import_request`. Defer unless measured.

3. **Stack budget.** 32 lanes * 8 KiB stack = 256 KiB PSRAM per
   active connection. Two connections = 512 KiB. PSRAM has 8 MiB
   free, ample. Internal RAM impact: 32 task TCBs (~128 B each) per
   connection = 4 KiB internal. Compare to 24 worker TCBs today =
   3 KiB. Net +1 KiB internal per connection. Acceptable.

4. **EP0 routed through a lane.** EP0 today completes inline in
   `intake_submit`. After R20 it goes through `lanes[idx_for_ep0]`.
   The very first EP0 URB on a fresh connection pays the lane-spawn
   cost; that's also the URB that does
   `SET_CONFIGURATION`/`GET_DESCRIPTOR` so the kernel waits for it
   anyway. No protocol-level concern. If raw-REPL handshake latency
   regresses (it depends on EP0 + bulk turnaround), see caveat (2).

5. **OUT URBs through a lane.** Today OUT runs inline; the read loop
   does not return until OUT completes. Effect: read loop pacing is
   coupled to OUT pacing. After R20 the read loop returns
   immediately after queueing. The kernel keeps issuing OUT URBs
   without waiting for prior ACKs. This is the correct USB/IP
   semantic (kernel batches), but it's a behaviour change worth
   noting if a test relies on inline OUT timing. Smoke + 30/30 should
   catch a regression here.

6. **Per-EP-lane task ID logging.** Verbose mode (`s_urb_verbose`)
   currently logs `usbip_dispatch:` with no task identifier. With one
   task per EP a task name suffix (`usbip_l_8x` for `ep=0x08, IN`) is
   useful for trace analysis. Set the task name in
   `xTaskCreatePinnedToCoreWithCaps` to a per-lane string built from
   the index.

## Out of scope for R20

- Anything in `usbhost.c` beyond removing the submit-order callback
  plumbing (no IDF-side changes, no ep_submit_mutex changes, no
  enumeration changes).
- Larger USB transfer sizes -> R21.
- Removing the `attachment_acquire` slot table -> not asked.
- Removing the synthetic-device inline fast-path -> not worth the
  churn.

## Findings file expectations

`test/integration/phase3/r20-findings.md` should be written by the
implementing agent with the same shape as R19's findings file:

- branch name, commits added in order
- one section per step actually executed, with build/flash/test
  results
- regression matrix (A through F above) with pass/fail per step
- throughput numbers from probe F, pre and post
- caveats observed (anything not predicted in this plan)
- iteration budget used vs. five-cycle ceiling

If the agent skips a step (e.g. cycle budget runs out before step 5),
say so explicitly with what's left and what state it's left in. Half
of step 5 silently merged is the worst outcome.
