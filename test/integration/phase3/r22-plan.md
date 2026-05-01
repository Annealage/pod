# R22 plan: pipelined submitter + high-priority responder

Replace the synchronous "submit URB, block on done_sem, send RET_SUBMIT,
loop" pattern with two concurrent paths:

1. **Submitter** (the existing per-EP lane task): pulls URBs from its
   lane queue and calls `usb_host_transfer_submit` non-blocking. Loops
   to the next URB without waiting. Allows up to N URBs in flight per
   pipe (capped via a counting semaphore).
2. **Responder** (new, one per connection, priority 11): drains a
   completion queue fed by the IDF callback. For each completed URB it
   sends RET_SUBMIT under `tx_mutex`, handles tx_owner / UNLINK
   arbitration, and releases an in-flight slot back to the lane.

The IDF callback runs in the IDF worker task at priority 9; it pushes
the completed URB onto the responder's queue and signals. Because the
responder is at priority 11 (above the worker's 9), FreeRTOS preempts
the worker and runs the responder immediately within the same tick.
No `vTaskDelay`, no done_sem wait, no tick-aligned wakeup.

R22 is the architectural follow-up to R20. R20 removed the worker pool
and per-EP gates. R22 removes the per-URB serialisation that R20 left
in place inside `submit_xfer`.

## Why

Today's per-URB cycle on bulk-IN, after R20:

1. Lane task pops URB from its queue.
2. Calls `usbhost_bulk_transfer` -> `submit_xfer` -> `usb_host_transfer_submit` (returns immediately).
3. Blocks on `xSemaphoreTake(done_sem, ...)`.
4. URB completes on wire (~110 us at FS bulk for a 128 B URB).
5. IDF interrupt; IDF worker (prio 9) calls our callback; callback gives done_sem.
6. Lane (prio 5) is ready but does NOT preempt the worker. Lane runs in the next round-robin slot for prio-5, which at HZ=100 is **tick-aligned**.
7. Lane sends RET_SUBMIT under tx_mutex (~1 ms).
8. Lane pops next URB, repeats.

Step 6 is the bottleneck. Wall-clock per URB ~11 ms; observed throughput
11 KiB/s on bulk-IN streaming. The 700+ KiB/s direct-USB benchmark on
the same Pico hardware confirms the wire is not the limit; our wakeup
latency is.

R22 attacks step 6 in two ways at once:

- The responder has priority > IDF worker, so the give-on-callback
  preempts immediately. Sub-tick wakeup, no tick alignment.
- The submitter does not block, so URBs N+1, N+2, ... are already
  on the wire by the time URB N completes. The per-URB wakeup cost
  amortises across the N URBs in flight.

Realistic ceiling: bulk wire (~1.0-1.2 MB/s on FS) minus per-URB TCP
send (~1 ms per URB, possibly coalesceable). At 128 B/URB the per-URB
TCP send is the new floor at ~128 KiB/s; with submit-side pipelining
saturating the wire and completions batched on the responder side the
target is **300-700 KiB/s**, approaching direct-USB.

## What stays the same

- R20 per-EP lane structure: one queue and one submitter task per
  active `(ep, dir)`. Lazy spawn at first URB to a given lane.
- R16 iter5 per-EP submit mutex (`ep_submit_mutex`) around the
  halt+flush+clear cancel sequence.
- R16 iter3b heap-allocated `conn_state_t` with refcount, R16 iter5
  heap-allocated `usbhost_inflight_t`. Wedged-IDF safety unchanged.
- R18 attachment slot release timing.
- `tx_owner` atomic arbitration between RET_SUBMIT and RET_UNLINK.
  Still needed (UNLINK race is independent of dispatch shape).
- The synthetic CMSIS-DAP fast path in `intake_submit` (synthetic
  `data_transfer` callbacks are sub-ms inline). Untouched.

## Target architecture

### Per-connection state additions

```c
typedef struct conn_state {
    /* ... existing fields ... */
    QueueHandle_t      responder_queue;   /* inflight_urb_t* of completed URBs */
    TaskHandle_t       responder_task;
    SemaphoreHandle_t  responder_alive;   /* given when responder exits */
} conn_state_t;
```

`responder_queue` is sized at `USBIP_INFLIGHT_MAX * 2` to hold both
EPs' completions across both directions in worst case.

### Per-lane state additions

```c
typedef struct {
    QueueHandle_t      queue;
    TaskHandle_t       task;
    bool               alive;
    SemaphoreHandle_t  inflight_slots;   /* counting, max=USBIP_PIPELINE_DEPTH */
} per_ep_lane_t;
```

`inflight_slots` is a counting semaphore initialised to N (default 16,
matching the kernel's typical pending count for cdc-acm). The lane
task takes it before each submit. The responder gives it after each
RET_SUBMIT. Lane blocks (back-pressure) if N URBs are already in
flight on this pipe.

### usbhost.c API split

`submit_xfer` is split into two:

```c
/* Non-blocking submit. Caller owns the inflight until the callback
 * delivers it back via cb(ctx, inflight). Returns 0 on accepted by
 * IDF, -EIO on submit failure. */
int usbhost_submit_async(const char busid[USBIP_BUSID_SIZE],
                         uint8_t ep_addr, bool is_control,
                         const usbip_setup_packet_t *setup,
                         const uint8_t *out_data, size_t out_len,
                         uint8_t *in_data, size_t in_capacity,
                         volatile bool *cancel,
                         void (*cb)(void *ctx, int status, size_t in_len),
                         void *ctx);

/* Synchronous halt+flush+clear of a specific endpoint. Used by the
 * UNLINK handler under the per-EP submit mutex to force-cancel an
 * in-flight URB; the IDF will then deliver the cancelled callback. */
void usbhost_cancel_ep(const char busid[USBIP_BUSID_SIZE],
                       uint8_t ep_addr);
```

The existing synchronous `usbhost_bulk_transfer`,
`usbhost_interrupt_transfer`, `usbhost_control_transfer` are kept as
wrappers around the async API for the synthetic device path or any
other consumer that wants the simple shape. They internally use a
local sem and the same `cb` plumbing. Test harness gets a simpler
API too.

### Lane task (post-R22)

```c
static void lane_task(void *arg) {
    per_ep_lane_t *lane = (per_ep_lane_t *)arg;
    while (true) {
        inflight_urb_t *u = NULL;
        if (xQueueReceive(lane->queue, &u, portMAX_DELAY) != pdTRUE) continue;
        if (u == NULL) break;  /* sentinel */

        xSemaphoreTake(lane->inflight_slots, portMAX_DELAY);  /* backpressure */

        /* Hand off to async submit. Completion delivered via
         * lane_completion_cb which pushes onto responder queue. */
        int err = usbhost_submit_async(/* ... */, lane_completion_cb, u);
        if (err < 0) {
            /* Submit failed: IDF won't call cb. Synthesise completion. */
            u->status = err;
            u->in_len = 0;
            xQueueSend(u->conn->responder_queue, &u, portMAX_DELAY);
            xSemaphoreGive(u->conn->responder_sem);
        }
    }
    /* teardown ... */
}

static void lane_completion_cb(void *ctx, int status, size_t in_len) {
    inflight_urb_t *u = (inflight_urb_t *)ctx;
    u->status = status;
    u->in_len = in_len;
    /* Push to responder queue. IDF callback context (worker prio 9). */
    xQueueSend(u->conn->responder_queue, &u, 0);
    /* No notify needed: queue send wakes the responder waiting on it. */
}
```

`lane_completion_cb` runs in the IDF worker context (prio 9). The
responder is blocked on `xQueueReceive(responder_queue, ...)` at
priority 11. The queue-send unblocks the responder; FreeRTOS preempts
the IDF worker; responder runs immediately.

### Responder task

```c
static void responder_task(void *arg) {
    conn_state_t *conn = (conn_state_t *)arg;
    while (true) {
        inflight_urb_t *u = NULL;
        if (xQueueReceive(conn->responder_queue, &u, portMAX_DELAY) != pdTRUE) continue;
        if (u == NULL) break;  /* sentinel */

        /* Same RET_SUBMIT/tx_owner logic as run_inflight today. */
        bool was_cancelled = u->cancel;
        int status = was_cancelled ? -ECONNRESET : u->status;
        size_t in_len = was_cancelled ? 0 : u->in_len;

        bool send_ret_submit = false;
        xSemaphoreTake(conn->inflight_mutex, portMAX_DELAY);
        if (u->tx_owner == TX_OWNER_NONE) {
            u->tx_owner = TX_OWNER_RET_SUBMIT;
            send_ret_submit = true;
        }
        xSemaphoreGive(conn->inflight_mutex);

        if (send_ret_submit) {
            tx_ret_submit(conn, u->hdr.seqnum, /* ... */);
        }

        /* Release the lane's in-flight slot. */
        per_ep_lane_t *lane = &conn->lanes[u->lane_idx];
        xSemaphoreGive(lane->inflight_slots);

        /* Retire. */
        retire_inflight(conn, u);
    }
}
```

The body is essentially the post-callback half of today's
`run_inflight`, lifted out into a separate task at higher priority.

### Cancel / UNLINK flow

The UNLINK handler in the read loop (`handle_urb_stream`) keeps
`inflight_begin_cancel` to set `cancel = true` and increment
`cancel_waiters`. New addition: after setting cancel, the read loop
calls `usbhost_cancel_ep(busid, ep_addr)` which takes the per-EP
submit mutex and runs halt+flush+clear. The IDF then completes the
in-flight URB with a cancelled status; `lane_completion_cb` queues
it; responder picks it up and sends RET_SUBMIT (-ECONNRESET).

`cancel_done_sem` semantics unchanged: read loop waits on it (250 ms
ceiling) so RET_SUBMIT lands before RET_UNLINK.

This moves the halt+flush+clear from inside `submit_xfer`'s polling
loop (R16 iter5 location) into the UNLINK handler. The per-EP submit
mutex is still load-bearing: it serialises the halt/flush/clear
against any concurrent submit from the lane task on the same EP. No
behaviour change to the IDF EP-command machine wedge avoidance.

### Pipeline depth tuning

`USBIP_PIPELINE_DEPTH` (default 16). cdc-acm under load keeps 16
bulk-IN URBs pending; matching that lets the kernel's ring drain
without back-pressure on our side. Cap at 16 to bound memory;
`USBIP_INFLIGHT_MAX` (32 today) covers two simultaneous pipes worst
case.

Override at build time. Workloads that send fewer concurrent URBs
(synthetic CMSIS-DAP, EP0 control) just don't fill the pipeline.

### Refcount simplification (optional)

In R20, `usbhost_inflight_t` carries refcount=2 (caller + IDF
callback) to handle the race where `submit_xfer` returned but the
callback hadn't fired (wedged-IDF). With async submit + responder,
the URB has exactly one owner at any time:

- Lane task while building it pre-submit
- IDF while in flight
- Responder after callback

No concurrent owners means refcount is no longer needed inside
usbhost.c. Drop the `ref` / `ref_lock` fields; free the inflight
once the responder is done.

The wedged-IDF case (callback never fires, ever) leaks the URB into
the IDF for the lifetime of the connection. Same as R19's
acknowledged trade-off: containment over recovery.

This is a follow-up cleanup worth doing in step 5 below.

## Files touched

- `src/c_modules/usbhost/usbhost.c` (~150 line delta: split submit_xfer,
  add usbhost_submit_async, usbhost_cancel_ep)
- `src/c_modules/usbhost/usbhost.h` (~30 line delta: declare async API)
- `src/c_modules/usbip/usbip_server.c` (~250 line delta: lane uses
  async API + completion cb, new responder task, cancel-on-UNLINK
  trigger)

No board files, no manifest, no Python touched. No sdkconfig changes
(in particular: HZ stays 100).

## Implementation order

Five steps, one commit per step on a worktree branch. Build, flash,
smoke between every commit. Each step is bisect-safe.

### Step 1: introduce `usbhost_submit_async` and `usbhost_cancel_ep`

Add the new API alongside the existing synchronous calls. Implement
`usbhost_submit_async` by re-using the existing `submit_xfer`
plumbing internally but exposing a callback-based completion delivery
instead of the done_sem wait. The IDF transfer callback now knows
which user-callback to invoke.

Keep `usbhost_bulk_transfer` etc. as wrappers around the new async
API + a local sem (so they remain synchronous from caller's view).
The synchronous callers (run_inflight today) keep working unchanged.

Build, flash, smoke 5/0, mpremote 30/30. Commit.

### Step 2: introduce per-conn responder + responder_queue

Add `responder_task`, `responder_queue`, `responder_sem` to
`conn_state_t`. Spawn at `handle_import_request` for the real-host
path. Teardown drains queue, sends NULL sentinel, waits on
`responder_alive` (analogous to `workers_done` today).

Responder task remains unused this step (nothing pushes to its
queue). Validates plumbing only: smoke + 30/30 with all R20 paths
still active.

Build, flash, smoke 5/0, mpremote 30/30. Commit.

### Step 3: route lane-task completion through responder

Change the lane task to use `usbhost_submit_async` + a completion
callback that pushes the completed URB onto `responder_queue`.
Move the post-completion logic (tx_owner claim, RET_SUBMIT,
retire) from `run_inflight` into the responder task body.

After this step the lane task no longer waits on done_sem. The lane
task ALSO no longer calls `tx_ret_submit`; that is the responder's
job.

Pipeline depth stays at 1 (counting sem initialised to 1) so behaviour
is functionally equivalent to step 2 — one URB in flight at a time
per pipe, but processed by submitter + responder split. This isolates
the architectural change from the parallelism change.

Build, flash, validate the full regression matrix (smoke 5/0,
mpremote 60/60, R18 t+1s probe, concurrent attach). Throughput
should be roughly equal to baseline (the responder hop adds a queue
send/receive but the priority preemption removes the round-robin
wait, net should be close).

Commit.

### Step 4: open the pipeline (depth=16)

Bump `USBIP_PIPELINE_DEPTH` to 16. The lane task now submits up to 16
URBs concurrently per pipe. The wire stays saturated; completions
queue up on `responder_queue`; responder drains them at priority 11.

This is where the throughput jump lands.

Build, flash, validate full matrix. Run
`cdc_throughput.py` and record numbers. **Expect ~5-30x improvement
on bufsize=256.** If improvement is below 3x, something is wrong;
bisect within step 4 (instrument the responder queue depth, check
mutex contention on tx_mutex, check IDF transfer-submit cost).

Commit.

### Step 5: drop the now-redundant refcount in usbhost_inflight_t

With async submit + responder ownership, the URB has a single owner
at any time. Drop `ref` / `ref_lock` from `usbhost_inflight_t`,
remove `inflight_unref` calls, free the inflight directly when the
responder is done with it.

Pure cleanup. Build, flash, smoke + 30/30. Commit.

After each commit log the build size (`make ... size`) and report
IRAM/DRAM movement. The new responder task adds ~12 KB PSRAM stack
per connection plus a queue and sem; internal-RAM impact is the new
TCB (~256 B per connection). Two simultaneous connections = ~512 B
internal, well within budget.

## Validation

The R20 regression matrix plus throughput probe. All non-negotiable.

| Check | What |
|-------|------|
| A | `bash test/integration/phase3/run.sh 192.168.0.166` -> 5 passed, 0 failed |
| B | mpremote 30/30 back-to-back after fresh power cycle, 0.5 s gap |
| C | `usbip detach -p 0; sleep 1; usbip list -r ... | grep -c` -> 2 |
| D | Concurrent attach: 2-1 then 1-1, mpremote on Pico CDC, pyocd reset, detach+reattach, mpremote again |
| E | Stress: two 30-iteration mpremote rounds back-to-back |
| F | Throughput: `python3 test/integration/phase3/cdc_throughput.py <pico-tty>` |

Captures from the implementing agent should include:

- **Pre-step-1 baseline**: re-run F on `2aed57a` (post-R21) to confirm
  the agent's measurement environment matches the committed
  `cdc_throughput.baseline.txt` (11 KiB/s at bufsize=256 +/- noise).
- **After step 3** (responder, pipeline=1): F should be within 20%
  of baseline.
- **After step 4** (pipeline=16): F is the headline number. Compare
  against the direct-Pico baseline (700 KiB/s). Anything > 100 KiB/s
  is a clear win; > 300 KiB/s is the target; > 500 KiB/s is excellent.

## Caveats and risks

1. **Priority 11 starves Wi-Fi.** Wi-Fi tasks on ESP32 typically run
   at priority 8. Priority 11 is also above lwIP TCP/IP task (5) and
   the MicroPython main task (1). If the responder runs continuously
   under sustained streaming load, Wi-Fi RX or lwIP processing could
   starve, leading to TCP retransmits or USBIP socket buffer
   overruns. Mitigation: responder body is short (`tx_ret_submit`
   does one TCP write of ~150 bytes, plus retire bookkeeping); blocks
   on `xQueueReceive` between URBs. Watch for Wi-Fi disconnects or
   `lwip_writev` returning EAGAIN under sustained streaming. If
   observed, drop responder priority to 10 (still above IDF worker
   prio 9) or wrap each URB processing in a `taskYIELD()` to give
   lower-priority tasks slots.

2. **Cancel race during teardown.** When the connection drops and
   `handle_import_request` sets `conn->shutdown = true`, in-flight
   URBs are cancel-flagged and we want them to drain. With pipelined
   submit there can be 32+ URBs in flight across both pipes. The
   teardown path needs to:
   - take per-EP submit mutex on each active EP, halt+flush+clear it
   - wait for all in-flight URBs to flow through the responder
     (they'll all complete with -ECANCELED status quickly)
   - drain the responder queue
   - join the responder task
   The R20 teardown order (signal cancel, drain inflight, sentinel
   each lane, wait workers_done) is preserved with one addition:
   sentinel the responder queue and wait `responder_alive` after
   lane tasks have exited. The 8 s ceiling from R20 should be plenty.

3. **TCP send under load.** With 16 URBs/pipe in flight and
   completions arriving back-to-back, the responder hits `tx_mutex`
   16 times per ~16 ms wire interval. On LAN-local TCP this is ~15
   KB/s of small writes (header + payload). Should be fine; lwIP
   handles it. If `lwip_writev` starts returning short writes
   (partial write under buffer pressure), the existing `write_all`
   fallback in `tx_ret_submit` handles it. Worth watching for tail
   latency at higher pipeline depths.

4. **The ordering invariant on RET_SUBMIT** must be preserved: for
   any single (ep, dir), RET_SUBMITs must be sent in submit-ticket
   order. With one responder per connection and IDF preserving
   completion order on a single pipe (which preserves submit order),
   the invariant holds. Cross-pipe order does not need to be
   preserved (the kernel keys URBs by seqnum, not arrival order).
   If a future change adds multiple responders or parallel TCP
   sends, this becomes load-bearing again. Document it in the code.

5. **The `cancel` flag is read non-atomically by the responder.** It
   is set by the read loop under `inflight_mutex` and read by the
   responder without the mutex (`bool was_cancelled = u->cancel;`).
   The same race exists today in `run_inflight`. The mutex pair on
   the write side ensures release semantics; the read sees a value
   that is either pre- or post-set. Either is correct because the
   tx_owner claim under the mutex makes the actual decision about
   whether to send RET_SUBMIT. Marking the field `volatile` would
   make the intent explicit (R19 caveat list flagged this).

6. **`usbhost_submit_async` callback context.** The callback fires
   from the IDF worker (priority 9) inside
   `usb_host_client_handle_events`. The IDF docs warn callbacks
   should be short. Our `lane_completion_cb` does one
   `xQueueSend(responder_queue, &u, 0)` and returns; that is short.
   Do NOT add work to the callback in future maintenance; route
   everything through the responder.

7. **The synchronous wrappers around `usbhost_submit_async`** still
   exist for the synthetic-device path's sake (and for any test code
   that wants the simple shape). They use a local sem inside their
   own callback. Their behaviour is functionally identical to the
   pre-R22 synchronous API. If a user-facing consumer of the
   synchronous API exists outside `usbip_server.c`, keep it working.

## Out of scope

- HZ change (FREERTOS_HZ stays 100; R22 attacks the same bottleneck
  via priorities, not via faster ticks).
- TinyUSB pivot (orthogonal architectural choice; both stacks
  pipeline equally well).
- Larger USB transfer sizes / kernel cdc-acm patch (real USB throughput
  is the wire, R22 closes the firmware-side gap to the wire).
- TCP send coalescing across URBs (path B b2 from R21; only do it if
  measurements after step 4 show TCP send is the new bottleneck).
- Removing the synchronous wrappers in usbhost.c (they're cheap and
  used by the synthetic path; no churn).

## Findings file expectations

`test/integration/phase3/r22-findings.md` should match R20's shape:

- branch name, commits added in order
- per-step build/flash/test results
- regression matrix (A through F) per step where measured
- throughput at step 4 vs baseline AND vs the 700 KiB/s direct-USB
  ceiling, with the cdc_throughput.py table for both
- caveats observed (esp. anything in Wi-Fi RX, TCP, mpremote
  responsiveness under streaming load)
- iteration budget used vs five-cycle ceiling

If step 4 lands below 100 KiB/s, the agent must bisect within step 4
and report which sub-piece is the limit (responder queue depth?
tx_mutex contention? IDF submit cost itself? in_buf malloc per URB?).
"It worked but was slower than predicted" without a why is not an
acceptable end state for step 4.
