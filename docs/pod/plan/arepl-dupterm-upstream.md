# The socket-REPL park, and what happened to the fix for it

**Status: on hold. Neither branch in this document should be pushed.** The
upstream author reworked their own fix in a way that supersedes one of the two
branches here and breaks the mechanism of the other. What remains open is a
scope decision for Andrew, described below.

## Two faults, not one

They were conflated for a long time and the distinction matters, because one is
fixed upstream and the other is by design.

**Fault A, the park.** A client that disconnects from the pod's socket REPL
stops the pod's single-core asyncio loop outright. The accept loop, Wi-Fi
supervisor and UART bridge all stop being scheduled; the listener stays bound, so
connections still complete their TCP handshake and are then never served. From
the host that looks like a pod that answers and says nothing, and only a reset
clears it. `mpremote` and `ampremote` never trigger it because they send Ctrl-B
before closing.

**Fault B, the uninterruptible synchronous line.** A busy device cannot be
interrupted over the socket REPL at all. The same Ctrl-C that aborts a
`time.sleep(12)` over the probe UART in 1.8s is ignored over the socket and the
sleep runs to completion. The interrupt-char check lives inside the dupterm
read, so it only runs when something reads stdin; a busy synchronous statement
never reads stdin, so its Ctrl-C is never seen. UART and USB-CDC do not have that
dependency because their RX path schedules the interrupt directly whatever the VM
is doing.

Fault B is a property of a synchronous blocking stdin read over a socket-only
console. It is not an arepl bug, no change in `arepl.py` can reach it, and it
stays after fault A is fixed. Anyone reading only about fault A would expect a
runaway `while True: pass` to become recoverable. It does not.

## The mechanism of fault A, as finally established

The author's account, which supersedes the model this document previously
carried:

arepl's main loop is `await StreamReader(sys.stdin).read(1)`. asyncio's
`Stream.read` assumes a non-blocking stream, but `sys.stdin.read` goes to
`stdio_read`, which is fully blocking via `mp_hal_stdin_rx_chr`. A dupterm socket
at EOF or RST makes `modlwip` poll report `POLL_RD`, so the StreamReader wakes,
calls the blocking read, that read consumes the EOF, deactivates the slot, has no
byte to return, and parks the whole loop in `mp_event_wait` forever. There is no
other stdin source, and the frozen accept task cannot dup a new client in to wake
it.

So the park is in the **outer poll-driven read**, not in the raw-mode branch. An
earlier model held that the outer loop awaits and therefore survives, and that
the park was specific to raw mode. That was wrong in a way that mattered: it
pointed the fix at the wrong layer.

## What happened to the two branches

| branch | status |
| --- | --- |
| `os-dupterm-generation` | **Dead.** Superseded by the author's rework. Do not push. |
| `arepl-raw-mode-disconnect` | **On hold.** Fixes a real gap, but its detection no longer fires. Do not push. |

`os-dupterm-generation` published a counter so a blocked reader could tell "no
byte yet" from "source gone". Its whole premise was that the author's fix
*masked* a dead slot's poll flags while leaving the slot attached, so nothing read
it, so it was never deactivated, so nothing could report it. The author has since
rewritten that commit to drop the slot outright rather than mask it. The blind
spot the counter existed to report no longer exists.

`arepl-raw-mode-disconnect` bounded the raw-mode read and consulted that counter.
The gap it addresses is real and still open: the author's fix stops a
*poll-driven* reader being woken into the stranding read, but arepl's raw branch
calls `sys.stdin.read(1)` with nothing polled first, so the fix never engages
there and a raw-mode disconnect still parks. The author verified this
independently: `stdio_read` never returns short, so the `if not ch: return` below
that read is dead code and arepl has no EOF path at all, and rp2's
`mp_hal_stdin_rx_chr` is a `for(;;)` around `mp_event_wait_indefinite`.

But the branch cannot ship as written. Against the reworked base the drop path
nulls the slot directly and never reaches `mp_os_deactivate`, so any generation
counter stays put. The bounded loop would poll every 500 ms forever without
yielding, which is worse than the park because it looks alive.

## What is open

Whether fault A's raw-mode half gets a real fix or is grouped into the fault B
limitation note. That is Andrew's scope call, and it shapes the upstream PR
framing, which is why the author declined to fold anything on their own
initiative.

If it is fixed, it wants designing rather than bolting a generation check onto a
loop that is synchronous on purpose. The raw loop feeds input with no await
precisely so a concurrent task's stdout cannot corrupt a raw or raw-paste
transfer, and that property has to survive whatever replaces the unbounded read.

The smallest thing that would work in the author's current shape is publishing
the drop from inside their own drop branch, so a synchronous reader can observe
it. Whether that is a counter or routing the drop through `mp_os_deactivate` (
avoided deliberately, to keep the poll path cheap) is the author's call.

## SHAs here are volatile

`native_async_repl` has been force-pushed at least twice during this work, and
the dupterm fix has carried a different SHA each time while the file content
stayed byte-identical. Compare by blob id or by subject, not by commit SHA, and
re-resolve before building anything. Do not cherry-pick from it; see the
composition note below.

## How a fix would reach the pod

Not by cherry-pick. `tessera` is composed by `mbm` from the branches registered
in `mbm.toml` at the repository root, and a cherry-pick would give the fix once
and then be dropped by the next recompose. The action is a recompose, after
`native_async_repl` is updated.

Two things gate that, both verified:

**The `lib/tinyusb` pointer is behind what tessera runs.** A recompose today
moves it backwards past the shared-EPX double-arm degrade fix, which is the #74
pod-kill: the usbip forwarder re-arms an endpoint TinyUSB still owns, TinyUSB
panics, and the pod drops off the network needing an SWD reset. Neither candidate
source rescues it:

| source | `lib/tinyusb` |
| --- | --- |
| default, `origin/tinyusb-rp2-host-abort` | `b414cc7d87` |
| `--local`, `tinyusb-rp2-host-abort` | `482039b9f8` |
| what `tessera` runs today | `6250fe7d09` |

Bump the branch pointer to `6250fe7d09` before recomposing. That also retires the
direct `lib/tinyusb` bump currently sitting on `tessera`, which is a stopgap not
present in any registered branch and which any recompose drops.

**The base.** `mbm rebase` targets `upstream/master` by default, which is 307
commits past tessera's base of `562d6be365`. On a board whose only management
channel is the thing under test, that is worth keeping separate from a
composition change: `--target 562d6be365` keeps the recompose to composition
only. Use `--dry-run` first.

After recomposing, check by content rather than trusting the merge list: a marker
per registered branch, `lib/tinyusb` landing on `6250fe7d09` rather than either
candidate source, and the merge-base still being `562d6be365` if the conservative
target was used.

## Reproduction, with the traps

Connect, send Ctrl-A, then close with `SO_LINGER 0` without sending Ctrl-B.

- A probe that enters raw mode and closes politely will not reproduce it.
- Any probe driving the CLI repairs the device it is measuring, because the
  prompt-poll preamble writes before it reads.
- Drive a command to completion as the health check rather than looking for a
  prompt: a leftover raw-mode session answers a prompt probe while being
  unusable.
- The pod also needs `netboot` to restart the REPL task when it returns. If
  `arepl.task()` is the last await in a `main()`, returning ends `asyncio.run()`
  and cancels everything else with it, turning a park into a clean unwind that
  still takes the management plane down. That half is in
  `src/boards/common/netboot.py` and is independent of whatever happens upstream.
