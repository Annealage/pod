# RP2350 pod: DUT troubleshooting (silent / stuck / 0xff-flooding REPL)

Practical recovery for the most common consumer-facing friction: a DUT forwarded
over the pod's USB/IP appears dead - its REPL is silent, floods `0xff`, or
`mpremote` says "could not enter raw repl". Almost always the DUT and the pod are
both fine; the cause is one of a small set of DUT-state / host-ecosystem issues,
each with a distinguishing symptom. This is written so a person or a Claude
session can get unstuck without a hardware teardown.

Do NOT assume "the USB/IP forward is unreliable" - it is reliable once the DUT is
correctly flashed, not DTR-gated by ModemManager, and not stuck in raw mode. Each
cause below is distinguishable; pick by the symptom rather than guessing.

## Which symptom? (decision tree)

Match the exact symptom to the cause, then jump to its fix:

- REPL is **silent** - input accepted (echoes drain) but no output at all
  -> Cause 2 (ModemManager gated DTR, so stdout is dropped).
- **`mpremote` "could not enter raw repl"** but a raw pyserial read (stripping
  `0xff`) works -> Cause 1 (RAW-mode latch) OR Cause 3 (a `0xff` flood buries the
  banner). Tell them apart: is there a *sustained* `0xff` flood? -> Cause 3.
- **Sustained `0xff` flood** (tens of KB of `0xff`, sometimes with a real prompt
  buried in it) -> Cause 3 (the DUT is transmitting `0xff` - almost always an
  INCOMPLETE FLASH; a hole over rodata prints `0xff`). NOT a pod relay bug.
- **`0xff` flood then goes quiet** on a fresh attach, after prior attach/detach
  churn -> Cause 4 (a stale usbip export slot); clears with `usbip.stop/start`.
- Input **drains from stdin but never executes** (no echo, no result) -> Cause 1
  (RAW-mode latch).

The short version: run `recover_dut_repl` first (Cause 1); if it floods `0xff`,
suspect an incomplete flash (Cause 3) or a stale slot (Cause 4); if it is silent,
install the ModemManager udev rule (Cause 2); escalate to reset / power-cycle only
if those do not fix it.

## Symptom: forwarded DUT REPL is silent, floods 0xff, or won't enter raw REPL

You attached the DUT (`attach_dut` -> a `/dev/ttyACM*`), but:
- opening the tty and sending commands gets no response, no echo, no `>>>`; or
- `mpremote` / a raw-REPL client reports "could not enter raw repl"; or
- a fresh attach floods `0xff` (sustained, or then goes quiet).

### Cause 1 - the DUT REPL is latched in RAW mode

A failed raw-REPL handshake (an `mpremote` Ctrl-A whose "raw REPL" banner never
came back - see cause 2) leaves the DUT in RAW mode. RAW mode executes on Ctrl-D,
so ordinary CR-terminated lines never run and nothing echoes - it looks dead.

Fix (non-destructive, build-agnostic):

    pod recover-dut <label> /dev/ttyACM0        # or the recover_dut_repl MCP tool

It sends Ctrl-C (break any running program) then Ctrl-B (leave RAW for the
friendly REPL) over the tty, holds DTR asserted so the reply isn't gated, and
reports whether the friendly `>>>` came back (`recovered`). This is the FIRST
thing to try - cheaper and less disruptive than a reset.

### Cause 2 - ModemManager toggled DTR off (the DUT's stdout is gated)

ModemManager (running by default on most Linux desktops) probes every new
CDC-ACM tty: it opens the port, sends AT commands, toggles DTR/RTS, then drops
it - deasserting DTR. MicroPython gates its REPL stdout on `tud_cdc_connected()`,
which is the CDC line-state DTR bit, so with DTR low the DUT executes your input
but silently drops all output. Intermittent, and behind your back: you think you
are holding DTR while MM knocks it to 0. This is the single most common
embedded-USB-CDC gotcha.

Fix (durable): tell ModemManager to ignore pod-forwarded DUTs.

    sudo pod install-udev          # writes /etc/udev/rules.d/99-annealage-pod.rules
    # then RE-ATTACH the DUT so the rule applies at enumeration

`pod install-udev` installs a udev rule that sets `ID_MM_DEVICE_IGNORE=1` for
devices imported over USB/IP (matched under the `vhci_hcd` virtual host
controller), so only pod-forwarded DUTs are affected, not your real USB devices.
Add `--vid f055` (repeatable) for a belt-and-braces per-idVendor match if your
setup needs it - `f055` is MicroPython's VID. `pod install-udev --print` shows
the rule without installing it. It must apply AT enumeration: a rule loaded while
the device is already probed does not help, so re-attach afterwards.

