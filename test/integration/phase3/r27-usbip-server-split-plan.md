# Structural split plan for `src/c_modules/usbip/usbip_server.c`

## Status

**Deferred.** The plan below is execution-ready; the actual split is held until either (a) someone is about to make a large change to the file anyway, or (b) the post-R27 measurement work (Step 4) decides to remove lane tasks, which would touch most of these boundaries anyway.

## Why deferred

The 2073-LOC file is hard to review but not blocking anyone today (not in an upstream PR). Executing the split is a 2-3 day refactor with substantial cross-cutting `static` state that needs hardware validation to land safely. Doing it now would change the diff burden of any in-flight branches and produce no immediate benefit. Doing it together with the lane-task removal (if Step 4 lands there) folds two refactors into one.

## Target shape

Same pattern as the TinyUSB example (upstream PR #3637), adapted for mpy-pod's extra concerns. Six `.c` files plus a private internal header.

| File | Source range | Approx LOC | Responsibility |
|---|---|---|---|
| `usbip_internal.h` | new | ~150 | shared types, externs for cross-file static state, prototypes. |
| `usbip_server.c` | top, accept loop, public API | ~280 | server task, client task, accept, lifecycle, public entry points. |
| `usbip_attachment.c` | 156-214 | ~60 | attachment table (acquire/release/list). |
| `usbip_io.c` | 216-327, 595-649 | ~250 | byte stream (read/write/writev/discard), send_op_common, send_device_with_interfaces, tx_ret_submit, tx_ret_unlink. |
| `usbip_ops.c` | 329-373, 1621-1791 | ~330 | DEVLIST + IMPORT op handlers, collect_all_devices, find_device_by_busid. |
| `usbip_inflight.c` | 500-553, 554-594, 654-840 | ~370 | inflight pool: link/unlink/free, alloc, begin_cancel, release_after_cancel, run_inflight, outstanding_drain_wait. |
| `usbip_lane.c` | 841-996, 1248-1327 | ~500 | lane_index, lane_completion_cb, lane_task, lane_dispatch. |
| `usbip_intake.c` | 1328-1620 | ~290 | intake_submit, handle_urb_stream, read_out_payload. |
| `usbip_responder.c` | 998-1247 | ~250 | responder_task (batched RET_SUBMIT under tx_mutex). |

## Internal header contents

`usbip_internal.h` exposes the shared state and cross-file functions. Static encapsulation is lost for these symbols but they remain file-internal to the usbip module (the public header `usbip_server.h` stays unchanged).

Types:
- `client_slot_t`, `usbip_server_state_t`
- `conn_state_t` (with `inflight_count`, `tx_mutex`, `inflight_mutex`, `lanes[]`)
- `inflight_urb_t`
- `lane_t`, `lane_task_arg_t`

Externs:
- `s_state` (server-wide state)
- `s_urb_verbose` (verbose logging flag)
- `TAG` (logging tag)

Function declarations (cross-file):
- I/O: `read_exact`, `write_all`, `writev_all`, `discard_exact`, `send_op_common`, `send_device_with_interfaces`, `tx_ret_submit`, `tx_ret_unlink`
- Conn lifecycle: `conn_state_free`, `conn_state_release`
- Inflight pool: `inflight_link`, `inflight_unlink`, `inflight_free`, `inflight_begin_cancel`, `inflight_release_after_cancel`, `run_inflight`, `outstanding_drain_wait`
- Lane: `lane_index`, `lane_completion_cb`, `lane_dispatch`
- Intake: `intake_submit`, `handle_urb_stream`, `read_out_payload`
- Ops: `handle_devlist_request`, `handle_import_request`, `collect_all_devices`, `find_device_by_busid`
- Attachment: `attachment_acquire`, `attachment_release`

## Sequenced execution

Bottom-up, one extraction per commit so bisect stays useful:

1. **Add `usbip_internal.h`.** Move type definitions; declare cross-file externs and prototypes. Original `usbip_server.c` includes it. Build still green.
2. **Extract `usbip_io.c`.** Move byte stream + tx wrappers. ~250 LOC out.
3. **Extract `usbip_attachment.c`.** Move attachment table. ~60 LOC out.
4. **Extract `usbip_ops.c`.** Move DEVLIST + IMPORT handlers. ~330 LOC out.
5. **Extract `usbip_inflight.c`.** Move pool + `run_inflight` + `outstanding_drain_wait`. ~370 LOC out.
6. **Extract `usbip_lane.c`.** Move lane infrastructure. ~500 LOC out.
7. **Extract `usbip_intake.c`.** Move intake + `handle_urb_stream`. ~290 LOC out.
8. **Extract `usbip_responder.c`.** Move responder task. ~250 LOC out.
9. **Trim `usbip_server.c`.** What remains is accept loop, server task, public API. ~280 LOC.

Update `micropython.cmake` after each extraction to add the new `.c` to the source list.

## Verification per step

Run after each commit:

```sh
test/unit/usbip/run.sh              # protocol parser unit tests
test/unit/usbhost/run.sh            # host stub unit tests
# Build esp32-s3 firmware
cd src/micropython/ports/esp32 && idf.py -B build-annealage_pod build
# HIL smoke (after flash): mpremote eval through USB/IP
test/integration/phase3/cdc_throughput.py /dev/ttyACM<N>
```

The unit tests catch protocol-level breakage; the HIL smoke confirms the lane / inflight / responder state machine still wires up correctly. Skip the HIL step on commits that move only protocol-side code.

## Risks

- **Lost static encapsulation.** Several large statics (`s_state`, `s_urb_verbose`) and many internal helpers become global symbols visible across the usbip module. Mitigated by keeping them in `usbip_internal.h` (not the public `usbip_server.h`), but a determined misuse by another module could link against them.
- **Header tangles.** `usbip_internal.h` must include enough to compile each `.c` standalone but not so much that it pulls FreeRTOS / lwip into protocol-side translation units. Curate the includes carefully.
- **Cross-cutting refactor breakage.** Moving `run_inflight` out of the file body while `lane_task` calls it requires the function to become non-static. That's a one-line change but easy to miss.
- **Bisect friendliness.** If commit N breaks the build, commits N+1..M will all fail to bisect. Each commit must build clean.

## Open question for measurement (Step 4)

If the measurement decides to remove lane tasks in favour of the queue-in-completion pattern from the TinyUSB example, the split layout changes:

- `usbip_lane.c` and `usbip_responder.c` disappear.
- The completion-side drain moves into `usbip_inflight.c`.
- Net: 4 .c files instead of 6, much closer to the TinyUSB example's structure.

Worth completing Step 4 before executing Step 5.
