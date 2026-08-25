# Bench lease: exclusive DUT checkout across agents

A time-boxed, named lease over the pod's DUT-facing surface so multiple agents
sharing one pod stop colliding on the DUT. An agent checks the bench out for N
minutes under its own name before it drives the DUT; other agents see who holds
it and until when, and are refused (with an override) until it is released or
expires.

This is a plan, not the code. It supersedes nothing; it is a new capability.

**Phase 8 of the agent-surface track (`overview.md`), and conditional: build it only if phases 1 to 7 leave a gap.** Sequenced after `conflict-legibility.md`, which builds its foundations. That
plan establishes caller identity, a pod-side holder record, the 8267 control
listener, and a host-side anti-bump gate, all on the weaker "holder by use"
model with no TTL or token. Three sections below are superseded once it lands:
§ Identity (defined there), § Transport and discovery (the listener is built
there; the lease adds verbs to it rather than binding a second port), and
Enforcement Layer 1 (the courteous gate exists there; the lease extends it from
"is someone else using this" to "has someone else reserved this"). The guarded
set, lease semantics, and steal protocol below are unaffected. Revisit this plan
once conflict-legibility stage D is done, and only if holding the bench across
gaps in activity turns out to be needed.

## Why

The pod is a single shared DUT rig that several agents already reach at once
(claude-net agents like `usbhost:corona@carbon`, plus the host CLI/MCP, plus a
human at the REPL). Nothing arbitrates the DUT between them. Every collision this
session was two callers unknowingly sharing the DUT: the usbip forwarder
contending the socket REPL, held-session REPL corruption (task #36), and the SWD
singleton (`ops._dp/_ap/_cm`, `debug/ops.py:18-25`) being driven by two callers
with no interlock. The pod already arbitrates *sub-resources* - `pio_arbiter`
tracks `block -> owner` and refuses a second owner (`debug/pio_arbiter.py:49-59`)
- but there is no coarse "who owns the DUT session" layer above it, and no notion
of a *caller identity* anywhere in the stack (the MCP dispatcher, the `Pod`
client, and the pod itself are all caller-anonymous).

The lease turns "mystery contention" into "agent X holds the bench, here is who,
for how long, and how to ask for it."

## Where it sits

Cross-cutting: a small firmware piece (F - the pod-side lease authority + a new
listener in `netboot`) plus the bulk in host tooling (H - the CLI/MCP gate and
client). It is buildable now and does not wait on any bench-gated wiring: the
whole feature is software plus a Wi-Fi round-trip, exercisable against the live
pod with the DUT already attached. It belongs in the Phase 6 (host tooling) /
Phase 7 (multi-agent hardening) band; add a pointer from `overview.md`'s phase
map when it lands.

## What gets locked

ONE coarse lease over the DUT-facing surface, not a lock per peripheral. The
`pio_arbiter` + peripherals `_INST` registry stay underneath as the fine-grained
layer (which PIO block the LA holds, which named peripheral instance is live).
The lease answers "who owns the DUT session"; the arbiter answers "which block
the LA is on." They compose.

Guarded set (refused when another agent holds the bench), from the current MCP
tool surface (`src/host/pod/mcp_server.py`):

- SWD / debug: `flash_dut`, `reset_dut`, `dut_halt`, `dut_resume`,
  `dut_read_reg`, `dut_write_reg`, `dut_read_mem`, `dut_write_mem`, `read_dut`,
  `gdb_dut`. All funnel through the one shared `DebugPort` singleton, so two
  callers corrupt each other's transactions.
- USB/IP: `attach_dut`, `detach_dut`, `ensure_dut_link`, `dut_exec`.
- Peripherals: `i2c_target`, `i2c_target_regs`, `gpio`, `adc`, `logic_analyse`,
  `peripheral_release`.
- Pod REPL sessions: `repl_open`, `repl_send`, `repl_interrupt`, `repl_close`
  (an open session is an exclusive pod resource; `repl_read`/`repl_list` are
  read-only).

Unguarded (observability / management, always allowed): `discover_pods`,
`pod_info`, `register_pod`, `dut_usb`, `mount_dir`, `tail_uart`, `repl_list`,
`repl_read`, and the `lease_*` tools themselves.

Gray - the read-only `dut` identity probe (`mcp_server.py` `handle_dut`). It does
not halt, but it *does* drive the shared SWD transport (reads DPIDR / AP IDR /
CPUID), so it races a lease-holder's in-flight SWD ops. Treat it as guarded when
the bench is held by someone else - i.e. it is lease-aware, refused while another
agent holds SWD, allowed when the bench is free. `pod_exec` stays unguarded but
is the arbitrary-code escape hatch (see Enforcement); it cannot be meaningfully
gated because it can do anything, including drive `ops` directly.

## Authority model

The pod is the single source of truth (it is the one point every agent converges
on; a host-side-only lock cannot coordinate across agent sessions and hosts). A
new pure-MP module `annealage_pod.lease` holds the lease as RAM module-globals,
mirroring `pio_arbiter` exactly - claimed/free plus an owner string, extended
with a token, a TTL, and a purpose:

```
acquire(holder, ttl_s, purpose="") -> {ok, token, expires_in}
                                    |  {ok: False, holder, purpose, expires_in, reason: "busy"}
renew(token, ttl_s)                -> {ok, expires_in} | {ok: False, reason: "expired"|"lost", holder}
release(token)                     -> {ok} | {ok: False, reason: "not_holder"}
steal(holder, purpose="")          -> {ok, token, stole_from}
status()                           -> {free: True} | {free: False, holder, purpose, acquired_s_ago, expires_in, stolen_from}
```

State lives beside the other pod singletons (`debug/ops.py:18-25`,
`peripherals.py:26`): module globals `_holder`, `_token`, `_purpose`,
`_acquired_ms`, `_expires_ms`, `_stolen_from`, created on first import, held for
the VM lifetime.

## Lease semantics

- **Lazy expiry, no timer.** Expiry is checked on every lease op via
  `time.ticks_diff(now, _expires_ms) >= 0`; an expired lease is treated as free.
  No background task, no RTC dependency (the RTC may be unset; `ticks_ms` is
  monotonic and `ticks_diff` handles the ~12-day wrap correctly for
  minutes-to-hours TTLs). The reported currency is always `expires_in` seconds,
  never wall-clock.
- **Atomic check-and-set.** `acquire` does the free/expired/same-holder test and
  the assignment with no `await` between them; on the single-core cooperative
  loop that is atomic, so two agents racing `acquire` cannot both win - the loop
  services one request fully before the next.
- **Re-entrant.** `acquire` by the current holder refreshes and extends (returns
  a token), idempotent like `pio_arbiter.claim` for the same owner.
- **Token proves ownership.** `renew`/`release` require the token, so agent B
  cannot release or extend agent A's lease. An opaque nonce (`os.urandom` hex);
  identity travels only on `acquire`.
- **Steal is deliberate and logged.** `steal` records `_stolen_from = _holder`,
  installs a new holder+token, and sweeps the bench clean
  (`pio_arbiter.release(...)` + `peripherals.release("*")`, guarded imports) so
  the reclaimed bench is in a known state, not mid-capture. The evicted holder
  learns on its next `renew`, which returns `{ok: False, reason: "lost",
  holder}` (its token no longer matches), rather than silently corrupting the
  DUT.
- **RAM-only, cleared on reboot.** Deliberately not persisted to the pod
  filesystem: a reboot means a clean bench, and a zombie lease surviving a reboot
  with no live holder would be the worst failure mode. Reboot = bench free.

## Identity

No caller identity exists anywhere today, so this is the new concept. Resolve a
stable agent name host-side, once per session, in order: the claude-net name
(agents already have `session:user@host`, e.g. `usbhost:corona@carbon`) >
`POD_AGENT_NAME` env > `$USER@$hostname`. Pass it on `acquire`; the token carries
it forward. The holder name is what other agents see and what makes the holder
claude-net-addressable (see Coordination).

## Transport and discovery

Two channels:

- **A dedicated lease line-protocol port** for the authoritative
  acquire/renew/release/status, added as a fourth background asyncio task in
  `netboot.main()` alongside `_wifi_supervisor`, `_repl_accept`, and
  `uart_bridge.serve` (the established pattern: bind synchronously before mDNS
  advertise, `setblocking(False)`, dual-stack `bind(("::", port))`, exception-
  guarded per cycle, `await asyncio.sleep_ms` between polls). It is
  request/response, not streaming: accept, read one JSON line, dispatch to
  `lease.*`, write the JSON reply, close. No persistent client, so it never holds
  a slot. The reason not to run this over the socket REPL: the REPL is the
  contended channel, and "hold the REPL to ask whether I can have the bench" is a
  bootstrap knot; a separate port answers even while usbip has the pod busy.
  Suggested port 8267 (next to REPL 8266); confirm against the port table in
  `overview.md` / `netboot.py`.
- **mDNS `lease-port` TXT key** so discovery knows where to ask, gated on the
  lease listener actually binding (the same conditional-key pattern as
  `uart-port`, `netboot._advertise_mdns`). Discovery does NOT carry the live
  holder in TXT - mDNS only re-advertises on Wi-Fi reconnect, so a holder in TXT
  would be stale; instead `pod discover` / `pod who` query the lease port to
  annotate each pod with its live holder. (A periodic re-advertise to put the
  holder in TXT is a possible enhancement, not v1.)

MVP fallback if the new listener is deferred: drive `lease.*` over `pod_exec`
(unguarded) and read holder via a `status()` exec. Works, but re-introduces the
REPL-contention coupling the dedicated port avoids. The dedicated port is the
recommendation.

## Enforcement

- **Layer 1 (ship first) - host courteous gate.** Each agent runs its own
  `pod-mcp`, so the MCP server *is* the per-agent boundary. It acquires once,
  caches the token, auto-renews on a heartbeat before expiry, and refuses to
  dispatch any guarded tool when it does not hold the lease - with a `force=true`
  / `--steal` override. The pod stays authority (the gate re-checks against
  `status()` / the pod's reply); the host just declines to act without the lease.
  This covers the accidental-collision case, which is what actually bit us.
- **Layer 2 (deferred) - pod-side teeth.** A guard on the `ops.*` entry points
  that checks the module-global holder, with the host calling a
  `lease.set_active(token)` at connection start so the pod knows who is driving.
  Only worth building if an unaware client (or a raw ampremote session) needs to
  be *prevented*, not just discouraged. Defer until Layer 1 shows it is needed.
- **The escape hatch stays open.** The socket REPL and `pod_exec` are deliberate
  god-mode: a human poking `ops.flash()` directly should always win. The lease is
  cooperative coordination between agents, not a security boundary against the
  operator.

Explicit acquire, not auto-acquire-on-first-op: auto-grabbing the bench because
an agent ran a read-only probe would be surprising. The agent calls `checkout`
once; the MCP layer makes the rest transparent (token auto-attach, auto-renew,
release on clean shutdown; TTL is the crash backstop).

## On-pod design

`annealage_pod.lease` (pure MP, FS-deployed like the rest of the package):

- Module globals as above; `_now()` = `time.ticks_ms()`; `_expired()` =
  `_holder is not None and time.ticks_diff(_now(), _expires_ms) >= 0`.
- `acquire`: if `_holder is None or _expired() or _holder == holder`, install
  holder / `_token = ubinascii.hexlify(os.urandom(8))` / `_acquired_ms = now` /
  `_expires_ms = ticks_add(now, ttl_s*1000)` / purpose, clear `_stolen_from` on a
  fresh (non-same-holder) grant, return `{ok, token, expires_in}`. Else return
  the busy dict.
- `renew`: if `_holder is not None and not _expired() and token == _token`,
  extend `_expires_ms`, return `{ok, expires_in}`. Else `{ok: False, reason:
  "expired" if free/expired else "lost", holder: _holder}`.
- `release`: if `token == _token`, clear all globals, return `{ok}`. Else
  `{ok: False, reason: "not_holder"}`.
- `steal`: `_stolen_from = _holder`; install new holder/token/expiry; best-effort
  `pio_arbiter.release(...)` + `peripherals.release("*")` under try/except;
  return `{ok, token, stole_from}`.
- `status`: free dict if `_holder is None or _expired()`, else the holder dict
  with `expires_in`, `acquired_s_ago`, `stolen_from`.

`netboot`:

- Bind the lease listener synchronously in `main()` before the first
  `_advertise_mdns`; pass the bound port (or `None`) into `_advertise_mdns` and
  include `"lease-port"` in the TXT dict only when bound (the `uart-port` gating
  pattern, `netboot.py` ~line 81).
- `asyncio.create_task(lease_service.serve(lease_port))` beside the existing
  tasks. Fully exception-guarded per cycle so the listener never silently
  disappears. It shares no state with `os.dupterm` / the REPL.

## Host side

- `Pod.lease_acquire(holder, ttl_s, purpose)`, `lease_status()`, `lease_renew()`,
  `lease_release()`, `lease_steal()` in `src/host/pod/client.py`: connect a TCP
  socket to the resolved IPv6-first lease endpoint (`self._resolver.endpoint(
  lease_port)`, default from the registry / 8267), send one JSON request line,
  parse the JSON reply. Short socket timeout + a poll loop so Ctrl-C stays
  responsive, matching the client's bounded-wait discipline.
- **Host-side token persistence.** The CLI is one-shot per invocation, so
  `pod checkout` and a later `pod flash` are separate processes; the token must
  persist between them. Store it per pod label in a small state file
  (`~/.config/pod/lease-<label>.json` holding `{token, holder, expires}`), read
  by the guarded verbs and by `lease_renew`/`lease_release`. The MCP server is
  long-lived per session and holds the token in memory, but reads/writes the same
  file so the CLI and MCP agree.
- **Identity resolution** helper: claude-net name > `POD_AGENT_NAME` >
  `$USER@$hostname`, overridable with `--as`.
- **The gate**: in `mcp_server.call_tool` and the CLI dispatch, before a guarded
  verb, check the cached token / query `status()`. If not held by us, refuse with
  the holder + `expires_in` + a "use --steal / message the holder" hint, unless
  `force`/`--steal`. The long-lived MCP server runs a background renew before
  expiry; the one-shot CLI cannot auto-renew, so it validates and warns when the
  lease is near expiry.

## CLI / MCP surface

CLI: `pod checkout --as <name> --for 30m [--why "..."]`, `pod release`,
`pod renew --for 20m`, `pod who` (status), `pod steal --force`. `pod who` and the
occupancy annotation on `pod discover` are read-only so anyone can always see who
holds a pod.

MCP: `lease_acquire`, `lease_status`, `lease_renew`, `lease_release`,
`lease_steal`; every guarded tool gains transparent token threading + the
`force` override. `lease_status` is unguarded read-only.

## Coordination and reporting

The claude-net identity is what makes the refusal useful. A refused agent gets
back `{holder, purpose, expires_in}` where `holder` is a claude-net-addressable
name, so agent B - denied - can (1) read `expires_in` and back off / poll, (2)
message the holder over claude-net ("done with the bench soon? need STM32 flash
for 10 min"), or (3) `steal --force` and escalate to the operator. No wait-queue
in v1: report holder + expiry and let the caller decide; a notify-on-release
queue is a fair follow-up but a lot more pod-side state.

A complementary read-only diagnostic worth noting: the pod can already see its
own accepted TCP connections (which peers are on which ports). That *presence*
signal (who is actually connected now) is distinct from the *lease* (who has
reserved intent) and catches un-leased usage; the lease is the primary
coordination primitive, presence is a cross-check. Presence surfacing is
optional and out of scope for v1.

## Interaction with the existing arbiter and peripherals

The bench lease is the coarse outer layer; `pio_arbiter`
(`debug/pio_arbiter.py`) and the peripherals `_INST` registry
(`peripherals.py:26`) stay as the fine inner layer, unchanged. Within a held
lease the arbiter still governs PIO-block ownership. `steal` and expiry-reclaim
sweep the inner layer clean (`pio_arbiter.release` + `peripherals.release("*")`)
so a reclaimed bench comes back in a known state. The lease does not reserve the
socket REPL exclusively - the REPL keeps its own single-client BUSY refusal
(`netboot._repl_accept`); the lease is a semantic layer above "who drives the
DUT," so a lease-holder still shares pod-management access.

## Open decisions

- **Enforcement strength** - ship Layer 1 (host courteous gate) only, or commit
  to Layer 2 (pod-side teeth) too? Recommendation: Layer 1 now, Layer 2 only if
  un-leased raw-REPL usage causes real corruption in practice.
- **Transport** - dedicated lease port (recommended), or the `pod_exec` + mDNS
  MVP to defer the new listener?
- **TTL defaults + ceiling** - default lease length, and whether there is a hard
  ceiling an agent cannot exceed (stops a checkout-and-walk-away holding the
  bench for hours). Recommendation: default 30 min, renewable, no hard ceiling
  but expiry always wins; revisit a ceiling if walk-aways happen.
- **Steal auto-sweep scope** - does `steal` unconditionally
  `peripherals.release("*")` (clean but destructive to the victim's in-flight
  peripheral state), or only free the SWD/USB exclusives? Recommendation: full
  sweep, since a stolen bench must be trustworthy for the new holder.

## Validation steps

1. Two host processes (or two agent MCP sessions) with distinct `--as` names.
   Process A `pod checkout --as A --for 5m`; confirm `pod who` shows A and an
   `expires_in` counting down.
2. Process B `pod flash ...` (a guarded verb) is refused with A's name +
   `expires_in`, not executed. B `pod checkout --as B` is refused BUSY.
3. A `pod release`; B `pod checkout --as B` now succeeds; the state file for the
   label reflects B.
4. Expiry: A checks out `--for 1m`, does nothing; after expiry `pod who` shows
   free and B can acquire without a steal. Confirm no timer / background task was
   needed (lazy expiry).
5. Steal: A holds; B `pod steal --force`; A's next guarded verb / `pod renew` is
   refused `lost`; the bench is swept (LA/peripherals released); B drives the DUT
   cleanly.
6. Re-entrant: A `checkout` twice extends rather than fails; token stable or
   refreshed, `expires_in` bumped.
7. Reboot: A holds; reboot the pod (`make flash` or reset); after boot `pod who`
   shows free (RAM-only state cleared).
8. Coexistence: with usbip forwarding + the socket REPL both live, the lease port
   answers acquire/status without wedging Wi-Fi RX (the load-bearing single-loop
   check - the lease task must not be a second lwIP mutator).
9. Atomicity: fire N concurrent `pod checkout --as X_i` at a free pod; exactly one
   wins, the rest get BUSY with the winner's name.

## Workflow decomposition

Model-tiered per the coding-workflow default (impl -> sonnet, test -> haiku,
review -> opus):

- **Impl (sonnet):** (a) pod-side `annealage_pod.lease` module + the `netboot`
  lease listener + mDNS `lease-port` gating; (b) host `Pod.lease_*` client
  methods + token state file + identity resolver; (c) CLI verbs
  (`checkout`/`release`/`renew`/`who`/`steal`) + the guarded-verb gate; (d) MCP
  `lease_*` tools + the guarded-tool token threading + `force`.
- **Test (haiku):** host unit tests for the lease client (mock the pod reply),
  the token state-file round-trip, the identity resolver precedence, the gate
  refusal logic, and the atomicity/expiry math; a hardware round-trip harness
  driving validation steps 1-9 against the live pod.
- **Review (opus, standard + adversarial):** the atomicity of the pod-side
  check-and-set (no `await` in the critical section), the steal/lost token
  transition, the lazy-expiry `ticks_diff` wrap correctness, the single-loop
  no-second-mutator property of the lease task, and the guarded/unguarded/gray
  op split (especially the `dut` probe's shared-SWD race).
- **Loop:** feed opus findings to the sonnet impl, re-run the haiku tests,
  re-review, until reviews are clean and the hardware validation passes.

## References

- `debug/pio_arbiter.py:35-80` - the claimed/free + owner pattern this
  generalises.
- `debug/ops.py:18-57` - the shared SWD singletons the lease guards.
- `peripherals.py:26,40-47` - the peripheral `_INST` registry + `release`.
- `src/boards/ANNEALAGE_POD_RP2350/netboot.py` - the asyncio task + mDNS gating
  pattern (see the UART bridge, `phase-5-peripherals-telemetry.md` F5.2).
- `src/host/pod/{client,cli,mcp_server}.py` - the host surfaces to extend.
- `docs/pod/plan/overview.md` - phase map (add a pointer when this lands).
