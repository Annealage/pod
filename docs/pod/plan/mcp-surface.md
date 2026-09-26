# MCP surface: subject-first namespaces, sessions by default

**Status: phases 1 and 2 have landed.** The 27-tool surface and the nested CLI
are implemented, and so is the session rework: sessions are keyed by an id
derived from what they are attached to, a pod session and any number of DUT
sessions coexist, `dut_open` brings the USB/IP link up itself, and `dut_exec`
reuses an open session or an attachment this host already holds instead of
rebuilding one. `src/host/README.md` and `docs/website-features.md` document the
result, and `src/host/tests/test_mcp_surface.py` holds the contract as tests.
Validated against a live pod and a forwarded nRF52840 DUT.

**28 tools as of 2026-09-04**: `dut_flm` was added to the "dut image" group
(`cmsis-flash-completion.md`), reporting or installing the DUT's CMSIS flash
algorithm - the MCP surface previously had no way to trigger a vendor-pack
download or point at an explicit pack, unlike the CLI's `pod flm`, which
already could. Same contract, same test file, count updated throughout.

Phase 3, the doc sweep, was done for phase 1 and needs a second pass over what
phase 2 changed. The pre-cutover names below are retained deliberately: this
document is the mapping from the old surface to the new one, and it stops being
readable if they are edited out.

A reorganisation of the pod's MCP tool surface. Today it is 40 flat tools whose names encode no grouping, whose DUT-versus-pod distinction is inconsistent, and whose most capable path (a persistent connection into the DUT) is reachable only by composing three tools nobody would guess at. The target is a smaller surface split into three clearly-named namespaces by *subject*, with the persistent DUT session as the default way to work and the one-shot as an explicit, named fallback.

This is a plan, not the code. It is independent of `conflict-legibility.md`, which changes what the tools *report*; this changes what they are *called* and how many there are. Both edit the same dispatcher, so they are sequenced rather than interleaved, and **this one goes first**: conflict-legibility's anti-bump gate attaches per-tool, and writing it against names that are about to change is wasted work. Together the two make phases 1 to 7 of one track, indexed in `overview.md`.

The project is pre-announcement and nothing downstream pins these names, so this is a single cutover with no aliases and no deprecation window. That is a deliberate choice made while it is still cheap; it will not be available later.

## What is wrong now

**Names do not group.** `dut_halt`, `dut_resume`, `dut_read_mem`, `dut_write_mem`, `dut_exec`, `dut_usb` use a prefix; `flash_dut`, `erase_dut`, `reset_dut`, `read_dut`, `attach_dut`, `detach_dut`, `reprobe_dut`, `gdb_dut` use a suffix. An agent scanning 40 names cannot see the families. The worst pair is `read_dut` (DUT memory to a host file) versus `dut_read_mem` (DUT memory inline): near-identical names, different tools, and nothing in either name says which is which.

**The best path is unreachable by inspection.** The persistent DUT connection exists and works: `attach_dut` returns a tty, `repl_open(device=<tty>, mount=<host dir>)` holds it open with a host directory as the DUT's filesystem, and `repl_send` / `repl_read` drive it. That is very close to "the agent is running on the DUT". But `repl_open`'s description leads with "to the pod", no tool name contains both `dut` and `repl`, and `pod_exec`'s description points the agent at the DUT path without naming the tool that completes it ("use attach_dut and connect the returned tty" - with what?).

**One-shot is the advertised path and it is the expensive one.** `dut_exec` is the only obviously-DUT-code tool, and each call detaches, sleeps 1.5s, re-attaches, sleeps 1.0s, and retries up to three times (`src/host/pod/client.py:822-843`). A ~2.5s floor per call, tearing down and rebuilding the USB attachment every time, for what an agent will naturally use as its inner loop.

**Sessions are keyed by pod label, so there can only be one.** `_REPL_SESSIONS[label]` (`src/host/pod/mcp_server.py:366`) means a pod session and a DUT session cannot be held at once, though driving pod peripherals while watching DUT output is a normal thing to want.

**Read and write are separate tools where the operation is one.** `dut_read_reg`/`dut_write_reg`, `dut_read_mem`/`dut_write_mem`, `i2c_target`/`i2c_target_regs`. Meanwhile `gpio` already does read-when-value-omitted in a single tool, so the surface disagrees with itself about its own convention.

