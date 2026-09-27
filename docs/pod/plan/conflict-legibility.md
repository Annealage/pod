# Conflict legibility: name the holder, refuse the bump

When two agents share one pod, every collision should say who holds what, and no arriving caller should be able to displace an incumbent by accident. This plan covers the observational half plus the anti-bump enforcement that falls out of it. It does NOT introduce a reservation protocol; that is `bench-lease.md`, which this plan sits underneath and which is re-scoped to build on the identity piece defined here.

This is a plan, not the code. It supplies **phases 4 to 7 of the agent-surface track** whose ordered index lives in `overview.md`; phases 1 to 3 are the surface reorg in `mcp-surface.md`, which renames and merges the tools this plan's gate attaches to. Phase 4 is name-independent and could land earlier; phases 5 to 7 target the post-reorg names.

## Holder record vs lease

The two are different mechanisms and the distinction decides most of the design:

- **Holder record (this plan).** You become the holder by *using* a resource. Holding is a fact observed, not a right granted. Liveness is derived from the resource itself (a live TCP connection, a live usbip attachment), so there is no TTL, no renewal heartbeat, and no zombie-lock class of failure: when the connection dies the holder record dies with it. A non-holder attempting a displacing operation is refused and told who holds it.
- **Lease (`bench-lease.md`).** A reservation made *ahead of* use, with a TTL, a token, and a steal protocol. Its added power is holding the bench across gaps in activity, which a holder record deliberately cannot do. Its added cost is expiry, renewal, and zombie handling.

Build the holder record first. It removes the collisions that actually occur without asking agents to learn a checkout protocol, and the lease becomes a small increment on top rather than a parallel mechanism.

## Current state, verified

Several of the protections assumed missing are already present on the live RP2350 runtime. Recording them so nobody rebuilds them:

- **Socket REPL already refuses without evicting.** `netboot._repl_accept` (`src/boards/ANNEALAGE_POD_RP2350/netboot.py:138`) serves one client, tracks it in `cur`, and answers a second connection with `annealage-pod: BUSY - REPL in use by another client` before closing it. The incumbent is untouched.
- **usbip already refuses a second import.** `handle_import` refuses when `attachment_acquire` fails, logging `busid '%s' already attached, refusing` (`src/c_modules/usbip/usbip_server.c:1698-1702`), and answers OP_REP_IMPORT with status 1.
- **PIO is arbitrated per (block, sm).** `pio_arbiter.claim` raises `PioConflict` naming the current owner (`src/mpy/annealage_pod/debug/pio_arbiter.py:54-72`).

The gaps are elsewhere, and they are of two kinds: refusals that name no one and are misclassified by the time they reach the agent, and host-side operations that displace an incumbent by tearing its attachment down first.

Note also that `annealage_pod/boot.py` carries a divergent, thread-based `_repl_accept_loop` with `listen(1)` and a blocking inner poll, where a second client's handshake completes into the backlog and then hangs with no BUSY notice (`src/mpy/annealage_pod/boot.py:274-333`). That is the ESP32-S3 path (`src/mpy/main.py`), not the RP2350 one, but it is the same package. Either port the netboot refusal into it or mark it ESP32-S3-only, so the divergence does not get copied back.

## The three real bump vectors

All host-side, all in `src/host/pod/client.py`, all working by detaching the incumbent so the pod's own refusal never fires:

1. **`dut_exec` detaches unconditionally.** `self.usbip_detach()` at `client.py:828`, commented as clearing a stale attachment, detaches *every* host vhci port bound to that DUT, including another agent's live one. Agent B running one `dut_exec` rips away agent A's session, and the pod cooperates because from its side the import was closed normally.
2. **`dut_flash` detaches a live session by default.** `keep_attached=False` (`handle_dut_flash`, `mcp_server.py:475`) is correct for a single user and silently destructive for two.
3. **`dut_link(action="down")` and `bench_device(action="down", name="*")` are unscoped.** The down action detaches every port for the DUT; `peripherals.release("*")` (`src/mpy/annealage_pod/peripherals.py:40-47`) deinits every named instance including another agent's live I2C or SPI target.

Two adjacent problems in the same family:

4. **The PIO arbiter's owner is a subsystem, not a caller.** Owners are strings like `la` and `swd`. Two agents both using the logic analyser both claim owner `la`, the claim is treated as idempotent-for-the-same-owner, and both are admitted. The arbiter's protection is real but invisible to caller-level contention.
5. **SWD has no interlock at all.** `ops._dp/_ap/_cm` (`src/mpy/annealage_pod/debug/ops.py:18-26`) is a singleton driven by whoever calls it. Interleaved transactions from two callers do not fail, they return wrong data. There is no error to make legible, which makes this the one place where legibility and guarding are the same build (see design item 5).

