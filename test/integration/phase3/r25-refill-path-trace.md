# R25 refill-path trace: where the ISR refills `pending_urb_tailq` slots

## Correction (2026-05-03)

The "Implication for the 165 ms" section near the bottom proposes
that the 165 ms `avg_round` is user-task wakeup latency. That
hypothesis was tested in R25 step 3 by bumping
`USBHOST_WORKER_TASK_PRIORITY` from 9 to 20 and **refuted**:
`avg_round` did not move materially (`r25-tune-idf-bulk-in-plan.md`
step 3 result, commit `4519860`). The HW pipeline-fullness finding
in this doc's main body remains correct, but the speculative
implication at the bottom is contradicted by the subsequent
measurement and should not be taken as a finding. Stage B
(`r25-isr-instrumentation.md`) then narrowed the gap to 11 ms
between consecutive bulk-IN ISR fires; the cause is not yet
established.

## Verdict

**Task-wake hypothesis REFUTED. The refill is in-ISR, synchronous, and
correct.**

`_intr_hdlr_chan` calls `_buffer_fill` immediately after `_buffer_parse`,
all in ISR context, before the ISR exits. The previously-free buffer
slot is reloaded with the next URB from `pending_urb_tailq` without
ever yielding to task context. The pipeline does NOT empty between
URBs as long as `pending_urb_tailq` has URBs available.

This rules out the simplest "task-wake gap" explanation for the 165 ms
`avg_round`. The 10.8 ms-per-URB cost lives somewhere else; the
strongest remaining candidate is the **measurement instrument itself**
(see "Implication for the 165 ms" at the bottom).

## Q1: is the slot refilled in the same ISR as `_buffer_exec`?

**Yes.** `_intr_hdlr_chan` (`hcd_dwc.c:841-907`) handles the
`USB_DWC_HAL_CHAN_EVENT_CPLT` case as a four-step sequence, all in
ISR context:

```c
case USB_DWC_HAL_CHAN_EVENT_CPLT: {
    if (!_buffer_check_done(pipe)) {
        _buffer_exec_cont(pipe);
        break;
    }
    pipe->last_event = HCD_PIPE_EVENT_URB_DONE;
    event = pipe->last_event;
    int stop_idx = usb_dwc_hal_chan_get_qtd_idx(chan_obj);
    _buffer_done(pipe, stop_idx, pipe->last_event, false);   // line 856
    if (_buffer_can_exec(pipe) && pipe->port->flags.conn_dev_ena) {
        _buffer_exec(pipe);                                  // line 860
    }
    _buffer_parse(pipe);                                     // line 863
    if (_buffer_can_fill(pipe) && pipe->port->flags.conn_dev_ena) {
        _buffer_fill(pipe);                                  // line 867
    }
    break;
}
```
- `hcd_dwc.c:847-869`

For bulk, `_buffer_check_done` always returns true
(`hcd_dwc.c:391-396`, the `!= USB_DWC_XFER_TYPE_CTRL` short-circuit),
so the ISR always proceeds through the full done -> exec -> parse ->
fill sequence.

State-counter sequence on a bulk-IN completion when both buffers were
in use (steady state):

1. `_buffer_done` (`hcd_dwc.c:425-437`):
   `buffer_num_to_exec--`, `buffer_num_to_parse++`,
   `buffer_is_executing = 0`. Slot is NOT yet fillable
   (`buffer_num_to_fill` unchanged).
2. `_buffer_can_exec` (`hcd_dwc.c:361-369`): TRUE because the OTHER
   buffer was pre-filled (`buffer_num_to_exec == 1`). 
3. `_buffer_exec` (`hcd_dwc.c:2299-2342`): writes the next QTD list
   address and enables the channel via `usb_dwc_hal_chan_activate`
   (`usb_dwc_hal.c:395-407`). Non-blocking; HW starts URB N+1 on the
   wire.
4. `_buffer_parse` (`hcd_dwc.c:2532-2583`): line 2582 increments
   `buffer_num_to_fill`. Slot N is now refillable.
5. `_buffer_can_fill` (`hcd_dwc.c:331-339`): TRUE iff
   `num_urb_pending > 0` AND `buffer_num_to_fill > 0`. With a deep
   `pending_urb_tailq` from the application's lane task pumping URBs,
   this is true.
6. `_buffer_fill` (`hcd_dwc.c:2223-2297`): `IRAM_ATTR`, takes head of
   `pending_urb_tailq`, decrements `buffer_num_to_fill`, increments
   `buffer_num_to_exec`. Slot is now pre-filled with URB N+2 ready to
   be executed when URB N+1 completes.

All in ISR context. No task wakeup needed for the pipeline to remain
full.

