# RP2350 pod: resume plan after the power-cycle

The pod is bricked and waiting on a physical power-cycle (the operator is remote).
This is the ordered runbook to pick up the moment it is back, plus the forward
plan it unblocks. It is the live counterpart to the auto-memory
`pod-rp2350-wifi-fix-plan` and `pod-rp2350-flash-xip-wedge`; where they disagree,
trust this doc and tell the maintainer.

## 0. Why the pod is down (context)

While flashing the Stage-0 arepl-fix firmware, `probe-rs download` halted core0
but the old `_thread` netboot kept core1 driving execute-in-place (XIP) from
flash; the erase was interrupted mid-flight and the external QSPI flash wedged in
continuous-read / QPI mode (bit-misaligned). The image header was half-erased, so
the pod no longer boots (Wi-Fi down). No SWD-accessible reset recovers it -
confirmed three ways: the bootrom `flash_exit_xip` (a `reset init` reaches the
bootrom at pc 0x88 and its own exit-XIP fails), a manual QMI direct-mode exit +
`0x66`/`0x99` software reset, and a QSPI-subsystem reset (there is no QMI-core
reset bit on the RP2350) + flash software reset in single and quad. The chip is
**undamaged** (status reg `0x05` reads WIP=0, the Winbond `ef` byte reads back),
so a power-cycle (a true power-on-reset of the flash) recovers it cleanly. Full
detail: `pod-rp2350-flash-xip-wedge` memory.

## 1. First: confirm the power-cycle recovered the flash

After the unplug/replug (or BOOTSEL-held power-on):

- Read the flash JEDEC ID over SWD and confirm it is clean and stable
  (`ef 40 16` for the W25Q32, not the shifted `40 16 ff ff` / `00 ef 00 00`
  garbage). Quick check: `probe-rs info --probe 2e8a:000c:0501083219160908 --chip
  RP235x` should enumerate, or the OpenOCD `reset init` flash auto-probe should
  succeed instead of "failed to exit flash XIP mode".
- The half-erased image will not boot, so the pod will not come up on Wi-Fi until
  reflashed. That is expected.

## 2. Make the flash path safe BEFORE reflashing (prevent a re-brick) - DONE

The brick's root cause was flashing while core1 ran XIP. Fixed 2026-06-16:
`make flash` now runs OpenOCD `program firmware.elf verify reset`, whose rp2350
`reset init` halts BOTH cores (cm0 and cm1) before any flash access, so neither
core executes from flash during the erase. Validated on the live pod (both cores
halted, `Verified OK`, clean reboot, no wedge). The old probe-rs path (halts only
core0) is kept as `make flash-probe-rs` for use only from a clean/bootrom state.
The single-core runtime (section 5) is still the structural fix that removes the
core1 mutator entirely. See `dev-notes.md` section 2 and the
`pod-rp2350-flash-xip-wedge` memory.

## 3. Integrate the native_async_repl consolidated SHA

The `native_async_repl:corona@carbon` agent is producing ONE consolidated SHA
(Andrew reviews before push) containing all of:

- arepl `\r\n` heading + Ctrl-B-exit fix (arepl.py:237 and :264) - mpremote's
  `enter_raw_repl` needs `read_until b"...\r\n>"`; the bare `\n` hung then
  fast-failed. Verified byte-correct against `pyexec.c:549/571`.
- F1 (serious): `raw_paste` returns bytes but `raw_repl` does `"".join(parts)` on
  it (arepl.py:278, outside the try/except), raising TypeError and killing the
  task. Since mpremote's default send path is raw-paste, this breaks
  `exec`/`fs cp`/`run`/`mount` on-device. Fix: `raw_paste` returns
  `b"".join(chunks).decode()` (decode the joined buffer to avoid a window-boundary
  UTF-8 split).
- F2 (minor): `raw_repl` exception path does `print(line)` (arepl.py:289), echoing
  the source into the normal-output stream; the C repl does not. Dropped.
- Issue-1 poll-RD: `mp_hal_stdio_poll` never reported the UART REPL stdin
  readable, so the poll-driven arepl never woke on UART input. Fix folded as a new
  rp2 commit (matching the existing `stdin_ringbuf` guard at mphalport.c:54 with
  `ringbuf_avail`). Already hardware-validated (hang -> response) pre-brick.
- The agent's own docs-xref + embed-build CI fixes (the current pin `957e43a901`
  has those 2 unrelated CI failures; the new SHA is clean).

