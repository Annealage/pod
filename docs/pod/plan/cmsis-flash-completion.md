# Finishing the CMSIS flash path, and deleting the native nRF driver

The pod has two flash backends. `native` (`src/mpy/annealage_pod/debug/flash_nrf52.py`) is a hand-written nRF52 driver: the pod halts the DUT and writes its NVMC peripheral registers over SWD, so no code runs on the DUT at all. `flm` (`src/mpy/annealage_pod/debug/flm.py`) is the generic CMSIS path: the pod copies the vendor's flash algorithm into the DUT's SRAM and calls `Init` / `EraseSector` / `EraseChip` / `ProgramPage` on the DUT's own core, per ARM's CMSIS-Pack `FlashOS.H` contract. One implementation covers any chip with a Device Family Pack.

`native` exists only because it was the quickest way to prove flash control on the one DUT that was wired up. It is a workaround that has been load-bearing for too long, and it is the default, which means the generic path never gets exercised. **The goal of this plan is to delete `native` entirely.** It is kept only as a revert tool while the CMSIS path is finished and validated, and it is deleted at the end.

There is no partial-credit outcome here: the deliverable is `flash_nrf52.py` gone, the `loader` parameter gone, and the CMSIS path being the only way the pod flashes anything.

## Why deleting it matters beyond tidiness

`native` is not gated on the DUT's family. `ops._ensure` builds it unconditionally (`ops.py:122`), with no reference to the registry's declared `target_family`, and it is the default loader. So pointing `dut_flash` at an STM32 today does not fail cleanly: it drives nRF52 NVMC register addresses (`NVMC_BASE = 0x4001E000`) on silicon where that address is an unrelated peripheral. That is a wrong-target write presented as a normal flash.

`flash_dut(target=...)` compounds it. The parameter is accepted by the client (`client.py:596`), plumbed from `pod dut flash --target T` and the MCP schema, and read nowhere in the body. A caller can name a target, get no error, and have the nRF path run anyway.

## Where the work stands

Verified while writing this, against the real pack in the local cache
(`~/.cache/annealage-pod/cmsis-packs/NordicSemiconductor.nRF_DeviceFamilyPack.8.44.1.pack`):

| Layer | State |
| --- | --- |
| Host pack layer, `src/host/pod/cmsis_pack.py` | Done. pdsc parse, device lookup, algorithm-by-address selection, RAM region pick, local cache, vendor index with opt-in download. 33 tests in `test_cmsis_pack.py` |
| Host FLM parser, `src/host/pod/flm.py` | Done. ELF32 parse of PrgCode / PrgData / DevDscr, entry symbols, RAM layout, sector map. 18 tests in `test_flm.py`, all against synthetic FLM images assembled in-test |
| On-pod runner, `src/mpy/annealage_pod/debug/flm.py` | Done, and hardware-validated on an nRF52840 in `d5f698d` ("selectable FLM flash backend (hardware-validated)") using a baked-in blob rather than a pack |
| Glue | Done. `Pod.resolve_flm_algo` / `install_flm_algo` / `flm_algo_info` / `ensure_flm_algo` with caching, `ops._select_loader`, FLM branches in `flash_file` / `flash_stream` / `erase_all`, `pod flm`, `loader=` on `dut_flash` and `dut_erase` |

The unproven surface was therefore narrow: **host pack to algo dict**. It now has silicon time too (erase-sector + program-page + read-back, both driven directly and through the real `dut flash` path) - see "Status (2026-09-03)" below for what that covered and what did not.

`cmsis_pack.algo_for_device("nRF52840_xxAA")` currently returns:

```
load_address   0x20000000     static_base   0x200005a0
begin_stack    0x200009b0     begin_data    0x200009b0
flash_base     0x0            flash_size    0x200000
page_size      0x1000         sectors       [(0, 4096)]
instructions   1456 bytes
pc_init 0x20000008  pc_unInit 0x20000048  pc_erase_sector 0x200000bc
pc_program_page 0x2000013c   pc_eraseAll 0x200000a0
```

Circumstantial but reassuring: those first two values are exactly where the live DUT was found parked (see below), so the pack path appears to reproduce the layout the validated blob used. It is not proof, because it is not known whether the blob or an early run of the pack path parked it.

## Context a fresh session needs

**The baked blob is not recoverable.** `4f2b538`'s message says it removes `flm_nrf52840.py` and `tools/flm_extract.py`, but neither path appears in that commit's diff and neither was ever tracked in this repository (`git log --all --name-only | grep -i flm` lists only the five current paths). They existed in a working tree and were never committed. Do not spend time looking for them, and do not plan to diff the pack-resolved algo against them.

**The validation harness was deleted by a stray edit and is recoverable.** `prototypes/validation/flm_validate.py`, 118 lines, a Tier-1 hardware validation of the FLM runner against the live nRF DUT, was removed in `f0c32d6` whose message is entirely about the #66 reprobe docs and never mentions it. Recover with:

```
git show d5f698d:prototypes/validation/flm_validate.py > prototypes/validation/flm_validate.py
```

It is the right shape already: pick a high scratch page (`0xF8000`), save its 4 KB via the MEM-AP, erase, program a 256-byte pattern through the runner, read back and diff on the pod, then restore the original. Non-destructive and re-runnable. Its one change needed is to stop importing `annealage_pod.debug.flm_nrf52840` and take a pack-resolved algo installed via `ops.set_flm_algo` instead.

**Flashing does not need the DUT's USB.** Flash, erase and verify are pure SWD and work today. Only the forwarded-REPL checks need USB/IP, and at the time of writing the DUT enumerates nothing on the pod's USB host port (running core, valid flash, `machine.USBHost().devices()` empty), which is a cabling or VBUS question. Do not let that block this plan.

**The DUT may be found parked in a flash algorithm.** See the resume bug below. If SWD works but the DUT seems dead, read `DHCSR` and `DFSR` before doing anything else; `pod dut reset <label> --mode sysreset` recovers it.

## Gap 1: every FLM operation leaves the DUT halted

This blocks everything else and must be fixed first.

The CMSIS calling convention sets `LR = load_address | 1` and relies on the blob's first halfword being a `BKPT`, so returning from an algorithm function halts the core (`flm.py:133`, and the contract in the module header). Every FLM call therefore ends with the core parked **on** that breakpoint.

All three flash paths end by calling `cm.resume()` (`ops.py:250` in `flash_file`, `ops.py:318` in `flash_stream`, `ops.py:347` in `erase_all`), and `erase_all`'s comment states "The core is always resumed in the finally". That resume cannot work: the PC is sitting on the `BKPT` halfword, so resuming re-executes it and re-halts immediately. The `except Exception: pass` around it hides the failure. The cleanup is not missing, it is silently ineffective.

Observed on the live nRF52840 DUT:

```
DHCSR   0x0003000b   C_DEBUGEN | C_HALT | C_MASKINTS | S_REGRDY | S_HALT
DFSR    0x00000003   HALTED(0) | BKPT(1)
DEMCR   0x00000000   no vector catch
FP_CTRL 0x00000260   ENABLE=0, FPB disabled
PC      0x20000000   == algo load_address
SP      0x200009b0   == algo begin_stack
LR      0x200004f5   inside PrgCode, Thumb bit set
```

Proof it is the breakpoint and not something external: clear DFSR, resume, wait 300 ms, and it re-halts with `DFSR = 0x00000002`, BKPT alone. `C_MASKINTS` is written only by `flm.py:142-143` (and `swd_dap.step()`), which is what ties the state to the FLM runner rather than to a plain debug halt.

This is FLM-only. The native path halts the core plainly via `fl.prepare()`, so its `resume()` behaves normally. That asymmetry is exactly why the bug survived: the default path does not have it.

Fix: after the last algorithm call in an operation, restore the DUT rather than calling a resume that cannot work. `sysreset` is the honest choice, since a freshly flashed DUT should restart its application anyway; moving PC off the breakpoint before resuming is the alternative if a caller needs the DUT left running without a reset. Whichever is chosen, `erase_all`'s comment and the `flash_dut` / `dut_erase` descriptions must state what the DUT is left doing, and the swallowed exception should stop being swallowed.

Gate: after a `loader="flm"` flash and after a `loader="flm"` erase, the DUT is running (`cm.is_halted()` false, `DFSR` clear) with no manual reset.

## Gap 2: the pack-resolved path has never run on silicon

The runner has silicon time, but only with the baked blob. The host pack-to-algo layer landed in `aca83d8` as uncommitted work and has never driven a real flash.

Restore `flm_validate.py` per above, repoint it at a pack-resolved algo, and run it. Then run the real operations: flat-binary flash, ELF flash (per-`PT_LOAD` routing), mass-erase, each with the read-back CRC verify that `flash_dut` already does.

Gate: all four pass on the nRF52840 with `loader="flm"` and a pack-resolved algorithm, and the DUT is left running.

## Gap 3: FlashDevice timeouts are parsed and then dropped

`_decode_device` reads `to_prog` and `to_erase` from the FlashDevice struct and returns them as `timeout_prog_ms` / `timeout_erase_ms` (`src/host/pod/flm.py:179`, `:206`). `FlmImage.build_algo` (`flm.py:112`) does not forward them into the algo dict: the dict `algo_for_device` returns has no timeout keys at all. The on-pod runner hardcodes `timeout_ms=8000` in `_call` (`src/mpy/annealage_pod/debug/flm.py:124`).