## Design

### 1. Caller identity

No caller identity exists anywhere in the stack: the MCP dispatcher, the `Pod` client, and the pod are all anonymous. Everything else here depends on this.

A label, not a credential. It grants nothing and is not checked for authenticity; its only job is to make a refusal say a useful name. Resolve host-side once per `Pod` construction, in order: the claude-net agent name (agents already carry `session:user@host`, e.g. `usbhost:corona@carbon`) > `POD_CALLER` env > `$USER@$hostname/$pid`. Store it on the client and send it with anything that establishes a holder record.

This is the same resolution `bench-lease.md` § Identity specifies. It moves here; that section becomes a pointer.

### 2. Pod-side holder record, on a control port

The pod is the one point every caller converges on, so the record lives there. A new pure-MP module `annealage_pod.holders`, module globals for the VM lifetime, mirroring `pio_arbiter`'s shape:

```
note(resource, caller, detail="")   record the current holder
drop(resource)                      clear it
who(resource=None)                  -> {resource: {caller, since_s, detail}}
```

Tracked resources and where each is set:

| resource | set by | liveness derived from |
| --- | --- | --- |
| `repl` | `netboot._repl_accept` on attach | the client socket; cleared in the existing dead-detection branch |
| `usbip` | `usbip_server` attachment_acquire | the import TCP connection |
| `swd` | `ops` entry guard (item 5) | the guard's own enter/exit |
| `pio` | existing `pio_arbiter._claims` | unchanged, exposed via `who` |
| `peripherals` | existing `peripherals._INST` | unchanged, exposed via `who` |

`pio` and `peripherals` are read through to their existing registries rather than duplicated, so there is one place each fact lives.

**Served on its own listener, not over the REPL.** This is the load-bearing decision: the REPL is the contended channel, so "hold the REPL to ask who holds the REPL" is a bootstrap knot, and the answer must still arrive while usbip has the pod busy. Bind **port 8267** (free; 8266 REPL, 2000 UART, 3333 flash, 3334 dump, 3335 gdb, 3336 LA), request/response JSON, one line in and one line out, connection closed after. It holds no slot and keeps no client.

Follow the `uart_bridge` pattern exactly, which is the established one for a second listener on this runtime: a synchronous `bind()` from `netboot.main()` before the first mDNS advertise, dual-stack `bind(("::", port))`, `setblocking(False)`, then `asyncio.create_task(serve(...))`, per-cycle exception guard, `await asyncio.sleep_ms` between polls, and the task never exits (`src/mpy/annealage_pod/uart_bridge.py:43-98`, `src/boards/ANNEALAGE_POD_RP2350/netboot.py:255-285`).

Advertise as an mDNS TXT key `control-port`, gated on the bind actually succeeding, the same conditional-key pattern as `uart-port` (`netboot.py:64-82`). The live holder does not go in TXT: mDNS only re-advertises on Wi-Fi reconnect, so a holder in TXT would be stale. Callers query the port.

**This is the port `bench-lease.md` should use too.** It specifies a dedicated lease line-protocol port on 8267 with the same rationale; when the lease lands it adds verbs to this listener rather than binding a second one.

### 3. Host-side gate on the bump vectors

The pod is authority; the host declines to act without checking. Each of the three vectors becomes conditional:

- `dut_exec` asks `who("usbip")` before detaching. If the holder is another caller, refuse with the holder and a suggested action. If it is us or nobody, proceed as today. This also removes a real inefficiency: the current unconditional detach/`sleep(1.5)`/re-attach cycle costs ~2.5s on every call (`client.py:822-843`), and is unnecessary when we already hold the import.
- `dut_flash`, `dut_erase`, `dut_reset`, and `dut_link(action="reprobe")` refuse when another caller holds `usbip` or `swd`, since all of them displace or corrupt a live session.
- `dut_link(action="down")` and `bench_device(action="down")` scope to the calling caller's own resources by default; `"*"` means "all of mine".

Each gets `force=true`, which bumps, logs the eviction on the pod with both caller names, and returns `stole_from` in the result so the acting agent sees in its own transcript that it displaced someone. The evicted agent finds out when its next operation fails against a holder record that no longer names it.

