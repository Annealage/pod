# R24 residual bug: investigation plan

## Symptom

Bulk-IN URBs submitted via `tuh_edpt_xfer` after a cancel-storm never
deliver data to the kernel. After fresh attach a single mpremote
`exec` succeeds; anything that triggers cdc-acm to UNLINK its bulk-IN
ring (TTY close+reopen, raw-REPL Ctrl-C/Ctrl-A/Ctrl-D sequence) leaves
the bulk-IN pipe non-functional. The firmware does not crash with the
current `r24-wip` HEAD (`75f2b35`); the URBs simply never deliver.
`cdc_throughput.py` cannot complete a single iteration because each
`read_test` enters raw-REPL via the same close-storm pattern.

## What R24 already fixes

For context. The five committed fixes on `r24-wip`:

1. `tuh_control_xfer` setup-pointer vs buffer-pointer independence
   (separate fields, not `[setup|data]` layout)
2. `tuh_xfer_t.setup` and `.buflen` UNION (don't write buflen for
   control xfers; setup gets corrupted into a small-int pointer →
   `EXCVADDR=0x40` LoadProhibited fault)
3. `CFG_TUH_API_EDPT_XFER=1` so bulk completion callbacks fire at all
4. `ep_submit_mutex` around `tuh_edpt_abort_xfer` to serialise vs
   concurrent submit
5. `USBIP_PIPELINE_DEPTH=1` (TinyUSB allows one xfer per (dev,ep);
   pipeline_depth=16 caused a flood of `tuh_edpt_xfer rejected`)
6. Cancel-synthesise URB completion via atomic CAS + per-(slot,ep)
   `current_inflight[]` tracking. TinyUSB's DWC2 `hcd_edpt_abort_xfer`
   does not reliably fire the user `complete_cb`; without synthesis,
   in-flight URBs leak after cancel and the next attach hits stale
   state.

Single-call mpremote works after these. Multi-call does not.

## Hypothesis space

Ranked by likelihood given the failure mode (URB submitted, transfer
times out silently, no crash):

| # | Hypothesis | Why plausible |
|---|-----------|---------------|
| H1 | TinyUSB `dev->ep_status[ep][dir].busy` or `.claimed` stuck after our cancel synthesis | We synthesise before TinyUSB's natural xfer-complete event clears these flags. Subsequent `usbh_edpt_claim` (called from `tuh_edpt_xfer`) would silently fail or set the EP busy without queuing a transfer. |
| H2 | DWC2 hardware channel state stale after `hcd_edpt_abort_xfer` | abort just disables the channel; if the next `tuh_edpt_xfer` reuses the channel without reset, the controller may queue but never generate IN tokens (channel half-disabled). |
| H3 | Race in our `current_inflight[]` slot under fast cycle | Atomic claim guards `inflight_t` free, but the slot pointer is cleared in two places (xfer_complete_cb and usbhost_cancel_ep). A late-firing TinyUSB callback for the just-aborted URB might re-clear an already-overwritten slot, or worse, the new URB's slot. |
| H4 | tx_owner stale across UNLINK→fresh-submit cycle | Each new URB allocates a fresh `inflight_urb_t` with tx_owner=0, so probably not, but worth checking handle_urb_stream's UNLINK_NO_MATCH path. |
| H5 | cdc-acm flooded with `-ECONNRESET` interprets device as broken | dmesg shows many `urb->status -104` after each session. cdc-acm may have a threshold above which it stops resubmitting. |

## Step 1: instrument and capture (this session)

Add diagnostic logs (verbose-gated, keep cost low):

- Per-submit: `ep_addr`, `inflight*`, return value of `tuh_edpt_xfer`
- Per-completion (xfer_complete_cb): `ep_addr`, `inflight*`,
  `xfer->result`, time delta from submit
- Per-cancel synthesis: `ep_addr`, `inflight*`, `completed` value
  before CAS, did-we-win
- A dump function readable via Python REPL:
  `usbhost.dump_ep(busid, ep_addr)` returns `(busy, claimed,
  current_inflight!=NULL, completed)` for that EP. Call it BEFORE
  the second mpremote and AFTER it timeouts to see what state is
  stuck.

Capture during a fresh-attach + mpremote 1 + mpremote 2 sequence with
verbose enabled.

Triage the resulting log to identify which hypothesis the evidence
supports. Look specifically for:

- Does `tuh_edpt_xfer` return true on the second mpremote's submits?
  - If false: H1 (claim failed)
  - If true: H2 or H3 (transfer accepted but never completes)
