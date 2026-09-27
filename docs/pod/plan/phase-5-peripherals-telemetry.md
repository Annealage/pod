# Phase 5: Peripherals, reset, API

Workstream F. The remaining DUT-facing capabilities, several reused from the
ESP32-S3 variant.

Goal: PIO I2C/SPI-target personalities, DUT UART over TCP, the `swd`/`nrst` reset
paths, and the RP_INFRA-compatible MP API on the bare Pico 2 W. INA228 telemetry
and the `power` reset path are gated on a future custom carrier.

## Dependencies

- F1 (transport); shares code with the S3 variant where possible.

## Tasks

### F5.1 PIO I2C/SPI target personalities
- Implement I2C-target and SPI-target responders on PIO with the S3 slaveio
  register-table model (`v1.6.0-native-flash-default:docs/esp32-s3/design/slaveio.md`): two flat buffers per
  personality (read-table / write-table), ISR-free PIO path, MP-side read/write
  and on-write-range callbacks. PIO target is the reason the RP2350 is expected to
  be more reliable than the S3 i2c-target.
- I2C address / SPI mode / freq cap configurable from MP; personalities mutually
  exclusive on shared pins.
- Status and GA scope (2026-07): the I2C-target half landed on the hardware
  I2C1 peripheral (`machine.I2CTarget`) rather than PIO - a PIO I2C slave does
  not fit a 32-instruction block (`docs/pod/peripherals.md`) - and is
  hardware-validated against an nRF52840 controller. The SPI target is not
  implemented and is marked cuttable for GA: I2C target + GPIO + ADC is the
  peripherals set GA ships with, and the SPI target moves to the post-GA
  backlog unless a design partner needs it earlier. If cut, the SPI clause
  drops from this phase's exit gate.
- Consumer signal (2026-07): the `aio` asyncio-hardware roadmap (a pod HIL
  consumer) has asked for the PIO SPI target to validate coherent SPI peripheral
  framing and, specifically, the >65535-byte multi-chunk continuation path (a
  PIO SPI responder that captures received bytes and returns a known counter
  pattern). That is exactly the "design partner needs it earlier" trigger the cut
  clause named, so the SPI target is flagged PROMOTABLE from the post-GA backlog.
  Promotion is owner-gated on the GA family-scope decision, not yet scheduled.

### F5.2 UART bridge over TCP
- DUT UART forwarded over a TCP socket (default port per mDNS TXT). Hardware UART
  or PIO UART depending on the Phase 1 pin/PIO allocation. Configurable
  baud/parity/bits.
- Current state (2026-07-01): NOT yet implemented on RP2350 - there is no pod
  TCP listener on the advertised port. The mDNS TXT already advertises
  `uart-port=2000` (netboot `_advertise_mdns`), so the broadcast currently
  promises a port nothing binds; the implementation plan below either binds the
  port or stops advertising the key until it is bound. The ESP32-S3 `uartbridge`
  C module does not port (IDF UART driver + FreeRTOS task + BSD sockets); the
  RP2350 path is a new pure-MP bridge - `machine.UART` on the DUT-UART pins,
  pumped to a TCP listener from inside the existing single-core asyncio runtime.
- The full implementation plan is in the section below.

## F5.2 implementation plan: DUT UART-over-TCP bridge

This section is the concrete plan for the RP2350 UART bridge. It is a plan, not
the code. It supersedes the bullet sketch above.

### Why a cooperative asyncio task, not an ops streaming server

The pod runs ONE asyncio event loop on core0 that owns the whole management
plane (`src/boards/ANNEALAGE_POD_RP2350/netboot.py`). That single-mutator
arrangement is load-bearing: a second core also mutating lwIP let an SWD halt
freeze a `pendsv_mutex` hold into a permanent cross-core spin, after which
`cyw43_poll` never drained RX (the CYW43 inbound-death deadlock). The fix was to
collapse the pod to one lwIP mutator on core0. Any new network surface MUST stay
on that one loop.