**Five tools are USB/IP plumbing** (`attach_dut`, `detach_dut`, `ensure_dut_link`, `dut_usb`, `reprobe_dut`) that an agent should rarely touch directly, occupying five slots in the listing at the same visual weight as `dut_flash`.

**The CLI and MCP have diverged into two vocabularies for one behaviour.** `pod read-reg` / `dut_read_reg`, `pod dut-exec` / `dut_exec`, `pod la` / `logic_analyse`, `pod usb` / `dut_usb`, `pod release` / `peripheral_release`, `pod recover-dut` / `recover_dut_repl`. 39 CLI subcommands against 40 MCP tools with no shared naming rule. Renaming one without the other doubles the divergence.

## Principles

1. **Subject first, in a prefix.** Every tool starts with the thing it acts on: `pod_`, `dut_`, or `bench_`. The prefix is the grouping mechanism, since MCP has no namespaces.
2. **The persistent session is the norm.** `dut_open` is the entry point for working with a DUT. The one-shot exists, is named as a one-shot, and its description says to prefer the session for more than a single call.
3. **Direction and destination are parameters, not tools.** Read-versus-write is an argument (`value` omitted = read, as `gpio` already does). Inline-versus-file is an argument (`out_path`). Neither justifies a second tool.
4. **The subject is chosen once, at open.** After that an agent holds a session id, and the verbs that drive it are the same whichever subject it opened. Multiple concurrent sessions per pod.
5. **Plumbing collapses to one tool with an action.** Operations an agent should rarely reach for do not each get a top-level slot.
6. **Pod tools are only what is needed to find, enroll, and configure a pod.** Anything the pod does *to* the DUT is `bench_`, not `pod_`.

## The three namespaces

- **`pod_`** - the pod as a managed device. Find it, enroll it, inspect it, run code on it, give it a host directory. Deliberately small.
- **`dut_`** - the device under test, by every route: its REPL (session and one-shot), its debug port (SWD), its flash, and the USB/IP link that carries it.
- **`bench_`** - the pod's instruments pointed at the DUT: GPIO, ADC, logic analyser, the I2C/SPI device personalities, the UART tap. Pod hardware, DUT purpose, which is why these are neither `pod_` nor `dut_`.

## Proposed surface

27 tools, from 40.

### pod (6)

| new | replaces | change |
| --- | --- | --- |
| `pod_discover` | `discover_pods` | rename only |
| `pod_register` | `register_pod` | rename only |
| `pod_info` | `pod_info`, `repl_list` | returns registry handles plus the live holder record and this process's open sessions. `repl_list` folds in here (see `conflict-legibility.md` § 4: it reads as a global view but is per-process under stdio) |
| `pod_exec` | `pod_exec` | unchanged. The arbitrary-code escape hatch |
| `pod_mount` | `mount_dir` | rename only |
| `pod_open` | `repl_open` (its pod-default case) | opens a persistent session on the pod's own socket REPL and returns a session id, driven by the same `session_*` verbs as a DUT session. Its description must state that it holds the pod's single REPL slot for its lifetime and excludes every other agent, which `repl_open` today does not make prominent |

`pod_open` earns its slot on two things `pod_exec` cannot do: a continuous tail of the pod's background asyncio output, and a mount held open rather than unmounted on return. The exclusion it causes is real, which is an argument for saying so in the tool description, not for hiding the capability.

### dut session (5)

| new | replaces | change |
| --- | --- | --- |
| `dut_open` | `attach_dut`, `ensure_dut_link`, `repl_open`, `recover_dut_repl` | the entry point. Brings the USB/IP link up, attaches, opens a persistent session on the DUT's CDC tty, optionally runs the Ctrl-C/Ctrl-B un-stick first (`recover=true`), optionally mounts a host directory for the session lifetime, optionally `cp`s files and runs setup `exec` before connecting. Returns a session id |
| `session_send` | `repl_send`, `repl_interrupt` | writes to the session's stdin and returns what came back within `wait`. `control=` sends Ctrl-C / Ctrl-B / Ctrl-D, which absorbs `repl_interrupt` and also exposes the Ctrl-B and Ctrl-D that `recover_dut_repl` currently hardcodes |
| `session_read` | `repl_read` | cursor tail, unchanged behaviour |
| `session_close` | `repl_close` | closes and optionally detaches the link |
| `dut_exec` | `dut_exec` | kept as the named one-shot: open, run, close. Reuses an open session when one exists rather than tearing down and rebuilding the attachment, which removes the ~2.5s floor from the common case |

