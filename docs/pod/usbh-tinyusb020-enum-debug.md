# Investigation: USB-host enumeration regression on TinyUSB 0.20 (pod RP2350)

Status: RESOLVED 2026-06-22 (tasks #56 enum root-cause, #57 on-demand redesign).
Fix = an ON-DEMAND windowed poll of tuh_task in shared/tinyusb/mp_usbh.c
(machine-usbhost `2371080bad`), composed into tessera `a449c54815` (lib/tinyusb
`b414cc7d8`). A bounded ~1.5 s poll window (20 ms cadence) opens on each device
attach/remove edge (and on host-active) and self-lapses once enumeration
settles, so a forwarding device is serviced purely by the HCD interrupt path
with zero idle polling. Hardware-validated on the pod (instrumented build,
then stripped + reflashed clean): window ARM->DISARM with ~83 fires/window then
silent in steady idle; the wrap-handler HCD attach edge fires (att 0->1);
`pod attach` enumerates + forwards (`dut 42` / `committed 42`), Wi-Fi 0% loss.
The three micropython fork branches (machine-usbhost, tinyusb-rp2-host-abort,
tessera) are pushed to andrewleech/micropython through serve-review; the
superproject pins `src/micropython -> a449c54815`.

The first fix (task #56) was a *permanent* 20 ms periodic pump; #57 replaced it
with the on-demand window after review feedback that a 50 Hz forever-tick is
overkill for episodic attach/detach servicing. The findings below are from #56.

## Problem

The pod (RP2350, native USB host forwarding the DUT over USB/IP) does not
enumerate the DUT when built against TinyUSB 0.20 (`b414cc7d8`, the PR #3702
`sie_stop_xfer` abort/close on the 0.20 base). On the validated 0.19 base
(`c047f54f5`) the same pod + DUT enumerate fine. On 0.20 the firmware builds,
boots, and Wi-Fi/REPL/SWD all work; only USB-host enumeration fails (empty
usbip export, `bus_reset` no help).

## What is known / ruled out

- DUT is alive and powered (SWD identity reads OK), host is `active`, same
  hardware that enumerates on 0.19. Not a dead DUT / inactive host / cabling.
- TinyUSB 0.20 made host enumeration a deferred, timer-driven state machine
  (`usbh_defer_func_ms_async` + `enum_delay_async`, introduced in tinyusb
  `0daa444a9`); the upstream contract is "call `tuh_task` periodically"
  (docs/reference/getting_started.rst; every host example loops on tuh_task).
- Added a periodic soft-timer pump of `tuh_task` to `shared/tinyusb/mp_usbh.c`
  (machine-usbhost `1bcd3bda9f`): static soft timer (5 ms, PendSV-backed)
  schedules the existing `usbh_task_node` while the host is active, mirroring
  the rp2 BLE-HCI / lwIP services. It builds, is Wi-Fi-safe (0% ping loss), but
  did NOT fix enumeration - even after an explicit `active(False/True)` cycle
  that definitely arms the timer. So the periodic pump is necessary but NOT
  sufficient; there is a second cause.

## Candidate causes (to disambiguate with instrumentation)

1. The pump is not actually firing on the pod (soft-timer impl / lifecycle).
2. The pump fires, but enumeration stalls in the 0.20 EPSTATE/`epx` control
   transfer path (the analysis-workflow H2, ranked ~8%).
3. An already-connected device (DUT attached before host init) is not detected
   as a connect event on 0.20 init, so enumeration never starts.

## Method

Iterate on the working tree (tessera @ 0.20+pump) - no mbm recompose per
build. Capture the pod's stdout over the GP0/GP1 backup UART:
`/dev/serial/by-id/usb-FTDI_TTL232RG-VREG1V8_FT55TKQB-if00-port0`.

Probe A (this round): `mp_printf` pump counter + `tuh_mounted()` in
`mp_usbh_task` (guaranteed routing via dupterm). Distinguishes:
- pump count not climbing  -> cause 1 (pump not firing)
- climbing, never mounts    -> cause 2/3 (enum stalls) -> add usbh.c trace
- mounts, export still empty -> usbhost_rp2.c export-seeding, not enum

## Findings log

### 2026-06-22: ROOT CAUSE + FIX (confirmed on hardware)

Instrumented `mp_usbh_task` (call counter), the soft-timer callback (fire
counter), and `start_task_timer` (prints live init/active + counters), captured
over the socket REPL. The first build (original `ONE_SHOT` + manual re-arm in
the callback): two `[usbh] arm:` snapshots ~2s apart showed `tmr 0 -> 1` (timer
fired ONCE in 2s) and the DUT never mounted - the pump never actually ran.

ROOT CAUSE (mine, not tinyusb): `soft_timer_handler` (shared/runtime/softtimer.c:85)
pops the expired entry into a LOCAL `heap`, calls the callback, and for a
`ONE_SHOT` entry does not re-insert; then writes `soft_timer_heap = heap`
(line 105), CLOBBERING any re-insert the callback did via `soft_timer_insert`.
So re-arming a soft timer from inside its own callback is silently discarded -
it fires exactly once. (BLE-HCI avoids this by re-arming from scheduler-context
work, not the callback.)

FIX: `SOFT_TIMER_MODE_PERIODIC` - the handler re-inserts the entry itself
(softtimer.c:100-102), preserved by the line-105 write-back. Drop the manual
`soft_timer_insert` from the callback. Same mechanism lwIP uses.

CONFIRMED (PERIODIC build): arm snapshots `tmr 0 -> 400, pump 0 -> 441,
mnt1 0 -> 1` over 2s; `pod attach` -> Attached f055:9802; REPL round-trip over
the USB/IP forward returned `dut 42`; detach exercised the 0.20 `sie_stop_xfer`
abort, pod stayed alive; Wi-Fi 33/33 0% loss (avg 4.5 ms) with the pump at
~200/s and the REPL responsive. So H1 (periodic pump) was the correct cause;
the first attempt just had the re-arm bug.

Secondary: the pod leaves `machine.USBHost.active=0` when idle; boot.py's
start_usbip wrapper calls `active(True)` before `usbip.start()` (boot.py:152),
so the normal `pod attach` flow arms the pump via the `active()` hook.

### 2026-06-22: FINALISED (PERIODIC, 20 ms, recomposed clean)

Debug instrumentation stripped; machine-usbhost pump switched to PERIODIC and
committed (`e5b72a0321`). Per review feedback the cadence was coarsened 5 ms ->
20 ms: the interrupt path (mp_usbh_schedule_task from __wrap_hcd_event_handler)
already pumps on every USB event, so the timer is only a fallback for 0.20's
deferred enum delays (150 ms debounce + 2-50 ms reset/recovery) and a coarse
20 ms cadence keeps idle overhead low. Re-validated at 20 ms standalone:
`*** ATTACH OK @20ms ***`, Attached f055:9802, round-trip `dut 42`, detach
exercised the 0.20 abort, Wi-Fi 0% loss.

Recomposed tessera clean (`1d0f62b08e`): tree-diff vs the pre-0.20 tessera is
exactly the 5 intended files (machine_usb_device.c, machine_usb_host.c,
lib/tinyusb gitlink, mp_usbh.c, mp_usbh.h), firmware builds 0 warnings.

Remaining (gated, user's call): push machine-usbhost / tinyusb-rp2-host-abort /
tessera through serve-review, re-pin the superproject, commit this doc, push
`rp2350-pivot`.

---

(older entries / plan below)