- Does our synthesis fire for the second mpremote's URBs?
  - If yes (we synthesise, kernel sees -ECONNRESET): submission is
    being aborted somehow. H1 most likely.
  - If no (URBs hang without synthesis): they're submitted but
    TinyUSB never fires anything. H2.
- What does `dump_ep` show post-failure?
  - busy=1, claimed=1, no inflight: TinyUSB thinks it's busy with
    nothing. H1.
  - busy=0, claimed=0: state looks clean but new submits don't fire.
    H2.

## Step 2 (if H1 confirmed): clear ep_status flags in synthesis

In our cancel synthesis path, AFTER claiming completion atomically,
reach into TinyUSB internals to clear `ep_status[ep][dir].busy = 0`
and `ep_status[ep][dir].claimed = 0`. This is bypassing the TinyUSB
abstraction but is internally what TinyUSB's own xfer-complete handler
does at line 655-656 of `usbh.c`. Risk: layering violation; if TinyUSB
adds more state to clear in a future version, we'd miss it.

Alternative within H1: don't synthesise — instead, after abort, poll
`ep_status.busy` for ~50 ms; if it clears, we got a natural
completion; if it doesn't, manually clear and synthesise.

## Step 3 (if H2 confirmed): re-open the EP after abort

```c
void usbhost_cancel_ep(...) {
    tuh_edpt_abort_xfer(daddr, ep_addr);
    synthesise_completion(...);
    // Cache desc at tuh_mount_hook time; restore here
    tuh_edpt_close(daddr, ep_addr);
    tuh_edpt_open(daddr, &cached_ep_desc[ep_idx]);
}
```

This forces DWC2 to release and re-allocate the channel. Heavier than
clearing flags but most likely to actually work for the channel-state
case. Need to cache the `tusb_desc_endpoint_t` per (slot, ep_idx) in
the slot table so we can re-open without rewalking the config blob.

## Step 4 (if H3 confirmed): refcount the inflight

Drop the single-owner assumption that R22 step 5 introduced. Add a
small refcount: 1 for "in TinyUSB's tracking", 1 for "in our
current_inflight slot", 1 for "in our pending-synthesis path". Each
exit path drops one ref; whoever drops the last frees. This is
heavier but correct under all possible orderings.

## Step 5 (escalation): use TinyUSB vendor class driver

`tuh_edpt_xfer` with `CFG_TUH_API_EDPT_XFER=1` is the experimental
"raw bulk forwarding" path. The blessed TinyUSB pattern for arbitrary
bulk forwarding is to register a vendor class driver via
`tuh_class_driver_t` that owns the EPs and gets proper lifecycle
callbacks (mount, umount, xfer_complete with full context).

Rewriting `usbhost.c` to plug into the class-driver framework is
~400-600 LOC of restructuring but lands us on a well-tested path. The
TinyUSB submodule we're on (machine-usbhost branch) likely has
examples we can crib from in `lib/tinyusb/examples/host/`.

Defer to step 5 only if H1/H2/H3 don't yield in 2-3 cycles.

## Acceptance criteria

R24 is "done" when ALL of these pass on `r24-wip`:

1. Smoke 5/0
2. mpremote 30/30 back-to-back, 0.5 s gap (matches R23 baseline)
3. R18 t+1s post-detach probe = 2
4. Concurrent CDC + CMSIS-DAP attach + pyocd reset
5. `cdc_throughput.py` runs to completion with a non-zero rate at
   each bufsize
6. No firmware crash, no leaked URBs (memory baseline stable)

Throughput improvement is NOT a criterion; we already proved the
kernel-side TCP RTT caps it at ~11 KiB/s regardless of stack (R23
findings, corrected section).

## Risk: what if it can't be made to work?

The R23 IDF backend is rock solid. If R24 can't be stabilised after
H1/H2/H3 fixes, the call is to keep R23 on main and park `r24-wip` as
research. The architectural alignment with upstream MicroPython that
motivated R24 is secondary; throughput parity is primary, and R23
already has it.

Document `r24-wip` outcome in `r24-findings.md`. Possible findings:

- "TinyUSB host on DWC2 cannot pipeline bulk EPs reliably under
  cancel-storm; staying with IDF host for production"
- "Vendor class driver path works (step 5 succeeded); switch to it"
- "All five fixes plus EP reopen lands; merge to main"

## Iteration budget

- 1 cycle: step 1 (instrumentation + capture)
- 1 cycle: step 2 (H1 fix)
- 1 cycle: step 3 (H2 fix) if step 2 insufficient
- 2 cycles: step 5 (vendor class) if H1+H2 fail
- 1 cycle: re-validate full matrix + throughput

5 cycles total. Any iteration that overruns triggers reassessment
(park r24-wip, document, return to R23-on-main).