The flash/dump/LA streaming servers in `annealage_pod.debug.ops`
(`flash_stream`, `dump_stream`, `la_stream`) bind, accept, serve one client,
then return; they block the REPL for the duration. That is acceptable for them
because they are short-lived, agent-invoked, and run on the same core0 stdin the
loop already serves. The UART bridge is different: it is a long-lived listener
that must be bound from boot (so the advertised `uart-port` is real) and must
coexist with the REPL accept loop and the Wi-Fi supervisor. It therefore CANNOT
be a blocking `ops` server. It is a background asyncio task created in
`netboot.main()`, alongside `_wifi_supervisor` and `_repl_accept`, polled
cooperatively so it is never a second mutator and never blocks the loop.

### On-pod design

A new module `annealage_pod.uart_bridge` (pure MP) provides the coroutine; the
board's `netboot.py` owns its lifecycle. The bridge:

- Opens `machine.UART(_UART_NUM, baudrate=..., tx=Pin(TX), rx=Pin(RX))` once at
  start, sized RX/TX FIFOs left at the port default. UART config (num, tx, rx,
  baud, bits, parity, stop) comes from `_rp2_pinmap` constants with `config.py`
  overrides (see "DUT UART pin choice").
- Binds a dual-stack listener exactly like the REPL accept loop:
  `socket.socket(socket.AF_INET6)`, `SO_REUSEADDR`, `bind(("::", port))`,
  `listen(1)`, `setblocking(False)`. This is the `modlwip` IPv6-ANY listener
  promoted to dual v4+v6, so the host reaches it over the pod's stable IPv6
  address (same as `dbgsrv.serve` and `_repl_accept`).
- Serves ONE client at a time. A second connection is refused with a one-line
  notice and closed without evicting the live session, mirroring the
  `_repl_accept` BUSY behaviour, so an agent dialling the wrong pod cannot knock
  an active UART consumer off.
- Runs a single cooperative poll cycle, NEVER a blocking `accept`/`recv`. Each
  iteration:
  1. If no client: non-blocking `accept` via a `select.poll` registered on the
     listener (poll timeout 0), exactly the `netutil.accept` slice pattern but
     inlined into the asyncio task so it yields with `await asyncio.sleep_ms`
     between polls instead of spinning.
  2. If a client is attached: drain UART->TCP and TCP->UART without blocking.
     `n = uart.any()`; if `n`, `uart.read(n)` and `client.send(...)` (handle a
     partial/short send by retaining the unsent tail for the next cycle). Poll
     the client socket for readability; if readable, `client.recv(...)` and
     `uart.write(...)`. A client poll returning HUP/ERR/NVAL (the
     `netboot._POLL_DEAD` mask) means the peer is gone: close and clear the
     client.
  3. `await asyncio.sleep_ms(_UART_POLL_MS)`.
- Chooses a poll cadence fast enough for an interactive console but idle-cheap.
  Start at ~5 ms while a client is attached (so a 115200 console, ~11.5 KB/s,
  loses no bytes given the UART RX FIFO depth and the per-cycle `uart.read(n)`
  of everything pending), backing off to the REPL accept cadence (~150 ms) when
  idle. The exact value is tuned against observed RX-FIFO overrun in validation;
  the UART FIFO plus per-cycle full drain is the buffer, not a Python-side ring.
- Is fully exception-guarded per cycle (same discipline as `_wifi_supervisor` /
  `_repl_accept`): any transport error closes the current client and continues;
  the task never exits, so the pod's listener never silently disappears. It does
  not touch `os.dupterm` and shares no state with the REPL, so it cannot disturb
  the management channel.

Lifecycle: `netboot.main()` creates the task with
`asyncio.create_task(uart_bridge.serve(port, uart_cfg))` only when a DUT UART is
configured (always, on the bare Pico 2 W default) and BEFORE it advertises mDNS,
so the advertise can be gated on the bind succeeding (see "mDNS gating").

### DUT UART pin choice

The bridge defaults to `machine.UART(1)` on GP4 (TX) / GP5 (RX), reconciled
against the code and the hardware doc as follows:

- `docs/pod/hardware-setup.md` marks GP4 DUT-UART-TX / GP5 DUT-UART-RX as
  UART1, SUGGESTED (untested) - the pinout diagram rows and the UART-bridge
  wiring row.
