# uartbridge design notes

Companion to `spec.md` §4.4 / §5.3 and `architecture.md` §4.5. Records
the implementation decisions for the WS-G uartbridge C user module that
exposes the DUT UART as a TCP service.

## 1. Scope

- TCP server on `cfg.tcp_port` (default 2000).
- Forwards bytes byte-for-byte between the configured ESP32-S3 UART
  (default UART2 on GPIO15 TX / GPIO16 RX per Appendix A §A.5.1) and a
  single TCP client.
- Per-connection settings configured at start time: baud (300..921600),
  data bits (5..8), parity (none/even/odd), stop bits (1 or 2), local
  hardware flow control (CTS/RTS) on or off.
- Optional RFC2217-style telnet framing (default off) lets the client
  renegotiate baud/parity/data/stop without a stop+restart on the MP
  side.

Hardware flashing and live integration with the level translator
direction strap (Appendix A §A.5.1, GPIO15 fixed-out, GPIO16 fixed-in)
are Phase 3 concerns. Phase 2 exit is build-clean plus protocol-level
unit tests.

## 2. MicroPython surface

```python
import uartbridge

uartbridge.start(uart=2, port=2000, baud=115200, bits=8,
                 parity=None, stop=1, flow=False, telnet=False,
                 tx=-1, rx=-1, rts=-1, cts=-1,
                 replace_client=True, core=0,
                 rx_buf=0, tx_buf=0)            # -> None
uartbridge.stop()                              # -> None
uartbridge.config_get()                        # -> dict
uartbridge.client_count()                      # -> 0 or 1
```

`parity=None` maps to `UART_PARITY_DISABLE`; `parity=0` maps to
`UART_PARITY_EVEN`; `parity=1` maps to `UART_PARITY_ODD`. Pin numbers
of -1 mean "use the IDF default for this UART instance"; the
ESP32_S3_ANNEALAGE_POD board variant binds UART2 to GPIO15/16 already, so
the production callsite passes only `uart=2, port=2000, baud=...`.

C-side errors raise `OSError(esp_err_code)`.

## 3. Concurrency model

Three FreeRTOS tasks, all pinned to `cfg.task_core` (default
PRO_CPU = 0):

| Task          | Lifetime         | Stack | Prio | Role                                          |
|---------------|------------------|------:|-----:|-----------------------------------------------|
| `ub_accept`   | start..stop      | 4 KB  | 5    | `accept()` loop on the listening socket       |
| `ub_tcp_rx`   | per-session      | 4 KB  | 5    | `recv()` from client, `uart_write_bytes()`    |
| `ub_uart_rx`  | per-session      | 4 KB  | 5    | UART event queue drain, `send()` to client    |