Each agent runs its own `pod-mcp` under stdio transport, so the MCP server process is a clean per-agent boundary and the gate has exactly one place to live.

### 4. Surface honesty

- **`_classify_exec_failure` gains a busy branch.** Today the pod's BUSY line falls through to `raw-REPL entry / connection failed` (`client.py:89-92`), which reads as a transport fault. It must classify as busy and carry the holder. This is the single highest-value item in the plan: a collision currently presents as evidence of a broken pod, and the documented response to a broken pod is reset and power-cycle, which is precisely the destructive recovery `CLAUDE.local.md` forbids.
- **The pod's BUSY line names the holder.** `netboot._repl_accept` has the incumbent's address in hand and should include it plus the caller label.
- **`pod_info` returns the `who` record** for every resource, alongside the registry handles it already carries.
- **Per-process session state stops reading as global truth.** Under stdio transport `_REPL_SESSIONS` (`mcp_server.py:129`) is per-process, so the session list `pod_info` returns is this process's own, and reads as a global view while showing empty during another agent's session. This phase makes that view carry the pod's holder record beside this process's sessions.
- **Structured results.** `call_tool` returns `str(result)` (`mcp_server.py:1617`), a Python dict repr, and returns errors through the same success path with no `isError`, so a failure is indistinguishable from a success whose text begins with `Error:`. Emit JSON and set `isError`. Without this a gate refusal is just more prose for the agent to guess at.

### 5. SWD re-entrancy guard

The only item here that refuses rather than reports, because there is no failure to report: two callers interleaving on the `ops` singleton get silent corruption.

A flag at the `ops` entry points: enter records `(caller, op, since)` in `holders`, exit clears it, a second caller entering while held is refused naming the holder. This is a mutex, not a lease: no expiry, no ownership across calls, no override protocol, released on exit including the exception path.

Note that SWD ops arrive over the socket REPL, which is already single-holder, so a caller holding a `pod_open` or `dut_open` session is implicitly serialised. The exposure is one-shot `pod_exec` sequences, which release the REPL slot between calls and let a second caller interleave mid-sequence. A short sticky window on the `repl` holder record (the record survives disconnect for N seconds, refreshed by the same caller reconnecting, bounced for a different one) closes that without any checkout protocol. This is the one place the design admits a timer, and it should be seconds, not minutes.

## Non-goals

- No reservation ahead of use, no TTL, no renewal, no steal protocol. That is `bench-lease.md`.
- Not a security boundary. `pod_exec` and a human at the REPL are deliberate god-mode and always win; a human poking `ops.flash()` should not be gated.
- No persistence. The record is RAM-only and a reboot means a clean bench, for the same reason the lease is RAM-only: a zombie holder surviving a reboot with no live holder is a worse failure than no record at all.

## Landmines found while scoping this

- **`supervisor._default_cleanup` no longer auto-registers.** It used to fire on every REPL disconnect and power off both DUT rails and open every relay, inert only because `power` and `relays` raise `_pinmap.assert_esp32_carrier` on the RP2350 and `run_cleanup` swallows and prints the exception - a cross-agent destructive side effect waiting for `carrier-hardware.md` to make the rails real. **Fixed**: `register_cleanup(_default_cleanup)` is gone from module import (`src/mpy/annealage_pod/supervisor.py`); a target opts in explicitly if it wants that lone-user power-off-on-disconnect behaviour. `test_nothing_registered_on_import` (`test/unit/annealage_pod/test_supervisor.py`) guards the regression.
- **PIO arbiter owner strings collide across callers** (see bump vector 4). Composing the owner as `subsystem@caller` once identity exists restores the refusal for two agents using the same subsystem, and costs nothing else.

## Phasing

Phases 4 to 7 of the agent-surface track (`overview.md`). Each is independently useful and hardware-validatable.