The session verbs are shared between subjects and keyed by session id, not pod label. The separation the surface needs is at `dut_open` versus a pod open, which is where the agent actually makes the choice; duplicating the three verbs into `dut_send`/`pod_send` etc. buys nothing after that point and costs three slots. The tradeoff is that `session_send` alone does not say what it is talking to, which the session id and `pod_info` both answer.

Mount is how files reach the DUT, and it is a `dut_open` argument rather than a `dut_cp` tool. This is the persistent-first answer to what is currently a gap: `mount_dir` exists for the pod and there is no DUT equivalent at all.

### dut debug, over SWD (6)

| new | replaces | change |
| --- | --- | --- |
| `dut_identify` | `dut` | rename. The bare `dut` name is unguessable and reads like a namespace rather than a verb |
| `dut_halt` | `dut_halt` | unchanged |
| `dut_resume` | `dut_resume` | unchanged |
| `dut_reg` | `dut_read_reg`, `dut_write_reg` | `value` omitted reads, `value` given writes |
| `dut_mem` | `dut_read_mem`, `dut_write_mem`, `read_dut` | `data` given writes; otherwise reads, inline by default or streamed to `out_path` when given. Kills the `read_dut`/`dut_read_mem` collision |
| `dut_gdb` | `gdb_dut` | rename only |

### dut image (4)

| new | replaces | change |
| --- | --- | --- |
| `dut_flash` | `flash_dut` | rename only |
| `dut_erase` | `erase_dut` | rename only. Kept separate from `dut_flash` because it is independently destructive and deserves its own name in a permission prompt |
| `dut_flm` | (new, 2026-09-04) | report or install the DUT's CMSIS flash algorithm, mirroring the CLI's `pod flm`: no options beyond `label` reports what is installed, `device`/`pack`/`download`/`force` resolve and install one. `download` (+`vendor`/`pack_name`) is the only way this surface can fetch a pack from the vendor index - `dut_flash`/`dut_erase` only ever resolve from the local pack cache |
| `dut_reset` | `reset_dut` | rename only |

### dut link (1)

| new | replaces | change |
| --- | --- | --- |
| `dut_link` | `attach_dut`, `detach_dut`, `ensure_dut_link`, `dut_usb`, `reprobe_dut` | `action="status"` (what the pod exports, who holds it), `"up"`, `"down"`, `"reprobe"`. Five plumbing tools to one, and `dut_open` / `dut_exec` handle the link so this is a repair and inspection tool rather than a step in the normal path |

### bench instruments (6)

