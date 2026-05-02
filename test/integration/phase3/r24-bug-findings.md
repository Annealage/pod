# R24 residual bug investigation findings

> **Superseded by `r24-wip-history.md`** in this same directory, which
> captures the full picture (seven TinyUSB-on-DWC2 gotchas, why the R24
> pivot rationale itself is unverified, how to resume). Read that one
> first; this file is a snapshot from one earlier debugging step.

Following `r24-bug-plan.md`. Step 1 (instrument), step 2 (skipped, H1
refuted), step 3 (close+open EP), and additional CLEAR_FEATURE
landed. r24-wip HEAD `3285e6c`.

## What now works

**Single-call mpremote: 30/30 pass.** Empirical: ran 30 sequential
`mpremote connect <pico-tty> resume exec "print('R24-N')"` calls with
0.5 s gap; all 30 returned correct output. Pattern PPPPPPPPPPPPPPPPPPPPPPPPPPPPPP.

This is the typical mpy-dev workflow (each command opens fresh TTY,
runs single exec, closes). For this workload R24 is at functional
parity with R23 main.

## What still does not work

**Multi-step single-session protocols hang.** Examples that fail:

- `mpremote ... resume fs cp <local> :remote` (uses raw-REPL paste-mode,
  many eval cycles per session)
- `cdc_throughput.py read_test` (uses pyboard.Pyboard.enter_raw_repl,
  many iterations within one open)

When `fs cp` hangs, the Linux side puts the mpremote process into
**kernel D-state on `usb_poison_urb`** because cdc-acm cancelled URBs
that our firmware never gives back. Power-cycling the S3 does not
release vhci_hcd's URB poison; the mpremote process is unkillable
until full system reboot or vhci_hcd module unload (which itself can
hang if any vhci device has poisoned URBs).

This makes R24 unsafe for streaming workflows.

## What the step 1 instrumentation showed

Live UART trace from a successful 30/30 mpremote run plus the
hung fs cp run:

| Metric | 30/30 mpremote (working) | fs cp (hangs) |
|--------|-------------------------|---------------|
| `natural_won` (TinyUSB callback fires for bulk URBs) | ~50/call | 65 then stops |
| `cancel_ep` invocations | ~16/call (close-storm per call) | 3 total (0 on bulk-IN ep 0x82) |
| `tuh_edpt_xfer rejected` | 0 | 0 |
| `ep_reset:` invocations | 14/call | 0 |
| `synth_won` (we synthesised completion) | 14/call | 1 (control xfer) |

For mpremote's close-storm (cdc-acm UNLINKs all 16 URBs on close), we
abort, close+open, CLEAR_FEATURE the EP, synthesise -ECONNRESET. All
16 URBs get cleanly cancelled and the next mpremote attach starts
fresh. Works.

For fs cp's hang, the cancel path is **not** involved. 65 natural
completions go through, then stop. The kernel cdc-acm sends bulk-OUT
to write the script, expects bulk-IN response. One of those bulk-INs
either doesn't get submitted by us, doesn't complete in TinyUSB, or
completes with wrong data and the kernel drops it.

Adding firmware verbose log lines per submit/complete/cancel of every
URB during fs cp produced a stream of `usbip_in:` and `R23 batch send`
lines until natural_wins=65, then silence. The firmware appears
healthy (no panics, kernel-side TCP still flowing). It's the URB
delivery semantics during normal multi-step flow that's broken.

## What's confirmed about TinyUSB host on DWC2

Useful intel for any future TinyUSB-host work on this MCU:

1. `tuh_edpt_xfer` requires `CFG_TUH_API_EDPT_XFER=1` to fire user
   callbacks; default 0 silently drops them.
2. `tuh_edpt_xfer` allows **only one** transfer in flight per (dev,
   ep). Submit while busy returns false; pipeline depth must be 1
   at the lane level if not class-driver-managed.
3. `tuh_xfer_t.setup` and `.buflen` are in a UNION; setting both
   for control transfers corrupts the setup pointer
   (EXCVADDR=0x40 LoadProhibited fault).
4. `tuh_edpt_abort_xfer` does NOT reliably fire the user
   `complete_cb` for bulk EPs. Synthesise the completion explicitly
   in user code, gated by an atomic CAS to deconflict with any
   late-firing TinyUSB callback.
5. `tuh_edpt_abort_xfer` leaves the DWC2 channel in a half-allocated
   state. Subsequent `tuh_edpt_xfer` calls on the same EP fail until
   the EP is fully `tuh_edpt_close` + `tuh_edpt_open` cycled.
6. Even after close+open, the device-side data toggle is not reset.
   `tuh_edpt_clear_feature_stall`/CLEAR_FEATURE(ENDPOINT_HALT) via
   tuh_control_xfer is required to sync the device toggle to DATA0.
   Without it, alternating IN transactions get rejected as stale,
   producing a 50% intermittent failure rate.
7. Holding any user-level mutex around `tuh_edpt_abort_xfer`
   deadlocks against TinyUSB's internal `_usbh_mutex` if a
   class-callback is concurrently active. Don't.

These are non-obvious gotchas that the upstream TinyUSB host docs
don't call out clearly. r24-wip's commits embed them as comments
and code patterns for any later work.

## What it would take to make R24 ship-ready

The fs cp hang is not in the cancel path; it's in normal-flow URB
delivery. Hypotheses (untested in this round):

A. A bulk-OUT URB hangs at some point during the script-write loop.
   Possible kernel-side: vhci submits bulk-OUT, our firmware accepts
   but never returns RET_SUBMIT. Need verbose URB log gated on
   bulk-OUT EP specifically to confirm.

B. Bulk-IN data corruption mid-stream. cdc-acm receives the URB
   data, computes it doesn't match the expected raw-REPL framing,
   discards. Eventually cdc-acm gives up. Need to capture actual
   bulk-IN payload bytes and compare to what the Pico sent.

C. Race between control xfer (raw-REPL paste mode uses Ctrl-D bytes
   sent inline with bulk-OUT script bytes) and bulk transfers on the
   same connection. Maybe a control xfer mid-stream desynchronises
   us.

Each hypothesis is one cycle to confirm. Three cycles total to land,
roughly.

## Recommendation

**Park r24-wip. Stay with R23 IDF backend on main for shipping.**

R24's value was the architectural-future story (TinyUSB alignment with
upstream MicroPython, simpler stack story). The throughput investigation
(R21-R23 corrected) already proved that the throughput ceiling is
kernel-side TCP, not the USB host stack — so R24 doesn't unlock
throughput parity with direct USB.

For users who need streaming workflows (file transfer, mpremote
fs cp, cdc_throughput.py): R23 main is rock-solid. For the future
TinyUSB pivot, the seven gotchas above plus the structural fixes on
r24-wip (six commits) are the foundation; another round of focused
investigation can carry it across the line, but not in this iteration.

## Files committed on r24-wip

The branch holds the entire R24 attempt, all 11 commits including:

- `1697eea` setup/buffer independence
- `f1de4b3` UNION fix (the EXCVADDR=0x40 crash)
- `1130bbc` CFG_TUH_API_EDPT_XFER=1, pipeline_depth=1
- `75f2b35` cancel synthesis with atomic claim
- `acc2c5b` no ep_mutex around abort (deadlock fix)
- `a832f62` close+open EP after abort
- `3285e6c` CLEAR_FEATURE(ENDPOINT_HALT) after close+open

Plus two earlier infrastructure commits (`d9f192f`, `2ed6ea2`,
`7aead05`) and the bug investigation log (this file lands on main as
documentation, the experimental code stays on the branch).