## Q2: every `_buffer_fill` callsite

Three call sites in `hcd_dwc.c`:

1. **`_intr_hdlr_chan` line 867** - ISR context (the steady-state path
   above).
2. **`_pipe_cmd_clear` line 1859** - task context. Used by the clear-halt
   pipe command after a stall to refill all slots from
   `pending_urb_tailq`. Not in the steady-state hot path.
   ```c
   if (pipe->num_urb_pending > 0) {
       while (_buffer_can_fill(pipe)) {
           _buffer_fill(pipe);
       }
   }
   ```
3. **`hcd_urb_enqueue` line 2633** - task context. Called whenever
   user code submits a URB. `hcd_urb_enqueue` first appends the URB to
   `pending_urb_tailq` (line 2629), then opportunistically calls
   `_buffer_fill` if a slot is free, and `_buffer_exec` if the channel
   is idle:
   ```c
   TAILQ_INSERT_TAIL(&pipe->pending_urb_tailq, urb, tailq_entry);
   pipe->num_urb_pending++;
   if (_buffer_can_fill(pipe)) {
       _buffer_fill(pipe);                                    // line 2633
   }
   if (_buffer_can_exec(pipe)) {
       _buffer_exec(pipe);                                    // line 2636
   }
   ```

Both the ISR path and the user-submit path can refill. The ISR path
keeps the pipeline full while URBs are flowing; the submit path takes
over when the queue drains and the channel goes idle.

## Q3: does `usb_host_transfer_submit` fill synchronously?

**Yes, synchronously, in task context.** The chain:

- `usb_host_transfer_submit` (`usb_host.c:1589`) calls
  `usbh_ep_enqueue_urb` (line 1615).
- `usbh_ep_enqueue_urb` (`usbh.c:1561-1579`) does some validation
  then calls `hcd_urb_enqueue` (line 1578).
- `hcd_urb_enqueue` (`hcd_dwc.c:2604-2648`) appends to
  `pending_urb_tailq` (line 2629), then synchronously calls
  `_buffer_fill` (line 2633) and `_buffer_exec` (line 2636) if their
  guards allow. So the URB can land on the wire within the same
  `usb_host_transfer_submit` call if the pipe was idle.

No deferred-work mechanism. No task notification. The whole submit-to-
hardware path is task-context, single function-call chain, protected
by `HCD_ENTER_CRITICAL` at line 2619.

## Q4: precise mechanism coupling URB-N completion to slot-N+2 fill

**(a) ISR-only, immediate.** Per Q1, the ISR's `_intr_hdlr_chan` does
the entire `done -> exec(N+1) -> parse(N) -> fill(N+2)` sequence in
one critical section before the ISR exits. The user task is signalled
via `xSemaphoreGiveFromISR(event_sem)` (the `_unblock_client` chain in
`usb_host.c:197`) but that is only for user-callback delivery; the
HCD's internal pipeline-keeping does not depend on the user task
running.

The only way the slot stays empty after URB N+2 completes is if:

- `pending_urb_tailq` is EMPTY when the ISR runs `_buffer_can_fill`
  (`buffer_num_to_fill > 0` but `num_urb_pending == 0`). Then no fill
  happens and the channel is no longer pre-filled. The next user
  submit goes through `hcd_urb_enqueue` which itself calls
  `_buffer_fill` + `_buffer_exec`, restarting the channel from
  task context.

For our R23/R25 workload, the `pending_urb_tailq` should be deep
(application lane task pumps 16 URBs concurrently with submit
overhead ~50 us each), so this empty-queue case should be the
exception, not the steady state.

## Q5: NUM_BUFFERS hardcoded?

**Hardcoded to 2 with NO Kconfig override.** `#define NUM_BUFFERS 2`
at `hcd_dwc.c:57`.

```
$ grep -n "NUM_BUFFERS" hcd_dwc.c Kconfig
hcd_dwc.c:57:#define NUM_BUFFERS                             2
hcd_dwc.c:183:    dma_buffer_block_t *buffers[NUM_BUFFERS];
hcd_dwc.c:190:            uint32_t wr_idx: 1;
...
(Kconfig: no match)
```

The `wr_idx`, `rd_idx`, `fr_idx` fields are 1-bit bitfields
(`hcd_dwc.c:190-192`) with the comment "Bit width must allow
NUM_BUFFERS to wrap automatically". So bumping NUM_BUFFERS to 4 also
requires widening those bitfields to 2 bits each, plus `buffer_num_to_*`
counters from 2 bits to 3 bits. Non-trivial code change but contained
to lines 57 and 184-198.

