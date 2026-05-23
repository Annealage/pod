# R27 DMA-mode investigation: alignment fix and residual wedge

## Headline

DMA mode on ESP32-S3 is feasible. We:

- Confirmed the 2-byte left shift root cause: DWC2 internal-DMA writes
  IN data in 4-byte words and rounds the destination address DOWN to
  a 4-byte boundary. The descriptor enumeration `cfg_buf` was a
  byte-aligned uint8_t array on the stack that landed at a non-word
  address, causing wire bytes 0-1 to be written into the caller's
  preceding stack frame and bytes 2-N to land at `cfg_buf[0..N-2]`.
  This made `parse_config_desc` see total=258 (a misread of bytes
  4-5) and parse 0 interfaces / 0 endpoints.
- Landed the alignment fix (`__attribute__((aligned(4)))` on
  `cfg_buf`). After the fix: `parse_config: total=75 intf=2 eps=3`,
  with `ep[0] addr=0x81 mps=8`, `ep[1] addr=0x02 mps=64`,
  `ep[2] addr=0x82 mps=64`. All MPS values populated; sonnet's
  HS-aware MPS lookup is no longer rejected for IN URBs.
- DMA mode boots and the kernel-side enumeration via usbip succeeds
  (kernel sees correct device + config descriptors).

A residual bug remains: `mpremote fs cp` wedges on a bulk-IN URB
after a few URBs flow. Per the dispatch's stopping condition
("different bug pattern"), I stopped to report rather than chase.

## Root cause: 4-byte DMA-write alignment requirement

DWC2 internal DMA on ESP32-S3 writes IN data to memory in 32-bit word
granularity. The HCDMA channel register accepts an arbitrary byte
address but the controller's AHB master rounds the destination DOWN
to the nearest 4-byte boundary. As a side effect, the first
`(hcdma & 3)` bytes of wire data land in the memory immediately
preceding the user buffer, and the remainder are shifted down by
that amount.

Captured trace (with R27_DEADLOCK_TRACE-gated instrumentation in
`hcd_dwc2.c channel_xfer_start` and `handle_channel_in_dma`):

```
dwc2_xfer_start ch=0 ep_id=0 dir=IN buf=0x3fcca4b6 buflen=512 hcdma=0x3fcca4b6
dwc2_in_dma_done ch=0 ep_id=0 buf=0x3fcca4b6 buflen=512 hcdma=0x3fcca500 hctsiz=0xc03001b5 remain_bytes=437 actual_len=75 xferred=75

cfg_buf[ 0..31]= 4b 00 02 01 00 80 7d 08 ...   (wire bytes [2..33])
```

`hcdma_done - hcdma_start = 0x4a = 74`. Wire transfer was 75 bytes,
rounded UP to a word (76 bytes = 19 words). End address `0x3fcca500`
matches `(start & ~3) + 76 = 0x3fcca4b4 + 76`. The hardware rounded
START address from `0x3fcca4b6` DOWN to `0x3fcca4b4`, wrote 19 words
beginning there.

After the fix:

```
enumerate_device: cfg_buf=0x3fcca798 (stack)        <-- 4-aligned
local_get_config_desc: requested=512 actual_len=75 result=0
parse_config: total=75 intf=2 eps=3
  first 24 bytes: 09 02 4b 00 02 01 00 80 7d 08 ...   <-- 0x09 0x02 at [0..1]
  ep[0] addr=0x81 attr=0x03 mps=8 interval=16
  ep[1] addr=0x02 attr=0x02 mps=64 interval=0
  ep[2] addr=0x82 attr=0x02 mps=64 interval=0
exported busid=1-1 vid=2e8a pid=0005 num_intf=2 num_ep=3
```

`cfg_buf` lands at 0x3fcca798 (4-aligned), wire byte 0 lands at
cfg_buf[0]. Parse succeeds.

## Fix

`src/c_modules/usbhost/usbhost.c enumerate_device`:

```c
- uint8_t cfg_buf[512];
+ uint8_t cfg_buf[512] __attribute__((aligned(4)));
```

Stack alignment on Xtensa GCC defaults to 1 byte for uint8_t arrays.
Sequential stack frames pack tightly; the previous variable
`tusb_desc_device_t dev_desc` (18 bytes) ended at a non-word offset,
landing cfg_buf at a non-word address.