| new | replaces | change |
| --- | --- | --- |
| `bench_gpio` | `gpio` | rename only |
| `bench_adc` | `adc` | rename only |
| `bench_la` | `logic_analyse` | rename. Matches the CLI's existing `pod la` |
| `bench_device` | `i2c_target`, `spi_target`, `spi_target_status`, `peripheral_release` | `bus="i2c"` \| `"spi"` selects the personality; `action="up"` \| `"status"` \| `"down"` covers bring-up, the SPI byte/transfer counters, and release, with `name="*"` on `"down"` giving the sweep `peripheral_release` provided. The two are the same concept (the pod presenting itself as a register-file device on the DUT's bus) and were only separate tools because they are separate implementations |
| `bench_device_regs` | `i2c_target_regs`, `spi_target_regs` | one regfile accessor across both buses; `off`/`length` read, `write` writes, `table` selects the SPI backing table |
| `bench_uart` | `tail_uart` | rename. Named for the tap, not the verb, leaving room for the TX direction that is currently CLI-only |

"Target" is avoided throughout because it already means two other things in this repo (the flash target family, the DUT itself). "Device" says what the pod is doing: presenting itself as a device on the DUT's bus.

## What this does not do

- No behaviour changes in phase 1. Every merged tool keeps its handler; the merge happens in the dispatcher and the schema. The handler functions in `mcp_server.py` are already separated from the MCP layer for exactly this kind of change. Phase 2 is where behaviour moves.
- No new capability. The one thing that comes close is `dut_exec` reusing an open session, which is a performance fix inside an existing tool.
- No change to `conflict-legibility.md`'s holder record, which is orthogonal and surfaces through `pod_info` and `dut_link(action="status")` once both have landed.

## CLI alignment

The CLI is 39 subcommands under a different naming rule, and the divergence is per-operation: `pod read-reg` against `dut_read_reg`, `pod dut-exec` against `dut_exec`, `pod la` against `logic_analyse`, `pod usb` against `dut_usb`, `pod release` against `peripheral_release`, `pod recover-dut` against `recover_dut_repl`.

Reorganising MCP alone makes this worse, so the CLI moves to the same taxonomy as nested subcommands, which is what its size has needed for a while: `pod dut open|exec|flash|erase|reset|reg|mem|halt|resume|gdb|identify|link`, `pod bench gpio|adc|la|device|uart`, and the pod verbs staying flat (`pod discover|register|info|exec|mount|cp|pins`). One vocabulary, MCP tool `dut_flash` and CLI `pod dut flash`, mechanically derivable in both directions.

The CLI keeps the extras MCP does not carry (`pins`, `cp`, `flm`, `unregister`, `install-udev`); those are operator tools, not agent tools, and that asymmetry is deliberate rather than an oversight to fix.

## Cutover, not migration

No aliases, no deprecation window, no compatibility shims. The project is pre-announcement, nothing external pins these names, and the cost of carrying a 40-name shadow surface under a 27-name one is permanent while the cost of a clean break is one afternoon of doc edits. Old names are deleted, not redirected.

What this buys beyond tidiness: the merges and the session rework can be designed for what is right rather than for what is expressible alongside the old arity. `dut_mem` does not have to keep a shape that `read_dut` could also satisfy, and sessions can move from label keys to session ids without a dual-keyed lookup.

The whole-repo consequence is that every consumer moves in the same change. The known set is `src/host/pod/cli.py`, `src/host/pod/mcp_server.py`, `src/host/tests/`, `src/host/README.md`, `docs/pod/*.md`, and `docs/website-features.md` (whose tool count of 34 is already stale against 40, which is itself evidence that the docs drift when the surface moves without a sweep).

## Phasing

**Phases 1 to 3 of the agent-surface track**, whose ordered index lives in `overview.md`. Sequenced by hardware risk rather than by blast radius, since blast radius is no longer a constraint. Phases 4 to 7 (contention legibility and the anti-bump gate) are in `conflict-legibility.md` and phase 8 (the conditional bench lease) in `bench-lease.md`.

- **Phase 1. The surface, in one pass.** Rename, re-prefix, merge the read/write and destination pairs, collapse the USB/IP plumbing into `dut_link`, and nest the CLI to match. Mechanical: every handler function in `mcp_server.py` keeps its body, and the change is in the schemas, the dispatcher, and the argparse tree. Keeping the CLI in the same pass is the point rather than an extra, since the two surfaces drifting apart is one of the problems being fixed. Gate: the host test suite passes and `pod --help` reads as the same taxonomy the tool list does.
- **Phase 2. Session rework.** Session ids instead of label keys, concurrent pod and DUT sessions, `dut_open` absorbing attach / link bring-up / recover / mount, `dut_exec` reusing an open session instead of rebuilding the attachment. The only phase with real behaviour change and the only one needing hardware. Gate: a DUT session and a pod session held simultaneously against the live pod; `dut_exec` measured against an open session to confirm the ~2.5s floor is gone; a `recover=true` open un-sticks a DUT deliberately latched in raw mode.
- **Phase 3. Doc sweep.** Every doc that names a tool, plus `docs/website-features.md` reconciled to the real count.

Phases 4 onward then attach to the final names. Each carries its own doc updates; phase 3 sweeps what phases 1 and 2 change, it is not a sweep for the whole track.

## Decisions

- **The third prefix is `bench_`, not `pod_`.** These are pod hardware but their purpose is the DUT, and folding them into `pod_` would take that group from six to twelve and destroy the "minimal pod surface" property that motivates the split. The cost is that an agent may reach for `pod_gpio` first; with no alias to soften it, the mitigation is that `pod_info` names the bench tools and the `bench_` group is contiguous in the listing.
- **`dut_halt` and `dut_resume` stay separate** rather than merging into an action enum. They are opposite verbs, not variants of one operation, and an enum would make the halt-then-inspect-then-resume loop read worse.
- **The `session_*` verbs are shared between subjects** rather than duplicated as `dut_send` / `pod_send`. The subject is chosen at `dut_open` or `pod_open`, which is where the agent actually decides; after that it holds a handle. The cost is that `session_send` alone does not name its subject, which the session id and `pod_info` both answer.
