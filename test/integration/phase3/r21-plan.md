# R21 plan: larger USB transfer sizes for streaming throughput

Goal: identify and remove the per-URB latency floor that caps sustained
streaming through `usbip` to a few hundred KB/s, and push closer to the
Pico's full-speed bulk peak (~1.0-1.2 MB/s wire).

R21 follows R20. R20 removes per-URB software overhead (queue hop,
done-gate spin, worker context switch). R21 attacks the per-URB
overhead that remains after R20: each URB pays a fixed cost (IDF
submit + device round trip + IDF callback + TCP send), which at
small URB sizes dominates throughput. Larger URBs amortise this fixed
cost across more bytes.

R21 is at least partly investigation, not just implementation. The
plan therefore lists experiments before edits.

## Current state to verify before changing anything

The ESP-side caps are already generous:

- `USBIP_MAX_TRANSFER_DEFAULT` = 16 KiB (server config knob,
  `usbip_server.c:81`).
- `USBHOST_MAX_TRANSFER` = 64 KiB (`usbhost.c:67`).
- IDF `usb_host_transfer_alloc(xfer_len, ...)` is called with the
  payload length up to `USBHOST_MAX_TRANSFER`, MPS-rounded for
  bulk-IN at `usbhost.c:813`.

So the ESP firmware is already willing to handle multi-KiB transfers.
Whether it ever sees them in practice is a kernel-side question:
Linux `cdc-acm` chooses the URB size for bulk reads. Confirm by
inspection before doing any firmware work.

## Experiments (run before editing firmware)

The implementing agent should run these on the actual hardware
(`mpy-dev` ESP32-S3 + Pico CDC + S3-on-host USB/IP attach) and
record results in `r21-findings.md`.

### Experiment 1: measure per-URB sizes the kernel actually issues

Enable verbose URB logging on the firmware:

```python
import usbip
usbip.set_verbose(True)
```

Drive a sustained read from the Pico CDC (script below). Capture
`usbip_in:` log lines from the ESP UART trace and tabulate
`transfer_buffer_length` per URB on EP `0x81` (or whatever the bulk-IN
EP is for the Pico CDC).

```bash
# Pico-side: stream a 64 KiB buffer over CDC
# (place /blob.bin on the Pico's flash first)
mpremote connect /dev/serial/by-id/<pico-cdc> resume exec '
  f=open("/blob.bin","rb")
  while True:
    d=f.read(512)
    if not d: break
    import sys; sys.stdout.buffer.write(d)
'
```

Hypothesis: kernel `cdc-acm` calls `usb_endpoint_maxp(epread) * 2` for
read-buffer sizing on full-speed bulk pipes (`drivers/usb/class/cdc-acm.c`
`acm_read_buffers_alloc`), giving 128-byte URBs on a Pico that
advertises 64-byte MPS. Confirm or refute by measurement.

Record: median, min, max, and most-common `transfer_buffer_length` for
bulk-IN URBs across a 64 KiB stream.

### Experiment 2: measure per-URB latency

Patch `run_inflight` to log `xTaskGetTickCount()` deltas:
- t0 = entry to `run_inflight`
- t1 = after `usbhost_*_transfer` returns
- t2 = after `tx_ret_submit` returns

Gate the timing log behind `s_urb_verbose` (do not leave it on by
default). Run the same 64 KiB stream as experiment 1. Tabulate
median (t1-t0), (t2-t1), and total (t2-t0) per URB.

This pins which leg of the per-URB pipeline dominates. Likely
candidates ranked by R19 evidence:

a. (t1-t0) IDF submit + device round trip + IDF callback. For a
   64 B IN URB on a full-speed device this is bounded below by one
   USB micro-frame (1 ms host-side polling on FS) plus IDF callback
   latency. Expect ~1-2 ms.
b. (t2-t1) `tx_mutex` acquisition + TCP send for ~80 bytes (URB
   header + payload). LAN-local TCP: <1 ms typically. If higher,
   something is wrong (Nagle, slow path).

If (a) > 2 ms median for a 64 B URB, the IDF / bus path has overhead
worth chasing (caveat 1 below).

### Experiment 3: measure throughput

Use the script in R20's "Validation" section F (the elapsed-time
read-back). Run pre-R21 baseline first, then re-run after each
proposed change in this plan. Report bytes/sec.