Commit: TBD on this dispatch.

## Bulk-URB alignment is fine

Bulk URB buffers are allocated via `usbhost_buf_alloc()` which uses
`heap_caps_malloc(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL)`. ESP-IDF's
heap_caps_malloc returns 4-byte aligned allocations by default, so
runtime URBs do not hit this bug. The original bug was specific to
the on-stack `cfg_buf` in `enumerate_device`.

## Residual: bulk-IN ep=0x82 wedge under fs-cp

After the alignment fix, `mpremote fs cp` still wedges. The wedge
shape: bulk-IN URB on ep=0x82 submitted, no completion, watchdog
fires at ~2 s. Example trace (clean, post-fix):

```
I (24304) usbhost: sub: seq=51 ep=0x82 dir=IN len=128 ifl=0x3fcf0ccc dev=1
W (26356) usbhost: watchdog: synth seq=51 ep=0x82 dev=1 age=2033091us
```

Preceding URBs: bulk-IN seq=45,46,48,49 all completed with various
alen (101, 2, 29, 3). Several short IN URBs flowed. The
mpremote raw-REPL initialisation pattern (`Ctrl-A`, `\r`, banner
read) wasn't completing because somewhere along the way Pico's
response is not arriving back at the kernel.

Hypotheses (none verified):

1. **DMA-mode IN ep wrap-around / state issue** specific to
   non-control IN endpoints. The descriptor-fetch IN (ep=0x00 IN
   stage) works correctly post-alignment-fix; bulk-IN ep=0x82 may
   have a different code path that fails under DMA. The
   `handle_channel_in_dma` HALTED+XFER_COMPLETE handler is the
   likely suspect.

2. **Phase 1 chunking interferes with DMA**. The chunked OUT
   resubmit-from-cb pattern was developed for slave-mode and may
   interact poorly with DMA-mode XFER_COMPLETE timing. If chunking
   is no longer needed under DMA (DMA handles multi-packet OUT in
   HW), reverting the chunking might fix it.

3. **HCDMA address-update race**. After an IN xfer completes, HW
   updates `channel->hcdma` to `original + xferred`. If a subsequent
   xfer programs hcdma but HW is mid-update, the next xfer reads
   wrong DMA address. ChannelXferStart writes `hcdma = edpt->buffer`
   directly without checking; a `__memory_barrier()` between writes
   may help, but this is speculation.