Harmless on the nRF52840, whose chip erase is well under a second. Wrong for any part whose erase or program time exceeds 8 s, which will present as a spurious `FLMError` timeout on a flash that was actually progressing.

Fix: carry the two values through `build_algo` into the algo dict, and have `_call` use the erase timeout for `pc_erase_sector` / `pc_eraseAll` and the program timeout for `pc_program_page`, keeping the current 8 s as the fallback when a pack omits them.

## Gap 4: whose flash geometry wins

The pack's FlashDevice for `nRF52840_xxAA` declares `flash_size 0x200000` (2 MB) and the pdsc algorithm entry declares `start 0, size 2097152`. The part has 1 MB, and the registry `dut` block declares `flash_base 0x0`, `flash_size 0x100000`.

The nRF52840 escapes the consequence because its algorithm has `pc_eraseAll`, so `erase_all` calls EraseChip instead of sweeping sectors. A part without EraseChip would sector-sweep across the pack's declared size and erase past the end of real flash.

Decide and document a rule. The defensible one: the registry's declared geometry bounds the operation when present, since it describes the actual part on the bench, and the pack's geometry is used only to lay out the algorithm and to supply the sector map. Whatever is chosen, `erase_all`'s sector sweep must clamp to it.

## Gap 5: genericity is unproven

The generic path has only ever run on nRF52. Until it flashes something that is not a Nordic part, "works for any chip with a CMSIS pack" is a design claim rather than a result, and `native` cannot be deleted with confidence.

`docs/website-features.md` already lists STM32 and RP2350-as-DUT as pending. RP2350-as-DUT additionally needs SWD multidrop (dormant plus TARGETSEL) bring-up, so an STM32 is the cheaper second target.

Gate: one non-Nordic Cortex-M flashed and verified end-to-end through the pack path.

## Gap 6: no real vendor FLM in the test suite

`test_flm.py`'s 18 tests assemble synthetic ELF32 images, which is good coverage of the parser's contract but proves nothing about real vendor artifacts. The one test that reads a real file is gated on an env var and skipped (`test_flm.py:276-278`).

Fix: commit a small real `.FLM` as a fixture and un-gate that test, or wire `ANNEALAGE_TEST_FLM` into the validation run so it is exercised somewhere that is not a developer's shell. Check the pack's licence before committing a vendor artifact; if that is not acceptable, the alternative is a test that resolves from the local cache and skips only when the cache is empty.

## Gap 7: the dead `--target` parameter

Delete it from `client.flash_dut`, the CLI, and the MCP schema, or wire it into the pack lookup, which is where a target name belongs: `resolve_flm_algo(device=...)` already accepts one and `ensure_flm_algo` defaults it from the registry's `target_family`. Wiring it is the better outcome, since naming a target per-flash is genuinely useful when a device has several algorithms. Leaving it inert is not an option.

## Sequence

Native survives to the end of step 4 and no further. Its whole remaining purpose is to be the thing that reflashes the DUT when a step in the CMSIS path leaves it unusable.

1. **Fix the resume bug** (gap 1), forward the timeouts (gap 3), settle the geometry rule (gap 4). Host-side and pod-side; needs a DUT for the gate but not for the work.
2. **Restore the validation harness** (gap 2) and the real-artifact test coverage (gap 6). Repoint the harness at a pack-resolved algo.
3. **Hardware-validate FLM on the nRF52840**: flat flash, ELF flash, mass-erase, CRC verify, DUT left running. This is where `native` earns its keep: if a step bricks the DUT, `loader="native"` reflashes it and the loop continues.
4. **Flip the default** to `loader="flm"`. `native` stays reachable as an explicit `loader="native"` but is gated to refuse when the declared `target_family` is not an nRF52, and its docstrings say plainly that it is a fallback awaiting removal. Fix `--target` here (gap 7). Gate: the full suite passes with `flm` as the default and no test opts back to `native` to stay green.
5. **Prove a second family** (gap 5) on an STM32.
6. **Delete it.** Remove `flash_nrf52.py`, the native arm of `_select_loader`, the native branches in `flash_file` / `flash_stream` / `erase_all`, the `_flash` global from `_ensure`, and the `loader` parameter from `Pod.flash_dut` / `Pod.erase_dut` / `handle_dut_flash` / `handle_dut_erase` / the CLI. Sweep `docs/pod/debug-stack.md`, `docs/website-features.md`, `src/host/README.md` and the DUT-compatibility table. Gate: no reference to a native or per-family flash driver survives outside git history, and the hardware validations from steps 3 and 5 still pass.

Steps 1 to 4 are worth doing as one branch; step 5 depends on hardware that is not currently wired, and step 6 is a small mechanical change once step 5 passes.

## Status (2026-09-03)

Steps 1 and 2 are done; step 3 is partially done - the parts a scratch-region test can prove without risking the DUT's actual firmware, not yet the full gate.