Target: 800 KB/s sustained on a 64 KiB read across the Pico CDC. The
hard ceiling on Pico FS bulk is ~1.2 MB/s; 800 KB/s is roughly 2/3
of that, which matches what well-tuned cdc-acm-over-usbip stacks
achieve on similar hardware. Numbers below 400 KB/s after R20 + R21
indicate a software bottleneck still uncaught.

### Experiment 4: alternate function class (sanity check)

Mass storage (BBB) URBs are sized by the SCSI layer, not by
`cdc-acm`'s heuristic, and routinely run at 16-64 KiB per URB. If
the Pico can present a small MSC LUN (or a USB ethernet, or any
class with bigger URBs), running the throughput probe against that
function class isolates whether the cap is in `cdc-acm` or in the
ESP firmware.

This experiment is optional and only if firmware-side fixes from
experiment 2 don't lift throughput as predicted.

## Implementation paths

Choose based on experiment results. The plan lists them in
likelihood-of-paying-off order.

### Path A: kernel-side cdc-acm URB size bump (most likely)

If experiment 1 confirms 128-byte URBs and experiment 2 confirms
per-URB cost ~3 ms, the bottleneck is on the host kernel and is not a
firmware bug.

**Not in this repo.** Document the Linux `cdc-acm` `readsize`
behaviour in `r21-findings.md` and either:

a. Note it as an external constraint and stop. The 800 KB/s target
   is unreachable without kernel cooperation; with R20 in place we
   accept the cdc-acm-imposed ceiling.
b. Provide a kernel module patch as a separate deliverable
   (out-of-tree `cdc-acm.c` build). If a Sonnet agent goes this
   route the patch should change `acm->readsize` from
   `usb_endpoint_maxp(epread) * 2` to a tunable module parameter
   defaulting to 4096. Build, load, re-run experiment 3.
   Document load command (`sudo modprobe cdc-acm readsize=4096`) in
   the findings file. The patch lives outside `mpy-pod` (probably
   in `tools/cdc-acm-patches/` or similar new directory) and is
   shipped as a reference, not auto-deployed.

Path A is documentation-heavy and expected to deliver the bulk of
the win.

### Path B: firmware-side fast-path for stable IN streams (speculative)

If experiment 2 shows `tx_mutex` contention as a non-trivial fraction
of (t2-t1) under sustained load, reducing the per-URB TCP send size
helps. Today `tx_ret_submit` builds a `malloc`-then-`memcpy`-then-
`write_all` sequence for each URB. Two sub-paths:

b1. Replace `malloc` + `memcpy` + `write_all` for the no-payload
    cases with a single `writev`. The kernel and ESP-IDF lwIP both
    support `writev` for socket fds; this saves one alloc + one
    copy per URB.
b2. Coalesce contiguous URB completions into a single `write_all`
    (a "send queue" inside `tx_mutex` that batches up to 16 ms or
    8 KiB). Adds latency (bounded by the coalesce window) in
    exchange for per-URB syscall reduction. Risky for raw-REPL
    handshake responsiveness; only worth doing if experiment 2
    shows TCP send is the dominant cost.

Path B is firmware work and can be implemented and tested in this
repo. Estimate: ~100 LOC for b1, ~200 LOC for b2.

### Path C: IDF submit/poll loop refinement (speculative)

`submit_xfer` waits for completion in a 50 ms loop:

```c
while (xSemaphoreTake(inflight->done_sem, pdMS_TO_TICKS(50)) != pdTRUE) {
    if (cancel) { halt+flush+clear; }
}
```

The 50 ms tick is a cancel-poll cadence, not a wake latency: the IDF
callback gives `done_sem` immediately when the URB completes. So this
is not the floor for happy-path completion. Unless experiment 2
contradicts this, leave alone.

### Path D: increase Pico-side bulk MPS (out of scope)

Pico runs full-speed (12 Mbps) only. Bulk MPS is hard-capped at 64 B
by the USB 2.0 spec for FS devices. Switching to high-speed (480
Mbps, 512 B bulk MPS) requires a USB host with a HS PHY and a device
with a HS-capable controller. RP2040 is FS-only. Not solvable here.

Document and move on.

## Files touched (path B)

If path B b1 is implemented:

- `src/c_modules/usbip/usbip_server.c`
  (`tx_ret_submit` to use `writev`)

If path B b2 is implemented:

- `src/c_modules/usbip/usbip_server.c` (new send-queue under
  `tx_mutex`, drained by a tx-coalesce task)

No `usbhost.c` changes expected from R21 itself unless experiment 2
surfaces something. No board files, no manifest, no Python.

## Implementation order