Integration steps:

1. Discard the local validation edits so the SHA is the sole source (no
   divergence): `git -C src/micropython checkout -- extmod/asyncio/arepl.py
   ports/rp2/mphalport.c`.
2. Bump the `native_async_repl` pin in `mbm.toml` to the new SHA.
3. Recompose tessera: `mbm rebase --target master --local --no-force-push` (base
   stays `1c63211817`). Re-resolve the recurring `ports/rp2/CMakeLists.txt`
   keep-both conflict if it recurs (keep BOTH the PIO-USB suppression block and
   the `cdc_host.c` unused-variable suppression), then `git add` +
   `GIT_EDITOR=true git merge --continue` + `mbm rebase --resume`.
4. `make` (build) then `make flash` (now safe per section 2).

## 4. Validate arepl over UART - closes Stage 0

The branch was unix-tested only; rp2 + the UART REPL is the gap we fill. Over the
backup UART REPL (pod GP0/GP1 <-> probe GP5/GP4), run the full mpremote matrix:

- `enter_raw_repl` (the `\r\n` fix), raw-paste exec round-trip (F1), `fs cp`,
  `run`, `mount`, Ctrl-B exit.
- Sync Ctrl-C interrupting a blocking statement (the out-of-band recovery) and the
  traceback print (the folded arepl traceback fix).

Report rp2 results back to the `native_async_repl` agent (their branch's rp2
coverage). This closes Stage 0.

## 5. Stage 1 - single-core asyncio runtime (the actual Wi-Fi deadlock fix)

Root cause of the inbound-death: two hardware cores both mutate lwIP (core1's
`_thread` netboot supervisor + core0 PendSV/cyw43_poll), so an SWD-halt-frozen
`pendsv_mutex` spinlock becomes a permanent cross-core spin-wait and `cyw43_poll`
never drains RX. Architecturally correct fix = remove the second lwIP mutator.

- Replace the `_thread` netboot supervisor with a single `asyncio.run(main())` on
  core0; delete the `_thread`; park core1.
- Long-lived tasks: one `arepl.task(persistent=True)`; a socket accept task
  toggling `os.dupterm(conn)` (single slot, evict-previous, non-blocking conn,
  never read conn in the accept loop); `wifi_supervisor`; `usb_host_pump`
  (`mp_usbh_task` + `await sleep_ms(0)`). `usbip` stays the C lwIP-RAW server.
- `persistent` needs two arepl changes (confirm they are in the SHA or request a
  follow-up): `stop_loop_on_exit=False` AND Ctrl-D -> continue (arepl.py:315
  `return` is not gated by the flag). EOF is a non-issue while UART stays primary.
- Verify: the SWD halt/resume churn can no longer produce a running-deadlock (use
  the ping + UART + SWD-register harness). The old netboot (`netboot.py`
  `_thread.start_new_thread(_serve_supervise, ...)`) is the thing being replaced.

## 6. Stages 2-4 (after Stage 1 gate)

- Stage 2: operational evidence gap - the pod's own PIO-SWD debugging a DUT under
  Wi-Fi load - plus an Issue-2 regression suite.
- Stage 3: make the debug-stack long ops (`debug/swd_stream` flash, LA capture,
  `flash_nrf52`) await-cooperative so a long C op cannot block the single loop.
- Stage 4: docs/memory; feed the rp2 validation and the `task()` persistent change
  back to the upstream branch.

## 7. State of the tree at the pause (reference, no action)

Committed on `rp2350-pivot` this session:

- `52962a1` docs split (ESP32-S3 -> `docs/esp32-s3/`) + the RP2350
  `hardware-setup.md` guide.
- `cd07563` `_pinmap`/NRST collision fix (NRST is GP13 on RP2350, carrier cluster
  guarded).
- `0421f6e` Pico 2 W pinout diagram + nRST prose update.

Uncommitted / in-flight (intentionally not committed):

- `mbm.toml` - the `native_async_repl` branch entry (pin bumps in section 3).
- `src/micropython` - submodule pointer at tessera `9d111000ed` (base
  `1c63211817`), plus the local arepl/mphalport validation edits to be discarded
  in section 3 step 1.
- `Makefile` + `tools/uf2_to_bin.py` - the build formalization (commit when ready;
  fold in the section-2 flash-safety hardening first).
- `docs/rp2350/plan/phase-4-usb-host-usbip.md` - a pre-existing forwarder re-cut,
  unrelated to this session; commit with the forwarder work.