PRO_CPU placement matches `architecture.md` §3 ("UART bridge task |
PRO_CPU | 5 | 4 KB"). The bridge is not latency-critical; spec.md
§3 places it alongside MP and Wi-Fi/lwIP. Co-location avoids the
PRO->APP IPC hop on every byte.

Synchronisation:

- `g_state.lock` (`xSemaphoreCreateMutex`) guards the mutable shared
  state: `client_fd`, `listen_fd`, `cfg`, `running`,
  `session_stopping`. Held only across short critical sections.
- `accept_done`, `tcp_rx_done`, `uart_rx_done` (binary semaphores) are
  posted by each task on exit so `uart_bridge_stop()` can wait for
  them with a bounded timeout (2 s).
- The IDF UART driver provides its own internal locking; the bridge
  serialises only with respect to the event queue and the
  `uart_read_bytes` / `uart_write_bytes` calls (these are
  thread-safe per the IDF v5.5.1 docs).

### 3.1 Connection lifecycle

```
        +-------------+
        |  ub_accept  |
        +------+------+
               | accept()
               v
        +------+------+
        | new client? |
        +-+---------+-+
          |         |
busy && replace?    busy && !replace?
          |         |
   tear down        |
   prior session    v
          |     close(new_fd)
          v
   spawn ub_tcp_rx + ub_uart_rx
   set client_fd
          v
   loop until session_stopping
   ----------> close(client_fd) <----------
   |              |              |
   tcp_rx exit    uart_rx exit   stop()
```

`replace_client = true` (default) preserves the spec wording "subsequent
connect requests close the old session and take over". Setting it to
false makes a second connect a no-op and immediately close the new
socket (useful when the host PC has flaky reconnects and the existing
session must be authoritative).

### 3.2 Shutdown semantics

`uart_bridge_stop()`:

1. Sets `running = false` and `session_stopping = true`.
2. `shutdown(listen_fd, SHUT_RDWR)` to break the blocked `accept()`.
3. Closes the active client socket so per-session `recv()` and
   `send()` calls return EOF/error.
4. Waits up to 2 s on each of the three `_done` semaphores.
5. Calls `uart_driver_delete()` (or releases the test FIFO).

The bridge does not post a sentinel UART event because the UART event
queue is drained with a 50 ms timeout in `xQueueReceive`. The 50 ms
poll cap also bounds the time `uart_rx_task` takes to notice
`session_stopping = true` while no UART traffic is flowing. Steady-state
this is invisible: under traffic the task wakes on every event.

## 4. Buffering strategy

Three buffers exist along the path:

1. **UART RX ringbuffer** (IDF-managed, default 2048 B). Configurable
   via `rx_buf`. Sized to absorb at least 60 ms at the maximum
   configured baud (921600 / 10 = 92160 B/s, 2048 B is ~22 ms; the
   default is sufficient when the bridge keeps up with the host
   socket). Callers who need more headroom raise `rx_buf` at start.
2. **UART TX ringbuffer** (IDF-managed, default 2048 B). Configurable
   via `tx_buf`. `uart_write_bytes()` blocks when full.
3. **Per-task stack buffers** of `UART_BRIDGE_IO_CHUNK = 512` bytes.
   No heap allocation along the data path.

No host-side ring is added on top of TCP; lwIP already provides one.
`TCP_NODELAY` is set on accepted sockets so single-byte interactive
traffic (e.g. REPL prompts) reaches the host without Nagle delay.

### 4.1 Overflow policy

UART events `UART_FIFO_OVF` and `UART_BUFFER_FULL` are logged at WARN,
the IDF input is flushed (`uart_flush_input`), the event queue is
reset, and the session continues. Same semantics as the SWO pipeline
(spec.md §4.7 "set the `DAP_SWO_BUFFER_OVERRUN` flag and continue
streaming on the next frame"). The bridge does not have a host-visible
overflow counter on its public API; if one is needed in Phase 3 it
will be added as `uartbridge.stats()`.

## 5. DTR/RTS over the bridge

**Decision: not supported in rev1.**

Per the task brief for WS-G, this entry resolves Appendix B's open
question 4 with respect to the uartbridge: DTR/RTS modem-control lines
are not propagated end-to-end through this bridge in rev1.

Related context in `spec-appendix-B-rp_infra-api.md` §B.8 item 6
flagged that esptool's reset sequence on a forwarded DUT-CDC depends
on DTR/RTS being conveyed end-to-end across USB/IP. That open question
concerns the USB/IP path (the DUT's USB-CDC, busid 1) and is owned by
WS-A and WS-B, not by uartbridge.

The DUT UART forwarded by uartbridge is a raw UART without modem
control lines on the carrier; carrier pins 11/12 (Appendix A §A.3) are
TX/RX only. There are no DTR or RTS pins on the carrier-side
DUT_UART_TX / DUT_UART_RX nets to forward. The S3-side UART2 can
operate hardware CTS/RTS flow control as a local-only feature
(`cfg.flow_control` true), but those signals are between the S3 and
the optional translator, not visible to the host.

Consequently:

- The MP API does not surface DTR/RTS state or callbacks.
- The optional `telnet=True` mode parses RFC2217 com-port
  subnegotiation but only acts on `SET-BAUDRATE`, `SET-DATASIZE`,
  `SET-PARITY`, `SET-STOPSIZE`. `SET-CONTROL` (DTR/RTS/break) and
  modem-line notifications are silently ignored. A real RFC2217 server
  would round-trip these; this bridge intentionally does not, because
  there is nothing on the carrier to drive.
- The accompanying esptool-reset workflow on the DUT runs over USB/IP
  (DUT busid 1), not over uartbridge. This bridge is for general DUT
  printf/REPL forwarding, console captures, and tests that talk to a
  DUT UART other than the one on its USB-CDC.

Should rev2 grow a DUT modem-control line, the implementation route is
to add two output GPIOs through fixed-direction translators, expose
them as `uartbridge.dtr(level)` / `uartbridge.rts(level)`, and let
RFC2217 `SET-CONTROL` flow through.

## 6. Telnet (RFC2217) mode

Default off. Enabled with `telnet=True`.

Decoder state machine in `telnet_filter()`:

- Normal -> on `0xFF` switch to `IAC`.
- `IAC` -> on `0xFF` emit one literal `0xFF` (escaped IAC); on `DO/DONT/WILL/WONT` consume one option byte; on `SB` enter subneg.
- Subneg -> accumulate up to 16 bytes, terminate on `IAC SE`.

Subnegotiation handler `telnet_apply_sb()` recognises option `44`
(COM-PORT-OPTION), then dispatches on the first sub-option byte:

| Sub-opt | Length | Effect on bridge config             |
|--------:|-------:|-------------------------------------|
| 1       | 6      | `baud` (4-byte big-endian)          |
| 2       | 3      | `data_bits` (5..8)                  |
| 3       | 3      | `parity` (1=NONE, 2=ODD, 3=EVEN)    |
| 4       | 3      | `stop_bits` (1 or 2)                |

Any other sub-option is silently ignored. The bridge does not echo
back the new settings as a real RFC2217 server does (the host already
knows what it sent, and the open kwarg is gated specifically because
"bare TCP is what the spec calls out"). All inbound non-IAC bytes flow
straight to the UART; no LF/CR rewriting, no character-mode
negotiation.

This minimal mode is enough for the pyserial RFC2217 client and for
custom host harnesses that want a single TCP connection per DUT with
runtime baud changes (e.g. for boards that boot at one baud and switch
mid-test). Hosts that don't want this behaviour leave `telnet` at its
default of false and get raw byte transparency.

## 7. Test strategy

The `test/unit/uartbridge/` suite drives the bridge purely through the
public API and a test-mode loopback that replaces the IDF UART driver
with two in-memory FIFOs. The same tasks run, the same socket plumbing
runs, only the UART hardware is bypassed. This lets the protocol-level
behaviour (round-trip transparency, replace-client semantics, telnet
filtering, shutdown ordering) be exercised on a host build without
real ESP32 hardware.

Test cases:

- raw round-trip: TCP -> UART -> verify bytes accumulated.
- raw round-trip: UART -> TCP -> verify bytes received.
- replace-client: second connect closes the first; first session's
  socket reads zero.
- telnet filter: IAC IAC -> single 0xFF.
- telnet baud rate change via subnegotiation -> `config_get` reflects.
- shutdown: `stop()` while a session is live closes both ends.

Hardware loopback (TX -> RX wired through the GPIO matrix on a real
board) is a Phase 3 deliverable; the GPIO-matrix-loopback path is
documented but not exercised in this WS.

## 8. Files

- `src/c_modules/uartbridge/uart_bridge.h` -- public API.
- `src/c_modules/uartbridge/uart_bridge.c` -- state machine, tasks,
  IDF UART glue, telnet decoder, test FIFO.
- `src/c_modules/uartbridge/moduartbridge.c` -- MP binding.
- `src/c_modules/uartbridge/micropython.cmake` -- build glue.
- `test/unit/uartbridge/` -- protocol-level unit tests.
- `docs/esp32-s3/design/uartbridge.md` -- this file.

## 9. Open items deferred to Phase 3

- Hardware loopback test on a real S3 board (TX->RX cross-wired).
- Verifying flow-control-on operation against a DUT that asserts CTS.
- Throughput benchmark at 921600 baud sustained for 60 s; spec WS-G
  exit criterion in `plan/phase-2-parallel-implementation.md` is
  Phase-3 scope per the WS-G charter.
- Coupling with the supervisor cleanup hook (architecture.md §5.4)
  so a REPL TCP disconnect tears down an active uartbridge session
  if the test profile requests it.
