# R16 iter3 findings

## What I ran in this iteration

Started on `r16-test` (commit 8c9b4a7). Worktree branch was based off
that commit. Two commits added on the worktree branch:

- `4edca91` R16 iter3a: wait unconditionally for workers at connection
  teardown. Reverted in `964b822`.
- `d857ff5` R16 iter3b: heap-allocate conn_state with refcounted
  destroy. Final state of the worktree branch.

## Iter3a (reverted)

Hypothesis: handle_import_request returned while a worker was still
inside run_inflight, leaving the worker dereferencing a stack-freed
conn_state. The 2 s timeout on workers_done made this likely under
close-storm load.

Fix: changed the workers_done wait to portMAX_DELAY.

Result: under stress this caused the device firmware to wedge entirely
(REPL became unresponsive, ESP console silent). The unconditional wait
deadlocks if any worker is wedged inside its IDF call (e.g. an
endpoint halt/flush/clear sequence that did not trigger giveback). The
2 s ceiling was masking that wedged-worker case rather than fixing it.
Reverted.

## Iter3b (committed, final state of branch)

Reframed the same use-after-free risk: heap-allocate `conn_state_t`
with a refcount. The read loop holds 1, each worker holds 1. The
last reference frees the struct. handle_import_request still has a
bounded 8 s wait on workers_done, but on timeout it safely drops its
own reference and returns. A wedged worker still owns its reference
and frees the struct whenever it eventually exits. No more
use-after-free of stack memory regardless of worker behaviour.

Effect on observed failures:

- Stress with single-CDC attach: device no longer wedges (the iter3a
  regression is fully resolved).
- Spec success criteria (5 mpremote round trips after fresh cycle):
  pass.
- Phase3 smoke `bash test/integration/phase3/run.sh 192.168.0.166`:
  5/0.
- Stress beyond 5 round trips: still hits intermittent
  `mpremote could not enter raw repl` rc=124 timeouts at iteration
  10-25 even though the CDC TTY remains usable on a subsequent call.
  Did not fix this.

## Failure A residual

The remaining failure surfaces as the kernel reverting to repeated
EP0 GET_DESCRIPTOR enumeration probes with no bulk traffic. The trace
shows the cdc-acm-driven bulk-IN URBs stop being submitted after some
in-flight burst, replaced by a long stream of EP0 control transfers
(actual_num_bytes patterns 4 / 18 / 20 / 34 = repeated descriptor
re-reads). dmesg around the failure shows
`vhci_hcd: the urb (seqnum N) was already given back` proving that
in some races RET_SUBMIT and RET_UNLINK are still both reaching the
kernel for the same URB.

The atomic tx_owner gate from 8c9b4a7 should prevent that. Static
review of the worker / UNLINK paths against the inflight_mutex did
not surface a leak. Possible remaining causes I did not fully verify:

- A path where `tx_ret_submit` is invoked WITHOUT first claiming
  tx_owner. The intake_submit early-error branches (lines 753-829)
  call tx_ret_submit without going through the gate. They never
  allocate a tx_ticket, so they always send. If any of these races
  with an UNLINK targeting the same seqnum (which would require the
  URB to have been linked, which these branches don't do), I don't
  see a path. Worth re-checking the EBUSY path at 891.

- The tx-order ticket gate is per-(ep,dir). If for some EP the worker
  pool has 24 URBs all on the same (ep,dir), tickets are
  monotonically assigned and done counter advances strictly in order.
  Verified that intake_submit holds inflight_mutex for the issue.
  Verified that run_inflight increments tx_order.done unconditionally
  (whether send_ret_submit or not) so a "lost the race" worker still
  unblocks the next ticket.

- A subtle window in inflight_release_after_cancel: it decs
  cancel_waiters under the mutex and frees if `retired` is true.
  If the worker has just set retired=true and given the sem but the
  UNLINK timed out and didn't take cancel_done_sem... the give from
  worker's `xSemaphoreGive(cancel_done_sem)` happens unconditionally
  if cancel_waiters > 0 at the retire moment. The UNLINK that already
  fell through past xSemaphoreTake's timeout will then have a
  potentially-pending give on a semaphore it later deletes via
  conn_state_release-cascading into vSemaphoreDelete in inflight_free.
  Did not chase this fully.

## Failure B residual

Did reproduce a variant of failure B with iter3b firmware. Sequence:

1. Fresh cycle.
2. attach 2-1 (synthetic) FIRST.
3. attach 1-1 (Pico CDC) SECOND.
4. Both succeed (mpremote `usbip.attached_devices()` returns
   `['2-1', '1-1']`).
5. mpremote any exec on the CDC TTY: timeout (rc=143/124).

In the OPPOSITE order (1-1 first, then 2-1) the 2-1 attach itself is
refused by the server with "Request Completed Successfully" status.
Did not pin this down. The trace UART buffer overflowed under load
before showing why; my brief investigation showed no IMPORT log for
the 2-1 attempt while the 1-1 connection was busy with EP0 traffic,
suggesting the 2-1 connection was reaching the server but failing
before reaching the IMPORT log line (or being silently dropped). The
listen backlog (4) and lwIP socket pool (10) should be sufficient.

If the order-dependence is robust, it points at a real shared-state
issue that needs investigation: synthetic device's read loop runs
intake/run_inflight inline (no worker pool), and shares the read
loop's task with the connection. If both connections have read loops
on the same core (USBIP_TASK_CORE = 1), and the synthetic device's
inline run_inflight does the per-EP tx-order spin (with vTaskDelay),
it may be competing with the real-host connection's workers for the
same CPU slot. Did not measure.

Did NOT observe `usb_kill_urb hang in D state` as described in the
spec for failure B; my mpremote calls returned with rc=143
(timeout-killed) cleanly.

## What is committed

- d857ff5 (final HEAD of worktree branch
  `worktree-agent-ae59ebcf5dd9dffb5`): heap-allocated conn_state with
  refcount, eliminating the stack-use-after-free risk that iter3a
  attempted to address. Smoke 5/0 passes. 5 mpremote round-trips
  after fresh cycle pass. Stress-tested 25+ round-trips passed
  without device wedge in some runs but still hits intermittent rc=124
  timeouts; the device REPL stays alive after each timeout.

## What remains

1. The atomic-ownership-gate must still have a hole. Need a deeper
   examination of the cancel_done_sem give/take ordering with multiple
   in-flight UNLINKs across the same connection. Recommend adding a
   per-URB sequence counter and crash-on-double-RET log to confirm
   which path emits the second giveback.
2. The 2-1 attach refusal under 1-1-first ordering needs root-causing.
   Add explicit ESP_LOGI in the accept loop and at start of
   handle_client and handle_import_request to confirm the second TCP
   connection is actually reaching client_task.
3. Failure mode A (cdc-acm sticking in EP0-enumeration loop after
   N round trips) is independent of teardown; see the trace pattern
   above. Likely needs Linux-side investigation: under what conditions
   does cdc-acm fall back from bulk to control re-enumeration? May
   correlate with a stalled bulk-IN that we did not properly cancel.

## Caveats

Iteration budget exhausted (5 build/flash/test cycles). The iter3b
heap-allocation change is the only commit that should land on
r16-test. iter3a's revert is also on the worktree but is a no-op
relative to 8c9b4a7.