- **Step 1, done.** Gap 1: `ops._flm_restore` ends an FLM operation with `cm.sysreset()` (not a plain resume, which cannot move PC off the algorithm's own BKPT trampoline) for `loader="flm"`; `erase_all`'s restore failure now surfaces into `err` instead of a silent `except: pass`. Gap 3: `build_algo` forwards the pack's `timeout_prog_ms`/`timeout_erase_ms`; the on-pod runner uses them for `EraseSector`/`EraseChip`/`ProgramPage`, falling back to 8 s when a pack omits them. Gap 4: `Pod.resolve_flm_algo` overrides the pack's `flash_base`/`flash_size` with the registry's declared geometry when present, which is what `erase_all`'s sector sweep (the no-`EraseChip` path) bounds against. All three unit-tested (`test_ops_swd.py::TestFlmRestore`, `test_flm_pod.py`'s per-operation-timeout tests, `test_flm.py`/`test_client.py`'s geometry tests).
- **Step 2, done.** `flm_validate.py` restored and repointed at a pack-resolved algorithm installed via `ops.set_flm_algo` (`Pod.ensure_flm_algo`) instead of the gone baked blob. Gap 6: a real-vendor-`.FLM` test now resolves from whatever CMSIS pack this machine has cached (`cmsis_pack.find_device`) and skips only when the cache is empty, so it runs on any machine with a pack cached without committing a vendor binary.
- **Step 3, partially validated on hardware (2026-09-03) against the live nRF52840 (fp `d83acd`).** Confirmed both via `flm_validate.py` (drives `FLMFlasher` directly: erase-sector + program-page + read-back) and via the real production path (`pod dut flash --addr 0xF8000 --loader flm`, host CRC-verify passing) at the scratch address `0xF8000`: the algorithm runs correctly end to end, and the DUT is left genuinely running afterward (`cm.is_halted()` false, `DFSR` clear) with no manual reset - gap 1's gate. **Not yet run**: a mass-erase (`erase_all`'s `EraseChip` path) or an ELF-segment flash. Both would exercise real, currently-untested code paths, but a mass-erase on this shared DUT wipes its actual MicroPython firmware and there is no restore image on hand to reflash afterward; deferred rather than run destructively without one. Gap 7 (the dead `--target`) is fixed and unit-tested (`resolve_flm_algo` receives it as `device=`) but not part of this hardware round - it only matters for a device with several algorithms, which the nRF52840 is not.
- **Found and fixed during this round, not one of the seven gaps**: driving `_select_loader("flm")` directly and calling `.load()` on the result (as `flm_validate.py` did) is unsafe across a `_flm_restore`-triggered reset within the same pod session - the cached `FLMFlasher`'s `_loaded=True` survives the reset even though the DUT's own firmware has since run over the blob's SRAM. `ops.py`'s own three entry points are unaffected (each calls `_flm_begin()` -> `reload()` unconditionally); `flm_validate.py` now calls `.reload()` too. See `dev-notes.md` item 12. Reproduced once (an `Init()` call hanging with PC parked mid-algorithm-blob-turned-DUT-firmware until the 8 s timeout fired), recovered with `cm.sysreset()`, and the scratch page re-erased back to how it was found.
- **Not started**: step 4 (flip the default) - blocked on step 3's full gate; step 5 (a second, non-Nordic family) - no such DUT is currently wired up; step 6 (delete `native`).

## Conventions for whoever picks this up

Flash is pure SWD, so the pod alone is enough for most of this. Run host commands from `src/host`, and make sure the `pod` console script is actually installed editable against the checkout you are working in, not a stale copy:

```
cd src/host
uv tool install --editable . --force
```

`zeroconf` and `mcp` are ordinary dependencies, not extras: both console scripts
this package installs need them (`pod discover`/`pod register` for mDNS,
`pod-mcp` for the whole agent-facing surface), so there is nothing left to
opt into. `mcp` is still capped below 2.0 in `pyproject.toml`, because 2.x
drops the decorator API `build_server` uses and `pod-mcp` then dies at startup
with `AttributeError: 'Server' object has no attribute 'list_tools'`. Installing
`mcp` via `--with` instead of letting the dependency resolve keeps that pin out
of version control entirely, which is how an unrelated `--force` reinstall can
silently pick up the broken version. Confirm with `python3 -c "import pod;
print(pod.__file__)"` and check it resolves into the `src/host` you meant, not
another checkout or worktree. The host suite baseline at the time of writing is
661 passed, 1 skipped.

Before any destructive recovery on the pod or the DUT, capture the state that explains the fault: for the DUT that means `DHCSR`, `DFSR`, `DEMCR`, `FP_CTRL` and PC/LR/SP over SWD. Reset and power-cycle destroy exactly the evidence that distinguishes a parked flash algorithm from a genuine fault, and the parked case is the likely one while gap 1 is open.