1. **Cycle 1: experiments 1, 2, 3 baseline.** Verbose-mode log
   collection, no firmware code changes (only the timing log added in
   experiment 2, which is verbose-gated). Build, flash, capture.
   Commit the timing log if useful long-term, otherwise revert.
2. **Cycle 2: choose path based on data.** Path A (kernel patch +
   document) or path B (firmware writev/coalesce).
3. **Cycle 3-4: implement chosen path.** One commit per logical
   change, smoke + throughput probe between.
4. **Cycle 5: re-measure throughput, write findings.**

## Validation

Same regression matrix as R20 (smoke 5/0, mpremote 30/30, R18
post-detach probe, concurrent attach). Plus:

- Throughput probe (experiment 3) post-R21 must be at least equal to
  post-R20 baseline. The success criterion is improvement, but
  parity is the floor.
- raw-REPL handshake latency: a known-good baseline is "first
  mpremote command after attach completes within 1 s". If path B b2
  introduces a coalesce window above 50 ms it can blow this; measure
  with `time mpremote connect ... resume exec 'pass'` and require
  <1 s.

## Caveats and risks

1. **The dominant cost may be on the wire, not in software.** A 64 B
   bulk-IN URB on full-speed USB takes one micro-frame (1 ms) to
   schedule plus device-side latency to fill. The kernel's polling
   interval and the USB scheduling algorithm determine when the
   device gets the next IN-token. If the kernel only issues 16
   pending URBs and waits for one to retire before issuing the
   17th, throughput is bounded by `16 / (per_URB_round_trip_ms)`,
   which at 1 ms = 16 KB/s for 64 B payloads. The fix is then
   "more URBs in flight" or "bigger URBs", both of which are
   kernel-side.

2. **Linux cdc-acm has a 32 KiB per-write cap.** Read-side may have
   a different cap. The `cdc-acm.c` source for the running kernel
   version should be referenced; do not work from man pages or
   stale stack overflow answers. The implementing agent should
   `grep -nH "readsize\|writesize\|wMaxPacketSize" /lib/modules/$(uname -r)/build/drivers/usb/class/cdc-acm.c`
   or equivalent.

3. **Stress regression.** Larger per-URB sizes increase per-URB
   memory pressure (xfer buffer + inflight buffer). Today's PSRAM
   budget has plenty of headroom (8 MiB free) but two connections
   each running 16 in-flight URBs at 4 KiB = 128 KiB. The
   `usb_host_transfer_alloc` path goes to internal RAM by default;
   verify whether it falls back to PSRAM under pressure or fails. If
   it fails, this is an internal-RAM ceiling not previously tested.

4. **Path A's kernel patch is system-specific.** Building an
   out-of-tree `cdc-acm.ko` for the Linux running on the developer
   workstation is straightforward but not portable across hosts. CI
   running this test should either skip path A or pin a Linux
   kernel version.

5. **mpremote round-trip behaviour is latency-sensitive, not
   throughput-sensitive.** Improving throughput while regressing
   round-trip latency is a net loss. Watch experiment 4 (raw-REPL
   handshake) for this.

6. **Synthetic CMSIS-DAP path is unaffected.** Synthetic devices
   never go through `submit_xfer`; their `data_transfer` callback
   runs in microseconds. None of R21's changes should touch the
   synthetic path. If pyocd reset slows down or breaks, R21 has
   regressed something out-of-scope and should bisect to the
   offending commit.

## Out of scope for R21

- Anything that requires re-flashing the Pico firmware (we do not
  control the Pico's USB descriptors).
- High-speed USB upgrade -> needs different hardware.
- Pure microbenchmarks without a real Linux host attaching ->
  R21 is about real-world streaming, not synthetic loopback numbers.

## Findings file expectations

`test/integration/phase3/r21-findings.md` should be written by the
implementing agent and include:

- experiment 1 results: per-URB size histogram, source attribution
  (cdc-acm formula confirmed or refuted)
- experiment 2 results: timing breakdown (t1-t0, t2-t1) per URB
  median + tail
- experiment 3 results: bytes/sec pre-R21, post-R21, with the
  command and capture script verbatim
- which implementation path was taken (A, B, or "no firmware
  change, documented external constraint") and why
- regression matrix (smoke, 30/30, R18 probe, concurrent attach)
- raw-REPL handshake latency pre and post
- caveats observed not predicted in this plan
- iteration budget used vs. five-cycle ceiling

If path A was taken, include the kernel patch and the modprobe
command in the findings file or in a sibling `r21-cdc-acm-patch/`
directory.