- These are the RP2 port's default UART1 pins, so `machine.UART(1)` binds them
  with no explicit pin override, but the bridge passes `tx`/`rx` explicitly
  anyway so the assignment is pinned in one place.
- No collision exists:
  - UART0 / GP0 / GP1 is the backup REPL console (`MICROPY_HW_ENABLE_UART_REPL`,
    `mpconfigboard.h`); GP0/GP1 are reserved and the DUT UART is a DIFFERENT
    instance (UART1) on DIFFERENT pins (GP4/GP5). The two UART REPL paths and the
    DUT bridge do not share a controller.
  - SWD is GP14/GP15 (PIO1), nRST GP13, local I2C GP10/GP11, the LA default
    block GP16-GP21. GP4/GP5 sit clear of all of them.
- `_rp2_pinmap.py` does NOT yet declare the DUT UART (it lists SWD, NRST, and the
  I2C target only). This plan adds `DUT_UART_NUM = 1`, `DUT_UART_TX = 4`,
  `DUT_UART_RX = 5` to `_rp2_pinmap.py` as the single source of truth, and adds a
  `"dut_uart"` entry to `pinmap()` so `pod pins` reports it. The bridge and any
  future on-pod UART helper import these constants rather than hard-coding GPIO
  numbers (the established `_rp2_pinmap` discipline). The ESP32-S3 `_pinmap.py`
  values (`DUT_UART_NUM = 2`, TX 17 / RX 18) are the ESP32-S3 carrier map and do
  not apply here.

Because GP4/GP5 are SUGGESTED-untested, the assignment is an open decision until
hardware-validated (see "Open decisions"); the constants make a later change a
one-line edit.

### mDNS gating

The advertise must never promise a dangling port. `netboot._advertise_mdns`
currently hardcodes `"uart-port": "2000"` unconditionally. Change it so the
`uart-port` TXT key is present only when the UART listener is actually bound:

- `uart_bridge.serve` reports its bind result. Have `netboot.main()` bind the
  listener (or learn the bind outcome) before the first `_advertise_mdns` call,
  and pass the bound UART port (or `None`) into `_advertise_mdns`.
- In `_advertise_mdns`, include `"uart-port"` in the TXT dict only when a bound
  port was passed; omit the key entirely otherwise. The same gating applies on
  every re-advertise the Wi-Fi supervisor performs on reconnect.
- Net effect: a build/config with no DUT UART, or a bind failure, advertises a
  pod with NO `uart-port` key, and the host `pod discover` simply shows no UART
  port for it - never a port that refuses connections. This removes the current
  "promises a port nothing binds" defect.

Bind ordering: bind the UART listener synchronously in `main()` (or have the
task signal "bound" via an `asyncio.Event` / a shared flag the supervisor reads
before advertising) so the first advertise reflects the real state rather than
racing the task's first poll.

### Host side

Mirror the `la_stream` wiring end to end. The streaming model differs from LA in
one respect: LA is a fixed-size capture (header + N words, then EOF); UART is an
open-ended bidirectional stream. The host `uart` path therefore runs until the
user interrupts it or a duration elapses, not until a fixed byte count arrives.

1. `Pod.uart_stream` in `src/host/pod/client.py` (replacing the
   `NotImplementedError` stub): connect a TCP socket to the resolved
   IPv6-first endpoint on the pod's UART port (`self._resolver.endpoint(port)`,
   `port` defaulting to the registry `uart_port` or 2000), then bridge it to the
   host. Two modes, like a serial console:
   - read-only "tail": stream pod->host bytes to stdout (or a callback / a file),
     until a duration or a KeyboardInterrupt;
   - interactive: also forward host stdin to the socket (full duplex).
   Use a short socket timeout and a select/poll loop so Ctrl-C is responsive,
   matching the bounded-wait discipline used elsewhere in the client. The method
   takes `port`, an optional `duration`, an optional `on_output` callback, and a
   `tx`/interactive flag; it does not go through the REPL exec path (the bridge
   is already listening), it is a direct socket like `read_dut`/`logic_analyse`'s
   data socket.