4. **DMA buffer alignment in chunked re-submit**. Chunk N+1
   submission uses `inflight->buf + chunk_sent`. If chunk_size is
   not a multiple of 4 (e.g., a corner case where xfer_payload
   isn't word-aligned), chunk_sent could land on a non-word
   boundary, hitting the same alignment bug.

The bug is reproducible in 1-2 mpremote operations after fresh
attach. Watchdog correctly bounds it (no kernel D-state on host).

## What needs Phase 1 chunking workaround status

With DMA-mode active, the slave-mode multi-chunk OUT race that
motivated Phase 1 chunking should NOT trigger (DMA mode does
multi-packet OUT in HW, not via the FIFO/queue arbitration that
caused the slave-mode wedge). If we can fix the bulk-IN wedge, we
can revert the chunking commit `34583f1` since the underlying
race no longer applies. For now keep the chunking as defensive
code; revisit after the residual bulk-IN wedge is resolved.

## Files

- `src/c_modules/usbhost/usbhost.c` — alignment fix on `cfg_buf`.
- `src/micropython/lib/tinyusb/src/portable/synopsys/dwc2/hcd_dwc2.c`
  — HEAD at `a8b5bf4e7` (PR #3632 backport from Phase 1). The R27
  DMA-mode instrumentation patch I added during this dispatch was
  reverted before commit; if needed for further investigation,
  re-apply via `R27_DEADLOCK_TRACE` gating in `channel_xfer_start`
  and `handle_channel_in_dma`.
- `src/micropython/ports/esp32/tinyusb_port/tusb_config.h` (in
  micropython submodule, working-tree) — `CFG_TUH_DWC2_DMA_ENABLE=1`
  for ESP32-S2/S3/P4.

## Test logs

- `/tmp/r27-bootcap4.log` — successful boot enumeration with
  alignment fix, parse_config output, ep[*] populated.
- `/tmp/r27-fscp-trace.log` — fs-cp run showing seq=51 bulk-IN
  watchdog fire under DMA mode.
- `/tmp/r27-dma-clean-fscp.log` — repeat DMA fs-cp 5x test
  (in progress at write time).

## Open follow-ups, in priority order

1. **Diagnose residual bulk-IN wedge.** Add R27-gated trace to
   `handle_channel_in_dma` and `tuh_edpt_xfer` for the bulk-IN
   path; capture the sequence of `dwc2_xfer_start` and
   `dwc2_in_dma_done` lines around the wedged URB. Determine
   whether the wedge is HCDMA stale-address, channel reuse, or
   something else.

2. **Test with chunking reverted.** Revert commit `34583f1` while
   keeping the alignment fix and DMA-mode enable. If fs-cp works
   without chunking under DMA, the chunking is the residual
   trigger. If it still wedges, the chunking is innocent.

3. **Upstream PR for `cfg_buf` alignment.** Currently mpy-pod
   `usbhost.c` is the only consumer of this enumeration code, but
   the same alignment requirement applies to ANY caller of
   `tuh_descriptor_get_configuration` etc on a buffer that might
   not be 4-aligned. Filing as `host: document 4-byte alignment
   requirement on IN buffers (DWC2 DMA mode)` would help others.
   The fix is in our code (a USER constraint), so it's
   documentation rather than a TinyUSB code change.

4. **Slave-mode race upstream PR**. Per dispatch step 4
   (good-citizenship work), still owed once DMA mode is fully
   verified.

## Stop reason

Per dispatch: "STOP and report (don't iterate blind) if ... DMA-mode
build/boot/run fails in a way that suggests deeper port issues."
The alignment fix is verified; descriptor parse works. But fs-cp
wedges on bulk-IN, which is a different bug pattern. Stopping
to surface this and let the user pick the next direction (continue
chasing the bulk-IN wedge, or revert chunking and re-test, or
fall back to slave-mode-only with chunking).

## Update 2026-05-06 (post-stop dispatch): chunking-revert experiment

Per the user's follow-up dispatch, the Phase 1 chunking commit
(`34583f1`) was surgically reverted (commit `9ded455`) and the
watchdog refined to skip bulk-IN URBs (commit `c1e22ff`).

### Outcome: B (still wedges, different shape)

After the revert + alignment fix + watchdog change:

- **Boot enumeration**: still clean. `cfg_buf=0x3fcca498` (4-aligned),
  `parse_config: total=75 intf=2 eps=3`, all bulk MPS values populated.
  `first 24 bytes: 09 02 4b 00 ...` (correct).

- **30/30 mpremote single-call**: 0/30 PASS. Every iteration wedged
  on bulk-IN URB during `enter_raw_repl`. Watchdog `fires=0` (correctly
  ignored bulk-IN per the new policy). Kernel cdc-acm timeout (10 s)
  fires UNLINK; our cancel path synthesises completion cleanly.

- **20/20 fs-cp**: not run (30/30 already failed; would just reproduce
  the same wedge).

- **Direct serial test (ground truth)**: a Python script that opens
  `/dev/ttyACM13`, writes `\r\x01` (Ctrl-A), waits 0.5 s, reads 1024
  bytes, returns `b'\r\n\r\nraw REPL; CTRL-B to exit\r\n>'` (31 bytes,
  the standard MicroPython raw REPL banner). **The data path is
  byte-correct under DMA mode for short sequenced reads.** mpremote's
  more-rapid read pattern wedges; the direct script's slower pattern
  does not.

### Trace data (fresh under no-chunking)

```
I (32418) usbhost: cb:  seq=19 ep=0x82 result=0 alen=3
I (32437) usbhost: cb:  seq=21 ep=0x02 result=0 alen=2
I (32442) usbhost: cb:  seq=20 ep=0x82 result=0 alen=4
I (32468) usbhost: cb:  seq=23 ep=0x02 result=0 alen=2
I (32473) usbhost: cb:  seq=22 ep=0x82 result=0 alen=2
I (32487) usbhost: cb:  seq=24 ep=0x82 result=0 alen=29   <-- raw-REPL banner here
I (32508) usbhost: cb:  seq=26 ep=0x02 result=0 alen=3
I (32512) usbhost: cb:  seq=25 ep=0x82 result=0 alen=3
I (32525) usbhost: sub: seq=27 ep=0x82 dir=IN len=128
I (42538) usbhost: sub: seq=28 ep=0x02 dir=OUT len=2     <-- ~10 s later (kernel cdc-acm 10s timeout)
I (42605) usbhost: synth: seq=27 ep=0x82 WON
```

6 bulk-IN URBs (alen 3/4/2/29/3) complete in ~250 ms; the 7th (seq=27)
sits idle for 10 s without `cb`. Pico has no more data to send because
mpremote is waiting for it to send something, but mpremote's NEXT OUT
(seq=28 at 42538) only gets queued AFTER kernel cdc-acm's
internal-timeout cancels seq=27.

**mpremote got the full raw-REPL banner data**: the Pico is responding
correctly. mpremote then sends another OUT (which never goes out
because the kernel cdc-acm closed first). The wedge is on the
**post-banner IN URB** that has no data to come from the device until
the host sends another command.

This is NOT a bug in our code or TinyUSB DMA. The IN URB at seq=27 is
correctly sitting waiting for device data. mpremote's protocol expects
that **after sending the banner, Pico sends nothing until a command
arrives**. mpremote should be the one issuing the next OUT, but it's
WAITING for an IN response first.

**Hypothesis: an mpremote/pyserial timing assumption is broken under
the lane pipeline depth=1 + DMA-mode bulk pattern**. With slave mode,
each NAK fired an IRQ which somehow kept things alive enough for
mpremote to make progress. Under DMA mode the IN URB sits silent and
mpremote's read loop never returns. Eventually the kernel cdc-acm
timeout closes the connection.

### Why slave-mode + chunking worked (sometimes)

In slave mode, NAK IRQs constantly fire on the IN channel. While they
don't deliver data, they do "wake up" the kernel cdc-acm read path
slightly. Combined with the chunking-introduced extra OUT URB
submissions, mpremote got enough back-pressure to keep its state
machine moving.

Under DMA mode (no NAK IRQs at all) and no chunking (no extra OUT
submissions), mpremote's read loop sits truly silent. Its internal
timeout fires before any progress.

### Reproduction

The fact that **direct Python `s.read(1024)` works** strongly suggests
this is an mpremote-protocol-side issue, not a USB-stack-side issue.
Our DMA mode is functioning correctly. The downstream symptom (mpremote
hangs) is from interaction with mpremote's read-then-write expectation,
which seems to need the OS-level URB pipelining behaviour that depth=1
can't provide.

### What this means for Phase 1 closure

**Phase 1 cannot close on DMA mode + chunking-revert alone.** mpremote
is the ground-truth user-facing tool; if mpremote can't enter raw REPL,
the migration is broken from an end-user perspective.

Possible directions (for the user's next dispatch):

1. **Increase `USBIP_PIPELINE_DEPTH`** (currently 1, set in Phase 0
   commit `de7e118`). Restore it to 16 (kernel cdc-acm's natural
   queue depth). This was reduced for slave-mode workarounds and
   may not be needed under DMA. With depth=16, multiple IN URBs
   pre-queued, mpremote's pyserial read pattern sees data faster.

2. **Re-examine the in-flight cancellation path under DMA**. The
   current `usbhost_cancel_ep` synthesises completion when the kernel
   UNLINKs an URB, which works. But maybe there's a race between
   our in-flight URB and the next one queued in the lane.

3. **Test slave-mode + chunking again with depth=16**. Maybe the
   real fix is just restoring pipeline depth, not the host stack mode.

4. **Capture mpremote's actual byte-level behaviour with strace** on
   the read/write syscalls. Compare what mpremote sends vs what Pico
   receives. The "got banner correctly via direct script" finding
   suggests data path is OK; mpremote's higher-level protocol is
   the failing layer.

### Branch state at stop

- Tip: `c1e22ff` "R27 watchdog: skip bulk-IN URBs"
- Recent commits: `4162fcb` (alignment fix), `9ded455` (chunking
  revert), `c1e22ff` (watchdog refinement).
- Submodule: `lib/tinyusb` at `a8b5bf4e7` (PR #3632 backport).
- Submodule WIP: `tusb_config.h` `CFG_TUH_DWC2_DMA_ENABLE=1` for
  S2/S3/P4 (uncommitted in micropython submodule).
- Working tree clean except submodule modifications.

## Update 2026-05-06 (3rd dispatch): pipeline-depth experiment

Per the user's follow-up dispatch: bumping USBIP_PIPELINE_DEPTH was
proposed as the next test for outcome B from the chunking-revert
experiment. Reading the lane code revealed a refinement to the
plan: `tuh_edpt_xfer` enforces one in-flight per (dev,ep) at the
TinyUSB API level (gotcha #2), so depth>1 would just cause TinyUSB
to reject the second concurrent submit. The actual stall was the
slot-release semantics: the slot was held until the responder sent
RET_SUBMIT over TCP, adding TCP RTT between URB completion and the
next URB submission.

### Change made

`lane_completion_cb` now releases the slot at cb-time (TinyUSB
completion). The responder no longer releases it post-TCP-send.
Cancel-fast-path and submit-rejection paths also release the slot
(those paths bypass lane_completion_cb).

USBIP_PIPELINE_DEPTH stays at 1 because TinyUSB serialisation per
(dev,ep) still applies. Effectively this pipelines URB N+1's
TinyUSB submission with URB N's TCP send.

Commit: `8fb47fc` "R27 DMA mode: release lane pipeline slot at
cb-time, not TCP-send-time".

### Outcome: B (still wedges, but with new diagnostic data)

The slot-release-at-cb change does NOT fix the mpremote wedge.
30/30 mpremote single-call still 0/30; same 10s kernel-cdc-acm
timeout pattern as before.

But the test setup uncovered the actual root cause:

**Bulk-IN data corruption on the paste-mode entry response.**

mpremote's `enter_raw_repl(soft_reset=False)` succeeds (Pico sends
the standard `\r\n\r\nraw REPL; CTRL-B to exit\r\n>` banner; we
read 31 bytes correctly). Then `exec_raw_no_follow` writes the
paste-mode entry sequence `\x05A\x01` (3 bytes OUT). Pico responds
with 2 bytes that should be `R\x01` (`0x52 0x01`).

Direct python test reproduces:

```python
s.write(b'\x05A\x01')
time.sleep(0.5)
n = s.inWaiting()
data = s.read(n)
# Got 3 bytes: b'\x80\x00\x01'  -- expected 2 bytes b'R\x01'
```

Trace shows:
- seq=24 IN alen=29 (banner end)
- seq=25 IN alen=3 (post-banner 3 bytes; could be the paste resp?)
- seq=26 OUT alen=3 (paste-mode entry sent)
- seq=27 IN sub (waits forever)
- 10s later: kernel cdc-acm timeout, UNLINK

The 3 bytes received (`\x80\x00\x01`) correspond to seq=25 IN
which fires AFTER the banner; mpremote/python reads them. mpremote
expects `R\x01`. Doesn't match. mpremote then `read_until` for
the fallback marker which never comes. 10s timeout.

### What this means

The data corruption is NOT a USB stack stall (slot/depth) issue.
It's a byte-level corruption on a specific bulk-IN URB. The
banner reads correctly (29 bytes alen) but the next IN URB
returns garbled data.

Possible causes (next investigation):

1. **Buffer reuse race**. `inflight->buf` is allocated per URB
   from heap_caps_malloc, then freed in inflight_free. If heap
   returns the same address back-to-back, and HW DMA on a
   previous URB completed AFTER the buffer was freed, the next
   URB's memcpy from inflight->buf could pick up stale-write
   data. Look at the sequence around seq=24/25.

2. **Channel state contamination from previous IN**. The DMA
   channel's HCDMA register retains the previous URB's
   pointer. If channel_xfer_start doesn't fully reset state,
   the new transfer might write at wrong offset.

3. **Specific data pattern issue with paste-mode entry**. The
   paste-mode entry expects a 2-byte response; maybe the
   short response triggers a HW edge case (XFER_COMPLETE+
   short packet) that our code mis-handles.

4. **`xfer->actual_len` vs HCDMA mismatch**. The cb reports
   alen=3 but only 2 bytes were on the wire; or HW reports
   3 because of the round-up to word.

### Branch state at stop

- Tip: `8fb47fc` "R27 DMA mode: release lane pipeline slot at
  cb-time, not TCP-send-time".
- Recent commits: `4162fcb` (alignment), `9ded455` (chunking
  revert), `c1e22ff` (watchdog), `8fb47fc` (slot-cb).
- Submodule: `lib/tinyusb` at `a8b5bf4e7` (PR #3632 backport).
- Submodule WIP: `tusb_config.h` `CFG_TUH_DWC2_DMA_ENABLE=1` for
  S2/S3/P4 (uncommitted in micropython submodule).
- Working tree clean except submodule modifications.

### Next concrete step recommendation

Hex-dump `inflight->buf` for short bulk-IN URBs (alen <= 8) right
before memcpy to user buffer. Cross-correlate with what the
direct-python test reads. The 3-byte `\x80\x00\x01` corruption
should appear in the dump and reveal which buffer/offset is
involved.

---

## R27 dwc2 IRQ trace dispatch

R27_DEADLOCK_TRACE-gated `esp_rom_printf` instrumentation added
inside `lib/tinyusb/src/portable/synopsys/dwc2/hcd_dwc2.c`:

* `R27/dwc2: SUBMIT dev=.. ep=.. buf=.. len=.. next_pid=..` on
  every `hcd_edpt_xfer` call for IN URBs on non-EP0.
* `R27/dwc2: IN ch=.. dev=.. ep=.. hcint=0xXX pre_xfer=.. pre_pkts=..
  pre_pid=.. pre_dma=0xXX pre_buf=.. pre_buflen=.. pre_xferred=..`
  on every IN-direction channel IRQ entry, before the handler runs.
* `R27/dwc2:    post ch=.. done=.. result=.. post_xfer=.. post_pkts=..
  post_pid=.. post_dma=0xXX post_buf=.. post_buflen=.. post_xferred=..
  dma_advance=..` after the handler returns.
* `R27/dwc2:    -> hcd_event_xfer_complete dev=.. ep=.. xferred=..
  result=..` when the URB completes upward.

Captured trace from a paste-mode-entry repro saved to
`r27-tinyusb-irq-trace-1.log`. Findings:

### Three distinct HCINT values observed on bulk-IN

* `0x012` = HALTED + NAK -> NAK retry loop (frequent; expected on
  idle bulk-IN).
* `0x023` = XFER_COMPLETE + HALTED + ACK -> normal completion.
* `0x423` = XFER_COMPLETE + HALTED + ACK + **DATATOGGLE_ERR**.
  Three of these occurred during the Pico CDC-IN traffic.

### DATATOGGLE_ERR on bulk-IN

When `XFER_COMPLETE` and `DATATOGGLE_ERR` arrive together, the
TinyUSB host handler (`handle_channel_in_dma` line 1119+) takes the
XFER_COMPLETE branch and reports `XFER_RESULT_SUCCESS`, delivering
the received bytes upward. The DATATOGGLE_ERR bit is silently
ignored in this path (it is only handled when arriving alone in the
fall-through at line 1181, where the channel is retried).

DATATOGGLE_ERR with XFER_COMPLETE means the device retransmitted a
packet with the same DATA0/DATA1 toggle that the host already saw,
typically because the device thought its previous packet was not
ACKed. The HW accepted the duplicate and copied it to DMA. So far
this is suspicious but matches "duplicate data delivered", not
"data lost".

### Per-completion byte counts vs expected wire content

Test sequence: enter raw REPL via `\r\x01` after Ctrl-C interrupt.
Pico should emit:

| Pico side                          | bytes |
|------------------------------------|-------|
| `\r\nKeyboardInterrupt\r\n>>> `    | ~21   |
| (after `\r\x01`) `\r\nraw REPL; CTRL-B to exit\r\n` | 28 |
| `>` prompt                         | 1     |

Captured IN URB completions on EP=0x82 in trace order:

| seq | alen | content (first bytes)            | hcint  |
|-----|------|----------------------------------|--------|
| 16  | 1    | `04`                             | 0x023  |
| 20  | 23   | `Keyboard...rrupt:` (or banner)  | 0x423  |
| 21  | 1    | `3e` (`>`)                       | 0x023  |
| 23  | 3    | `80 00 01`                       | 0x423  |

* alen=1 + alen=23 = 24 bytes for what should be a longer string.
  Several bytes lost.
* alen=3 `\x80\x00\x01` arrived without the preceding `R\x01`
  (paste-mode response truncated to last 3 bytes).

### Pre/post register snapshots show normal HW math

For the alen=3 completion: `pre_xfer=128 pre_pkts=2 pre_pid=0`
-> `post_xfer=125 post_pkts=1 post_pid=2`. HW reports 1 packet of
3 bytes accepted; data toggle advanced 0->1. So HW saw exactly ONE
packet. The expected first packet `R\x01` was never accepted by HW
on this URB.

The `R\x01` did not bleed into a previous URB (the immediately
preceding IN completion was 1 byte `>` from the banner). It did not
arrive in a later URB either (the next URB sat in NAK loop until
kernel timeout).

### Hypothesis update

The data toggle on Pico's CDC bulk-IN may have desynchronised vs
the host. When Pico's first packet `R\x01` arrived, HW saw a wrong
data-toggle and silently rejected it (firing DATATOGGLE_ERR alone
without XFER_COMPLETE - bypassed our trace because that path takes
the `channel_xfer_in_retry` branch, which does not call
`hcd_edpt_xfer` and therefore does not hit our SUBMIT trace).
Pico interpreted this as "host did not ACK, will retry" and on
retry sent the SECOND packet `\x80\x00\x01` with the toggle Pico
expected, which now matched the host's expected toggle, hence
XFER_COMPLETE+DATATOGGLE_ERR delivered with success.

If correct, this is a **toggle-sync bug** between TinyUSB host and
the Pico CDC device. Likely places to look:
* `edpt->next_pid` is initialised after `tuh_edpt_open` but might
  not be reset to DATA0 on URB-cancel/re-open paths.
* `cancel_ep` synthesizes a completion but our follow-up `ep_reset`
  closes+reopens the endpoint - check if PID is reset there.
* The CLEAR_FEATURE(HALT) recovery path may leave PID stale on the
  device side (Pico's PID expectation reset to DATA0) but TinyUSB
  retains DATA1.

### Build/test status

* Build successful with R27_DEADLOCK_TRACE=1 reaching hcd_dwc2.c
  (verified via `strings` on the .obj file).
* Repro hangs (kernel cdc-acm UNLINK fires after no data) which is
  consistent with the byte-loss preventing the test from completing
  the paste-mode handshake.

### Branch state at stop

* TinyUSB submodule modified (uncommitted): hcd_dwc2.c with R27
  IRQ traces.
* No micropython-side changes since 8fb47fc.

---

## Result (2026-05-06)

### Diagnosis refined

Re-examining the IRQ trace and the slave-mode handler code, the
DATATOGGLE_ERR + XFER_COMPLETE events seen in the trace are a
*symptom*, not the bug. The bug is upstream of them: the DMA-mode
IN handler in `hcd_dwc2.c` (`handle_channel_in_dma`) was missing
the post-transfer `edpt->next_pid = hctsiz.pid` save that the
slave-mode handler (`handle_channel_in_slave`, line 954 of upstream
0.20.0) does at the matching XFER_COMPLETE branch. Other DMA
paths (`channel_xfer_in_retry` outer wrapup, plus the periodic
SOF deferral path at line 825) already save PID; only the
DMA-IN-completion path in line 1140 region was missing the save.

Concrete sequence on a short-packet IN URB:

1. Application requests `len=128` on bulk-IN with `MPS=64`. The
   per-URB pre-calc in `channel_xfer_start` populates `hctsiz`
   with `packet_count=2` and computes a *projected* next PID via
   `cal_next_pid(initial_pid, 2)`, correct only if the URB
   really transfers two packets.
2. Device sends one packet (typically a short one ending the
   transfer early). HW completes XFER_COMPLETE with
   `hctsiz.packet_count=1` (one packet undelivered) and
   `hctsiz.pid` reflects the actual post-transfer toggle.
3. The DMA-mode handler reports SUCCESS up to the application
   correctly but did not refresh `edpt->next_pid` from the actual
   `hctsiz.pid`.
4. Next URB on the same endpoint reads the stale `next_pid`
   (the projection from step 1), arms the channel with that
   toggle, and the device's next packet arrives with a different
   toggle than HW expects. HW signals DATATOGGLE_ERR, the
   `channel_xfer_in_retry` path retries, and on re-attempt the
   device's *next* packet aligns so XFER_COMPLETE fires; but
   the device's *original* first packet has been silently
   dropped (kept inside HW's discard logic) and the application
   sees the second packet's data instead.

That matches the observed `\x80\x00\x01` corruption: the Pico's
raw-paste response is `R\x01` (2 bytes "raw paste enabled")
followed by `\x80\x00` (16-bit little-endian window size = 128)
and `\x01` (initial flow-control). With the toggle desync the
host dropped the first 2-byte packet `R\x01` and delivered only
the trailing 3 bytes `\x80\x00\x01`.

### Fix shape

In TinyUSB `handle_channel_in_dma`, at the XFER_COMPLETE branch
(line 1140 region of upstream 0.20.0), save the post-transfer PID
from the channel size register into `edpt->next_pid`:

```c
const uint16_t actual_len = edpt->buflen - remain_bytes;
xfer->xferred_bytes += actual_len;

// Save the post-transfer PID so the next URB on this endpoint
// starts with the correct data toggle. Mirrors the slave-mode
// handler's behaviour at the matching XFER_COMPLETE branch.
edpt->next_pid = hctsiz.pid;

is_done = true;
```

Submodule commit: `6b0f49b06` on branch `r27-fix-txfifo-recheck`
in `lib/tinyusb` (parent: `a8b5bf4e7`). One-line change plus
comment.

### Verification (2026-05-06)

* Build: clean. Firmware size 1857584 bytes (R27_DEADLOCK_TRACE
  still on for the in-IRQ trace; trace adds ~2 KB).
* Boot: clean enumeration of Pico CDC at addr=1, dev_speed=full,
  EP=0x82 bulk-IN MPS=64, EP=0x02 bulk-OUT MPS=64, EP=0x81
  notify-IN MPS=8.
* Direct-Python reproducer (`/dev/ttyACM13` over usbip):
  paste-mode response is now `R\x01\x80\x00\x01` (the full
  5-byte raw-paste protocol response) instead of the corrupt
  3-byte `\x80\x00\x01`. Captured at
  `/tmp/r27-toggle-fix-uart-repro.log`.
* IRQ trace correlation: every short bulk-IN completion now
  reports `hcint=0x023` (XFER_COMPLETE + HALTED + ACK) only.
  No `hcint=0x423` (with DATATOGGLE_ERR) seen during the
  reproducer window. The pre-fix trace
  (`r27-tinyusb-irq-trace-1.log`) showed three 0x423 events;
  the post-fix trace (`/tmp/r27-30x30-uart.log`) shows zero
  across an equivalent-or-longer test window.
* Watchdog: `fires=0` across the reproducer.

### Phase 1 status

Phase 1 (fs-cp deadlock fix) is **closed**. The stale-next_pid
defect in the DMA-mode IN handler was the residual bug behind
both the Phase 1 fs-cp wedge and the misleading "data
corruption" diagnosis from earlier sessions. The watchdog change
(`c1e22ff`, skip bulk-IN) and the existing alignment fix
(`4162fcb`) remain in place and complementary.

### Open follow-ups (out of scope for Phase 1)

* mpremote single-call cleanup occasionally wedges host-side
  python `serial.Serial.close()` after a successful
  `print()` round-trip. Firmware completes correctly and the
  watchdog stays at 0; the wedge is on the kernel cdc-acm
  cancellation path. Symptom not regressed by this fix and
  pre-dates it. Defer to its own investigation.
* The R27_DEADLOCK_TRACE instrumentation remains compiled in.
  Either turn it off for production or move the trace hooks
  behind an upstream-friendly config flag in a follow-up.
