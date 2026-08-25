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


## The three real bump vectors

All host-side, all in `src/host/pod/client.py`, all working by detaching the incumbent so the pod's own refusal never fires:

1. **`dut_exec` detaches unconditionally.** `self.usbip_detach()` at `client.py:828`, commented as clearing a stale attachment, detaches *every* host vhci port bound to that DUT, including another agent's live one. Agent B running one `dut_exec` rips away agent A's session, and the pod cooperates because from its side the import was closed normally.
2. **`flash_dut` detaches a live session by default.** `keep_attached=False` (`mcp_server.py:178`, `handle_flash_dut`) is correct for a single user and silently destructive for two.
3. **`detach_dut` and `peripheral_release("*")` are unscoped.** `detach_dut` detaches every port for the DUT; `peripherals.release("*")` (`src/mpy/annealage_pod/peripherals.py:40-47`) deinits every named instance including another agent's live I2C or SPI target.

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
- `flash_dut`, `erase_dut`, `reset_dut`, `reprobe_dut` refuse when another caller holds `usbip` or `swd`, since all of them displace or corrupt a live session.
- `detach_dut` and `peripheral_release` scope to the calling caller's own resources by default; `"*"` means "all of mine".

Each gets `force=true`, which bumps, logs the eviction on the pod with both caller names, and returns `stole_from` in the result so the acting agent sees in its own transcript that it displaced someone. The evicted agent finds out when its next operation fails against a holder record that no longer names it.

Each agent runs its own `pod-mcp` under stdio transport, so the MCP server process is a clean per-agent boundary and the gate has exactly one place to live.

### 4. Surface honesty

- **`_classify_exec_failure` gains a busy branch.** Today the pod's BUSY line falls through to `raw-REPL entry / connection failed` (`client.py:89-92`), which reads as a transport fault. It must classify as busy and carry the holder. This is the single highest-value item in the plan: a collision currently presents as evidence of a broken pod, and the documented response to a broken pod is reset and power-cycle, which is precisely the destructive recovery `CLAUDE.local.md` forbids.
- **The pod's BUSY line names the holder.** `netboot._repl_accept` has the incumbent's address in hand and should include it plus the caller label.
- **`pod_info` returns the `who` record** for every resource, alongside the registry handles it already carries.
- **Per-process session state stops reading as global truth.** Under stdio transport `_REPL_SESSIONS` (`mcp_server.py:366`) is per-process, so `repl_list` reads as a global view of open sessions while showing empty during another agent's session. Phase 1 folds it into `pod_info`; this phase makes that view carry the pod's holder record beside this process's own sessions.
- **Structured results.** `call_tool` returns `str(result)` (`mcp_server.py:1617`), a Python dict repr, and returns errors through the same success path with no `isError`, so a failure is indistinguishable from a success whose text begins with `Error:`. Emit JSON and set `isError`. Without this a gate refusal is just more prose for the agent to guess at.

### 5. SWD re-entrancy guard

The only item here that refuses rather than reports, because there is no failure to report: two callers interleaving on the `ops` singleton get silent corruption.

A flag at the `ops` entry points: enter records `(caller, op, since)` in `holders`, exit clears it, a second caller entering while held is refused naming the holder. This is a mutex, not a lease: no expiry, no ownership across calls, no override protocol, released on exit including the exception path.

Note that SWD ops arrive over the socket REPL, which is already single-holder, so a caller holding a `repl_open` session is implicitly serialised. The exposure is one-shot `pod_exec` sequences, which release the REPL slot between calls and let a second caller interleave mid-sequence. A short sticky window on the `repl` holder record (the record survives disconnect for N seconds, refreshed by the same caller reconnecting, bounced for a different one) closes that without any checkout protocol. This is the one place the design admits a timer, and it should be seconds, not minutes.

## Non-goals

- No reservation ahead of use, no TTL, no renewal, no steal protocol. That is `bench-lease.md`.
- Not a security boundary. `pod_exec` and a human at the REPL are deliberate god-mode and always win; a human poking `ops.flash()` should not be gated.
- No persistence. The record is RAM-only and a reboot means a clean bench, for the same reason the lease is RAM-only: a zombie holder surviving a reboot with no live holder is a worse failure than no record at all.

## Landmines found while scoping this

- **`supervisor._default_cleanup` fires on every REPL disconnect** and powers off both DUT rails and opens every relay (`src/mpy/annealage_pod/supervisor.py:57-76`, registered at import on line 76). It is inert today only because `power` and `relays` raise `_pinmap.assert_esp32_carrier` on the RP2350 (`src/mpy/annealage_pod/_pinmap.py:115-121`) and `run_cleanup` swallows and prints the exception. When `carrier-hardware.md` lands and those rails become real, every `pod_exec` agent A makes will power-cycle agent B's DUT on disconnect. This is a cross-agent destructive side effect caused by a *departure* rather than an arrival, so no holder record explains it after the fact. Fix before the carrier port: scope the default cleanup to the last disconnect, or make it opt-in per session.
- **PIO arbiter owner strings collide across callers** (see bump vector 4). Composing the owner as `subsystem@caller` once identity exists restores the refusal for two agents using the same subsystem, and costs nothing else.

## Phasing

Phases 4 to 7 of the agent-surface track (`overview.md`). Each is independently useful and hardware-validatable.

- **4. Identity + failure classification.** Caller label on the `Pod` client, busy branch in `_classify_exec_failure`, holder name in netboot's BUSY line, JSON + `isError` from `call_tool`. No new listener. Turns a collision from "pod looks dead" into "pod busy, held by X". Gate: two agents collide on the REPL and both transcripts name the other.
- **5. Holder record + control port.** `annealage_pod.holders`, the 8267 listener, `control-port` TXT, `pod_info` carrying the holder record, `repl_list` already gone in phase 1. Gate: `who` answers correctly while the REPL is held and while usbip has an import open.
- **6. Anti-bump gate.** The three vectors made conditional, `force` with eviction logging. Gate: agent B's `dut_exec` and `flash_dut` are refused against agent A's live attachment, and succeed with `force`.
- **7. SWD guard + sticky window.** Gate: two interleaved `pod_exec` SWD sequences produce a refusal rather than corrupt reads.

Only after phase 7 does `bench-lease.md` (phase 8, conditional) become worth revisiting, and by then its identity, transport, and enforcement-layer sections are already built.

## Relationship to bench-lease.md

`bench-lease.md` stands, with three sections superseded by this plan once it lands: § Identity (moves here), § Transport and discovery (the 8267 listener is built here; the lease adds verbs to it), and the Layer 1 host courteous gate (built here as the anti-bump gate; the lease extends it to reservation-aware refusal). Its guarded-set table, lease semantics, and steal protocol are unaffected. Update its opening to depend on this doc rather than restate the identity problem.