There is no per-pipe-type override. The struct allocates exactly
NUM_BUFFERS buffers regardless of bulk/intr/iso/ctrl.

## Implication for the 165 ms `avg_round`

This trace REFUTES the task-wake hypothesis at the slot-refill layer.
The ISR keeps the pipeline full as long as `pending_urb_tailq` has
URBs.

What remains? Re-reading our R23 `avg_round` definition:

- `t_submit_post` = right after `usb_host_transfer_submit` returns.
- `t_complete` = when our `transfer_done_cb` runs.

The chain from URB completion to `transfer_done_cb` is:

1. DWC2 ISR fires (per-URB).
2. `_intr_hdlr_chan` runs the done/exec/parse/fill cycle (ISR).
3. `_intr_hdlr_chan` returns; `intr_hdlr_main` (line 932)
   calls `pipe->callback`, which is `epN_pipe_callback`
   (`usbh.c:485`). That calls `endpoint_callback`
   (`usb_host.c:412`), which adds the EP to a pending list and
   calls `_unblock_client(client_obj, in_isr=true)` ->
   `xSemaphoreGiveFromISR(event_sem)` (`usb_host.c:197`).
4. ISR exits. `portYIELD_FROM_ISR` (line 957) if any task was woken
   with higher priority.
5. The user's task running `usb_host_client_handle_events`
   (`usb_host.c:913`) wakes from `xSemaphoreTake(event_sem)` (line
   929), calls `_handle_pending_ep` (line 758), iterates done URBs,
   and calls `urb->transfer.callback` (line 788) - which is our
   `transfer_done_cb`.

**Step 5 is task-context.** If the user task is at default priority
(~5 in IDF default) and the ESP32-S3 has higher-priority tasks
running (TCP/lwIP at ~18, Wi-Fi at ~23 on PRO_CPU), the wake-to-run
latency from `xSemaphoreGive` (in ISR) to `_handle_pending_ep` actually
running can be tens of milliseconds under load.

Critically: this DOES NOT prevent the HCD from keeping the pipeline
full. The HCD pipelines URB N+1 -> N+2 -> N+3 in ISR context regardless
of how slow the user task is to run `transfer_done_cb`. So:

- The HARDWARE round-trip per URB is ~110 us (per R23 `min_round`).
- The MEASURED `avg_round` of 165 ms is the time from
  `usb_host_transfer_submit` returning until `transfer_done_cb`
  RUNS, which includes the task-wakeup tail.

So our R23 measurement was ALWAYS going to surface the user-task
wakeup latency, NOT the HCD pipeline gap. The pipeline isn't gapped;
it's our callback that's late.

This is consistent with R25 step 4 result: HZ=1000 didn't help.
Tick rate doesn't gate task wakeup; what DOES gate it is task
priority relative to other ready-to-run tasks. The same "task wakeup
delayed by other priority-N+ tasks" pattern is invisible to a
HZ change because each task just yields to the next on completion,
not on tick.

## Smallest patch IF this hypothesis were correct (it isn't)

Not applicable: the ISR-refill is already correct.

## Proposed next investigation

The 165 ms is not in the HCD pipeline. It is in the user-task wakeup
+ `_handle_pending_ep` + `transfer_done_cb` chain. Three concrete tests:

1. **Bump our user task priority.** The task that calls
   `usb_host_client_handle_events` is `usb_host_worker` in our
   `usbhost.c`. Find its `xTaskCreate*` priority and raise it well
   above lwIP/Wi-Fi (try 23+, same tier as Wi-Fi). This is R25 step 3
   in spirit but applied to OUR task, not the IDF's host-lib task.

2. **Add a third instrumentation point**: timestamp inside the IDF
   `endpoint_callback` (`usb_host.c:412`) at the moment of
   `xSemaphoreGiveFromISR(event_sem)`. Compare to our
   `t_complete = transfer_done_cb entry`. The gap is the task-wakeup
   latency. If it's ~10 ms per URB, the hypothesis is confirmed.

3. **Worker-task pinning.** The ESP32-S3 has 2 cores. Wi-Fi runs on
   PRO_CPU. If our `usb_host_worker` is also on PRO_CPU, it competes
   with Wi-Fi RX. Pinning to APP_CPU may halve wake latency.

Both 1 and 3 are application-side changes (no IDF source patching
required). Option 2 needs the source edit but only as instrumentation;
no behavior change.

## Cross-reference

- Pipeline keeps URB N+1 in HW automatically: `hcd_dwc.c:858-867`.
- User task wakeup via `event_sem`: `usb_host.c:197` (give in ISR),
  `usb_host.c:929` (take in task).
- User-callback runs in task context: `usb_host.c:788` (call site
  inside `_handle_pending_ep`).
