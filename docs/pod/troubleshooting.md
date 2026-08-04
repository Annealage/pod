# RP2350 pod: DUT troubleshooting (silent / stuck REPL)

Practical recovery for the most common consumer-facing friction: a DUT forwarded
over the pod's USB/IP appears dead - its REPL is silent, or `mpremote` says
"could not enter raw repl". Almost always the DUT and the pod are both fine; the
cause is one (or both) of two ecosystem issues on the host side. This is written
so a person or a Claude session can get unstuck without a hardware teardown.

The short version: run `recover_dut_repl` first, install the ModemManager udev
rule once, and only escalate to a reset / power-cycle if those do not fix it.

## Symptom: forwarded DUT REPL is silent or won't enter raw REPL

You attached the DUT (`attach_dut` -> a `/dev/ttyACM*`), but:
- opening the tty and sending commands gets no response, no echo, no `>>>`; or
- `mpremote` / a raw-REPL client reports "could not enter raw repl"; or
- a fresh attach floods `0xff` then goes quiet.

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

## Recovery escalation ladder

Try these in order; stop at the first that works.

1. `recover_dut_repl` (`pod recover-dut`) - Ctrl-C + Ctrl-B over the tty. Clears a
   stuck RAW mode and a running/looping program. Non-destructive.
2. Install the ModemManager udev rule (`pod install-udev`) + re-attach, if the
   REPL is silent rather than mode-stuck (output produced but gated by DTR=0).
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
on-pod debug stack; read at `clkdiv>=32`, never the default 8 - clkdiv=8 flips
single bits on this rig):
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

Full diagnostic recipe and the worked example behind this doc: the
`pod-usbip-dtr-drop` auto-memory. The relevant point for consumers is that both
causes above are handled by steps 1-2; the SWD reads are only needed when
diagnosing a new failure shape.