- **4. Identity + failure classification.** Caller label on the `Pod` client, busy branch in `_classify_exec_failure`, holder name in netboot's BUSY line, JSON + `isError` from `call_tool`. No new listener. Turns a collision from "pod looks dead" into "pod busy, held by X". Gate: two agents collide on the REPL and both transcripts name the other.
- **5. Holder record + control port.** `annealage_pod.holders`, the 8267 listener, `control-port` TXT, and `pod_info` carrying the holder record. Gate: `who` answers correctly while the REPL is held and while usbip has an import open.
- **6. Anti-bump gate.** Landed, unit-tested; hardware validation of the actual cross-host collision (two real attaching hosts) is outstanding - see below. `dut_flash`, `dut_erase` (previously undetached - a gap this phase closed, since `erase_all()` halts the core the same way reset/flash do), `dut_reset`, `dut_halt` (not named in the original vector list, but it shares `_guard_live_attach` with the other three, so it inherited the gate and needed the same `force` escape hatch or it would be an operation that could be refused but never bumped), and `dut_link(action="reprobe")` all refuse via `Pod._gate_usbip` when `usbip_held_by_other()` is true, unless `force=True`; a forced bump adds `stole_from` to the result and logs the eviction on the pod's console (`holders.evict`, print-only - not a second wire-queryable log, since the record overwrite is what tells the evicted caller). `PodConflictError` is the new host-side exception, distinct from `PodExecError.busy`: it fires before any pod round trip, surfaced through `call_tool` as `kind="conflict"`.

  Two findings changed the vector list as originally scoped:
  - **`dut_link(action="down")` needed no change.** `usbip_detach(port=None)` already scopes to `attached_ports()` (this host's own local vhci ports, fixed during the phase-2 review), and usbip's single-import means there is never a second host's port reachable from here to begin with - the vector this bullet described no longer exists.
  - **`bench_device(action="down")` has no pod-side caller field to gate on** (peripherals persist regardless of who created them, unlike repl/usbip/swd). Scoping "my own" is instead tracked host-side, per pod-mcp process (`mcp_server._OWNED_PERIPHERALS`), consistent with each agent already owning a clean process boundary under stdio transport: a wildcard/omitted name on `action="down"` releases only instances this process brought up via `action="up"`; `force=True` sweeps every instance and reports the rest as `stole_from`. This is CLI-scope, not fleet-scope: `pod bench device down` from the CLI is a fresh process every invocation with nothing to track, so it is left at its pre-existing unscoped-sweep default rather than made a permanent no-op.
  - **`force` has no pod-side teeth for usbip specifically.** The pod's usbip server already refuses a second import on its own (see "Current state, verified"); this host cannot sever another host's live vhci attachment from here. `force=True` on `dut_flash`/`dut_erase`/`dut_reset`/`dut_link(reprobe)` skips this host's own pre-emptive refusal and proceeds - which matters because those four operate over SWD, entirely independent of the usbip server's own protection, so a forced flash/erase/reset/reprobe genuinely can corrupt another host's live forward. `force` on `dut_link(action="down")`/a plain re-attach has no such teeth: it cannot touch a different host's attachment either way.
  - **Outstanding validation gap.** Unit tests cover the gate logic (`TestUsbipConflictGate`, `TestForceArgReachesTheHandler`) with `Pod.who`/`usbip_held_by_other` mocked. `usbip_held_by_other()` is host-granular (two processes on the same host share one kernel vhci table, so a same-host collision cannot be synthesized), so proving the real refusal needs a second host genuinely holding the import - not yet done.
- **7. SWD guard + sticky window.** Landed, unit-tested and hardware-validated against a live pod. Every `ops` entry point that touches the shared `_dp`/`_ap`/`_cm` session (`info`, `discover`, `flash_file`, `flash_stream`, `erase_all`, `write_mem_stream`, `dump_stream`, `flash_crc`, `reset`, `halt`, `resume`, `read_reg`, `write_reg`, `read_mem`, `write_mem`, `gdb_serve`, `close`) is wrapped in `ops._guarded`: enter notes `(caller, op)` in `holders["swd"]`, exit drops it unconditionally including the exception path - a plain mutex, refusing a genuinely concurrent second caller outright. `la_capture`/`la_stream` (PIO only) and `stage_flm_blob`/`set_flm_algo`/`flm_algo_info` (RAM staging only) touch no SWD state and are not guarded. `caller=None` (a human at the REPL, or `ops.*` called with no client wrapper) is never gated, per the Non-goals.

  The mutex alone does not close the exposure this item exists for: the REPL is already single-holder, so two calls of the *same* `pod_exec` sequence are serialised, but each call releases the REPL slot on return, and the mutex releases with it - a second caller's sequence is free to interleave in the gap before the first sequence's next call. Closing that needed a second mechanism, and it does not live on the `repl` resource as originally scoped:

  - **The sticky window lives on `swd`, not `repl`, and stays private to the guard rather than becoming a `holders.py` primitive.** The plan text described extending the `repl` holder record's lifetime past disconnect. In practice `netboot._repl_accept` notes `repl` with the connecting *address* (`ip:port`) at TCP-accept time, before any code - and therefore any caller label - has run; reusing `repl` for the sticky check would have that address-keyed placeholder overwrite the caller identity the check depends on on every single connection, defeating it. `swd` has exactly one writer (the guard itself), so no such clobbering is possible, and it is the tighter scope besides: a caller running unrelated non-SWD REPL code during the window is correctly not bounced. The retained-past-`drop()` record itself lives in `ops.py` as a private `_last = (caller, since_ms, op)`, not as a `holders.py`-level primitive: `holders.py`'s one invariant is that nothing it tracks outlives an explicit `drop()`, and generalising a retained-value cache onto the module that also tracks `repl`/`usbip` would hand every future caller of this module (`bench-lease.md` reuses it) a ready-made TTL-adjacent mechanism only this one guard actually wants. `holders.py` gained two small, genuinely generic primitives instead - `held(resource)` (a direct `_HELD` lookup for a hot-path caller, `who()`'s single-resource case without its read-through work) and `now_ms()`/`age_s()` exposed alongside each other so `ops.py` stamps and ages its sticky record on the same clock this module uses rather than importing `time` a second, unrelated way. `ops._guard_enter` refuses a different caller when `_last` names them and `age_s(since_ms) < STICKY_S` (5s - seconds, not minutes, per the design). The same caller's next call always refreshes `_last`, so a sequence never self-refuses.
  - **Caller reaches the pod as a literal in the exec'd code, not over the wire.** `Pod.exec()` shells out to `ampremote`, an external raw-REPL client this repo does not control, so there is no protocol hook to add a caller preamble before raw-paste mode begins. Every `client.py` call site that builds an `ops.*` invocation now bakes `caller=%r` (`self.caller`) into the generated source instead - the same mechanism phase 6 already used for `holders.evict()` calls, extended here rather than inventing wire-level identity.
  - **`SwdBusy` surfaces like the existing REPL-busy case, not as a raw traceback.** `_classify_exec_failure` gained a `SWD_BUSY_MARKERS` check (before the generic busy/traceback branches) that recognises the exception's own message and classifies it as `"pod SWD busy (held by another caller)"`, with `PodExecError.busy = True` - so a caller sees "contention, wait and retry, do NOT reset" rather than an opaque on-pod exception dump.
  - **No MCP or CLI changes.** Both already route every SWD-touching call through the `client.py` methods this phase modified, and `caller` was already resolved once per `Pod` construction back in phase 4; there is no new parameter to expose at either surface.
  - **Hardware-validated 2026-09-03** against the live pod (fp `d83acd`): the mutex + sticky window directly (`ops.discover` from two labels, one immediately after the other, then again after the window elapsed) and the full host path (a real `Pod` client, two different `caller` values, `PodExecError.busy`/reason/message all correct) - both confirmed against a real DUT, not a mock. Found and documented (`dev-notes.md` item 11, not a defect in the shipped code once worked around) a `sys.modules.pop()` re-import-ordering gotcha specific to validating a `from .. import` relationship between two freshly-redeployed modules.

Only after phase 7 does `bench-lease.md` (phase 8, conditional) become worth revisiting, and by then its identity, transport, and enforcement-layer sections are already built.

## Relationship to bench-lease.md

`bench-lease.md` stands, with three sections superseded by this plan once it lands: § Identity (moves here), § Transport and discovery (the 8267 listener is built here; the lease adds verbs to it), and the Layer 1 host courteous gate (built here as the anti-bump gate; the lease extends it to reservation-aware refusal). Its guarded-set table, lease semantics, and steal protocol are unaffected. Update its opening to depend on this doc rather than restate the identity problem.

**Assessed 2026-09-03: the phase-8 gate is not met, deferred.** Phases 4-7 above already close the gap for the case that matters: `pod_open`/`dut_open` (`mcp-surface.md` phase 2) hold the pod's single REPL slot for the session's full lifetime, excluding every other agent until `session_close` - the same across-gaps exclusivity a lease's TTL would provide, with none of the added mechanism (no token, no expiry math). What a lease would still add - reservation ahead of first use, and exclusivity for a one-shot caller that avoids a session - is a coordination nicety, not a correctness gap, since the anti-bump gate above already refuses the corrupting displacement outright. No collision in this project's history is of the shape "two agents both wanted the bench with no way to arbitrate it" (see "The three real bump vectors" above); every one was a driver/transport bug or a vector phase 6 already closes. `bench-lease.md`'s own revisit condition stands unchanged.