Manual alternatives if you can't install the rule: `sudo systemctl stop
ModemManager` (blunt, affects real modems too), or check who holds the port with
`fuser /dev/ttyACM0`. Note pyserial deasserts DTR on close, so hold the port with
a long-lived opener (`mpremote`) rather than repeated open/close cycles.

### Cause 3 - a sustained 0xff flood: the DUT firmware is incompletely flashed

If the forwarded REPL floods a *sustained* run of `0xff` (tens of KB, sometimes
with a real prompt fragment buried in it) and `mpremote` can't match the raw-REPL
banner, the DUT is genuinely transmitting those `0xff` bytes - it is NOT a pod
relay bug (the pod faithfully forwards what the DUT sends). The usual cause is an
INCOMPLETE FLASH: a chunk dropped mid-flash left a hole of erased `0xff` flash,
and if that hole lands on a rodata string the firmware prints (e.g. the raw-REPL
banner), `strlen` runs over the `0xff` and emits a long `0xff` run.

Fix: re-flash a complete image and verify it.

    pod flash <label> firmware.bin --addr 0 --mass-erase   # or flash_dut MCP tool

`flash_dut` now does an end-to-end read-back verify (CRC of each flashed region
vs the source) and fails loudly on a hole, so a fresh flash cannot leave a silent
gap. An image flashed *before* that verify existed can still carry one - re-flash
with `mass_erase`. To confirm a suspected hole directly, CRC-map the region over
SWD (`ops.flash_crc(addr, len)`): an all-`0xff` region returns the CRC of `0xff`
bytes, which a real image never matches. See `debug-stack.md` (flash) and the
`pod-usbip-0xff-flood-dut-side` auto-memory for the worked example.

(A second, firmware-side source of a real `0xff` flood is the nRF USBD re-clocking
a stale EPIN EasyDMA buffer when its CDC tx_ff is empty - a DUT tinyusb/driver
bug, not the pod. Same memory covers how to tell it apart from a flash hole with
an SWD read of tx_ff + the EPIN buffer.)

### Cause 4 - a 0xff flood that then goes quiet: a stale usbip export slot

After repeated attach/detach churn (or a DUT re-enumeration), the pod's usbip
export slot can go stale and flood ~30KB of `0xff` on a fresh attach before
settling. Distinct from Cause 3 (that flood is sustained and is real DUT data);
this one is the pod's stale slot and clears with a server restart:

    pod exec <label> "import usbip; usbip.stop(); usbip.start()"

Then re-attach. See the `pod-usbip-stale-slot-reenum` auto-memory.

## Recovery escalation ladder

Try these in order; stop at the first that works.

1. `recover_dut_repl` (`pod recover-dut`) - Ctrl-C + Ctrl-B over the tty. Clears a
   stuck RAW mode and a running/looping program. Non-destructive.
2. Install the ModemManager udev rule (`pod install-udev`) + re-attach, if the
   REPL is silent rather than mode-stuck (output produced but gated by DTR=0).
   If instead it floods `0xff`: `usbip.stop/start` for a stale slot (Cause 4),
   or re-flash with `mass_erase` for an incomplete flash (Cause 3).
3. `reset_dut` (`pod reset`) - a SWD system reset re-inits the DUT core AND its
   peripherals (incl. USB), so it comes back with a fresh FRIENDLY REPL and
   re-enumerates cleanly. No physical replug needed. Use when 1+2 don't recover it
   or the firmware itself is wedged.
4. Physical power-cycle - only if `reset_dut` reports an error (e.g. SWD not
   connected) or the flash is XIP-wedged. On this bench a host-side cold boot is
   `mpy-dev cycle pico-probe` (cycles the pod's hub port, which power-cycles the
   pod-powered DUT too).

## Confirming which layer is at fault (SWD oracle, for maintainers)

The pod can SWD-read the DUT to pinpoint the cause without guessing (needs the
on-pod debug stack; for these one-shot forensic reads pass `clkdiv>=32` for the
widest sampling margin - the default `clkdiv=16` is spec-compliant, but a slower
clock further cuts the single-bit-flip risk this rig shows at over-spec clocks):
- `pyexec_mode_kind` (0 = FRIENDLY, 1 = RAW; address is build-specific, from the
  DUT ELF) - is the REPL mode-stuck?
- the tinyusb `_cdcd_itf[0].line_state` bit0 - is DTR actually asserted at the DUT
  right now, or did something knock it to 0?
- the mp `stdin_ringbuf` iget/iput - did the input reach and get consumed by the
  REPL (RX path healthy)?
- the CDC `tx_ff` buffer - pristine means nothing was transmitted (output dropped
  or never produced).
- an SWD-observable command (e.g. a GPIO `OUTSET` then read the latch back) proves
  execute-vs-not independent of the CDC path.
- `ops.flash_crc(addr, len)` - CRC a flash region; an all-`0xff` region (a flash
  hole, Cause 3) returns the CRC of `0xff` bytes, which a real image never matches.
  Map the region to find a hole; re-flash with `mass_erase` to fill it.
- for a real `0xff` flood that is NOT a flash hole, read the DUT's CDC `tx_ff`
  wr/rd (empty while it floods = the nRF EPIN re-clocking a stale buffer) and, to
  find what fills it, a DWT data-write watchpoint on the nRF `EPIN.MAXCNT` /
  an FPB breakpoint on `mp_hal_stdout_tx_str`. This is deep firmware-side work.

Prove pod-vs-DUT before concluding: the pod's USB relay has been exonerated
repeatedly (DTR forwarding, the host-controller completion path). A `0xff` flood
is real DUT-transmitted data (a flash hole or the EPIN phantom), not a pod relay
bug - the poison-the-host-DPRAM test in `pod-usbip-0xff-flood-dut-side` proves it.

Worked examples and the full SWD recipes live in the auto-memories:
`pod-usbip-dtr-drop` (DTR gating), `pod-usbip-0xff-flood-dut-side` (the `0xff`
flood, flash-hole and EPIN), `pod-usbip-stale-slot-reenum` (Cause 4). For most
consumers Causes 1-2 are handled by steps 1-2 of the ladder and Cause 3 by a
re-flash; the SWD reads are only needed when diagnosing a new failure shape.