2. `pod uart` CLI verb in `src/host/pod/cli.py`: add `cmd_uart(args)` and an
   `add_parser("uart", ...)` block mirroring `cmd_la` / the `la` parser, and add
   `"uart": cmd_uart` to the handler dispatch dict. Flags: `label` (positional),
   `--port` (default from the registry entry's `uart_port`, else 2000),
   `--duration` (seconds; default: run until Ctrl-C), `--tx/--interactive`
   (forward stdin), `--out` (write the stream to a file instead of stdout).
   Resolve the pod with `Pod.from_entry(_require_pod(args.label))` exactly as
   `cmd_la` does.
3. MCP tool in `src/host/pod/mcp_server.py`: add `handle_tail_uart(label, port,
   duration, ...)` next to `handle_logic_analyse`, a `Tool(name="tail_uart", ...)`
   entry in `list_tools` with the same input-schema shape as `logic_analyse`
   (label required; port/duration optional with defaults), and an
   `elif name == "tail_uart":` branch in `call_tool` dispatching via
   `asyncio.to_thread`. The MCP tool is read-only tail with a bounded duration
   (an agent cannot hold an open interactive stream); the interactive/`--tx`
   direction is CLI-only. Name it `tail_uart` to match the existing plan text and
   the `uart_stream` docstring intent.
4. Registry: `uart_port` is already a registered field (`pod register
   --uart-port`, shown by `pod info` / `pod discover`); the new verbs read it as
   the default port and fall back to 2000.

### Framing details

- Raw passthrough, byte-for-byte, no protocol framing on the wire. The TCP
  payload IS the UART byte stream in both directions. This matches the ESP32-S3
  bridge's default (raw, telnet off) and keeps the host side a plain socket.
- No RFC2217 / telnet option negotiation in this phase. Baud/parity/bits are set
  when the bridge opens the UART and are not renegotiable mid-session by the
  client; a baud change is a config change plus a bridge restart (or a future
  control verb), not in-band. This is a deliberate scope cut from the ESP32-S3
  `uartbridge` telnet mode.
- Baud configuration: default 115200-8N1, taken from `_rp2_pinmap` /
  `config.py`. The UART is opened with `bits`, `parity`, `stop` from the same
  config. Changing them requires re-opening the UART (restart the task or a
  `config.py` edit + reboot); there is no per-connection negotiation.
- Duplex: full duplex. The bridge pumps UART->TCP and TCP->UART independently
  each poll cycle. The host read-only tail simply never sends; the interactive
  CLI mode and any TCP->UART write drive the DUT UART RX line. The pod does no
  echo and no line discipline - it is a transparent pipe; any echo is the DUT's.
- Flow control: none by default (no CTS/RTS), matching the suggested 3-wire
  TX/RX/GND wiring in `hardware-setup.md`. Hardware flow control is out of scope
  unless a validated wiring adds the lines.

### Hardware validation steps

1. Wire pod GP4 (TX) -> DUT UART RX and pod GP5 (RX) -> DUT UART TX, common GND
   (crossed), per `hardware-setup.md`. A loopback test (jumper GP4<->GP5) first
   confirms the pod path with no DUT.
2. Loopback: with GP4 jumpered to GP5, `pod uart <label> --tx`, type a line,
   confirm it echoes back over the same socket (proves UART open + bridge pump +
   both directions) without touching SWD/Wi-Fi.
3. Confirm `uart-port` is advertised only when bound: `pod discover` shows
   `uart=2000` for the pod; temporarily disable the UART in config and confirm
   the key disappears from the advertise (no dangling port).
4. Real DUT: run a DUT firmware that prints to its UART at 115200; `pod uart
   <label>` (tail) shows the DUT output live; interactive mode delivers
   keystrokes to a DUT REPL on that UART.
5. Coexistence (the load-bearing check): with a UART client streaming, exercise
   an SWD op (`pod halt` / `pod gdb`) and a USB/IP forward concurrently, and
   confirm Wi-Fi RX does not wedge - i.e. the bridge has not reintroduced a
   second lwIP mutator. Hold UART + REPL + SWD + Wi-Fi at once (the same
   all-at-once check the LA used) and confirm the REPL stays responsive.
6. Disconnect handling: kill the host client mid-stream and confirm the pod
   bridge detects the dead socket, closes it, and accepts a fresh connection
   without a reboot; confirm a second simultaneous client is refused with the
   BUSY notice, not allowed to evict.
7. Throughput/overrun: stream sustained DUT output and confirm no dropped bytes
   at the chosen poll cadence; tighten `_UART_POLL_MS` if RX-FIFO overrun
   appears.

### Validation status (2026-07-04)

Bench-validated on the flashed pod (firmware built from this branch with the
netboot lifecycle + mDNS gating frozen in; `annealage_pod.uart_bridge` deployed
to the pod filesystem):

- The module imports on MicroPython, `bind()` binds the dual-stack listener, and
  `machine.UART(1)` opens on GP4/GP5.
- `netboot.main()` auto-starts the bridge at boot and gates the mDNS `uart-port`
  key on the bind: `_annealage-pod._tcp` advertises `uart-port=2000` because the
  bind succeeded (validation step 3).
- Disconnect handling (step 6): a host client that closes gracefully is cleared
  and a fresh connection is accepted (3/3 sequential reconnects); a concurrent
  second client is refused with the BUSY notice and the first is not evicted.
  This exercised a fix for a graceful-close leak found on hardware: a peer FIN
  polls readable with `recv() == b""` (not HUP/ERR), so an empty read must clear
  the session, otherwise every client after the first is refused BUSY forever.
- `pod uart <label>` connects and exits cleanly back to back; the bridge coexists
  with Wi-Fi and the socket REPL (the pod stayed reachable throughout).

DEFERRED - the UART byte round-trip itself (steps 1, 2, 4, 7) is NOT validated:
it needs a physical GP4<->GP5 loopback jumper, or a DUT UART wired to GP4/GP5
(both marked SUGGESTED-untested in `hardware-setup.md`), neither present on the
bench. To close it when the wiring exists: run the loopback echo (step 2), then
a real DUT at 115200 for the live-tail + throughput checks (steps 4, 7), and the
full all-at-once coexistence stress (step 5: UART stream + SWD op + USB/IP
forward + REPL held together, confirming Wi-Fi RX does not wedge).

### Open decisions

- DUT UART pin assignment GP4/GP5 (UART1) is SUGGESTED-untested in
  `hardware-setup.md`; confirm against the final DUT wiring and the carrier
  before treating `_rp2_pinmap.DUT_UART_*` as VERIFIED. The constants localise
  any change.
- Hardware UART vs PIO UART: this plan uses the hardware `machine.UART(1)` on
  GP4/GP5. A PIO UART would be needed only if GP4/GP5 are reassigned to pins
  without a hardware-UART function, or if a second DUT UART is wanted; PIO0 is
  free for it but PIO2 (CYW43 Wi-Fi) must never be touched (`pio_arbiter`). Keep
  PIO UART as a fallback, not the default.
- Interactive write authority (resolved 2026-07, consumer-requested): add a
  `uart_send` MCP tool that drives the DUT UART RX, alongside the read-only
  `tail_uart` tail. The `aio` asyncio-hardware roadmap needs to drive DUT RX with
  controlled framing for its UART-parity tests (esp8266 TX, zephyr). The CLI
  already forwards stdin via `pod uart --tx`; this promotes the TX direction to
  MCP. `machine.UART` sendbreak and baud-mismatch framing errors come cheaply;
  controlled overrun / parity-error injection is a secondary sub-feature, staged
  after the basic TX tool.
- Baud/line-setting control surface: whether to add a `pod uart-config` verb /
  on-pod control to change baud/parity without a reboot, versus keeping it a
  `config.py` + restart operation.

### F5.3 Power telemetry (INA228) - custom carrier, deferred
- Not present on the bare Pico 2 W. When a custom carrier exists, reuse the S3
  INA228 driver (I2C, portable): per-rail current/voltage for VTARGET and DUT-USB
  VBUS; vbus-present detection. MP API per S3 spec §7.2.

### F5.4 Reset integration
- Integrate the reset paths (Phase 3 D3.3) into the unified
  `annealage_pod.dut.reset(mode=...)` API: `swd` and `nrst` on the bare Pico 2 W;
  `power` when custom carrier hardware is available.
- Status (2026-09-02): `nrst` is **landed and hardware-validated** on the
  second-DUT-family bring-up (RP2350B pod, i.MX RT1052 Arch Mix), as scheduled.
  It is implemented in `annealage_pod.debug.nrst` and reached through
  `ops.reset(mode="nrst")` / `pod dut reset --mode nrst` / the `dut_reset` MCP tool,
  driving the GPIO directly rather than through `annealage_pod.dut` - that module
  is the ESP32-S3 carrier's four-path API and depends on carrier hardware a bare
  pod does not have. A pulse was confirmed to set the target's
  `DHCSR.S_RESET_ST`, so the core really is reset.
- Bring-up found a defect this task would otherwise have inherited: nothing
  configured GP13, and an RP2350 pad powers up with its internal pull-down
  enabled, so the pod was holding any DUT with a weaker reset pull-up in reset
  from power-on. `netboot.main()` now parks the line via `nrst.park()` before
  anything else touches the DUT. "Unhandled" was not neutral here - it asserted
  reset - so the park is part of the deliverable, not a tidy-up.
- The "unified `annealage_pod.dut.reset(mode=...)`" this task names was the
  ESP32-S3 carrier API and is NOT the RP2350 entry point; that carrier code (and
  `annealage_pod.dut` with it) has since been removed from the tree entirely
  (retrievable at tag `v1.6.0-native-flash-default` if ever needed). The unified
  reset surface on the RP2350 is `ops.reset(mode="sysreset"|"halt"|"nrst")`,
  which the host `pod dut reset --mode` and the `dut_reset` MCP tool drive.

### F5.5 RP_INFRA API mimicry
- Provide the RP_INFRA-equivalent surface (S3 spec §7.1, appendix B) so
  testbed_micropython needs only a transport adapter. Share the package with the
  S3 variant against the platform-capability interface (overview §5).

### F5.6 Measurement primitives (consumer-requested candidates)
- Two PIO-based measurement tools requested by the `aio` asyncio-hardware roadmap
  (a pod HIL consumer, 2026-07). Neither is covered by the existing logic
  analyser, which is fixed-depth timed sampling on PIO0 with no counting /
  frequency mode and a sub-second window (buffer caps at ~80 KB: 20000 samples at
  32-bit width, up to ~640000 at 1-bit):
  - **Wake-latency / GPIO round-trip histogram.** Drive a stimulus edge and
    timestamp the DUT's GPIO response on one PIO0 timebase, repeated N times,
    returning an array of deltas (the histogram directly, no manual VCD
    correlation). Serves the aio notify-primitive / pin-event and STOP-wake
    tickets.
  - **Edge-counter / frequency over a window.** Count edges (or measure
    frequency) on a pin for T seconds. Serves the aio tickless idle-wake-count
    test (wakes/second dropping from ~1000 to the deadline count), which the LA's
    short window cannot measure.
- Both are new PIO programs on the free PIO0 SM budget (PIO2 is CYW43 Wi-Fi,
  never touch; `pio_arbiter`). Both can coexist with SWD (PIO1) and Wi-Fi (PIO2)
  the same way the LA does. Prototype in-test via `pod_exec` + a small PIO program
  first, then promote the useful ones to stable MCP tools. Candidates, not
  scheduled; priority tracks the aio roadmap.

## Deliverables

- PIO I2C/SPI target modules with the register-table API.
- UART-over-TCP bridge.
- Unified reset API (`swd`/`nrst`; `power` on custom carrier). INA228 telemetry
  deferred to the custom carrier.
- The `annealage_pod` package presenting the RP_INFRA-compatible surface on
  RP2350.

## Exit gate

A DUT-as-master test drives the pod's I2C/SPI target; UART forwards over TCP; the
`swd` and `nrst` reset paths work; the RP_INFRA API surface is present. INA228
telemetry and the `power` reset path are validated when custom carrier hardware
exists.

## References

- `v1.6.0-native-flash-default:docs/esp32-s3/design/slaveio.md`, `v1.6.0-native-flash-default:docs/esp32-s3/design/uartbridge.md`
- `v1.6.0-native-flash-default:docs/esp32-s3/design/annealage-pod-package.md`, S3 spec §7, appendix B
- `src/mpy/annealage_pod/` (existing package to share/factor)
