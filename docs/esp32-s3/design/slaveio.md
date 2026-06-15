# slaveio design notes

Companion to `spec.md` §4.8 (slave-mode personalities) and
`architecture.md` §2 (component model). Records the WS-F implementation
decisions for the C user module that exposes hardware-driven I2C-slave
and SPI-slave register-table responders to the DUT.

## 1. Scope

- One personality active at a time: I2C-slave or SPI-slave.
- Register-table semantics on both: master writes
  `[reg_ptr][data...]`; master reads serve from `read_table[reg_ptr+]`;
  the pointer advances byte-for-byte and wraps modulo the table size.
- Hardware-driven: peripheral ISR fills the write table and serves
  bytes from the read table, no MicroPython code in the critical path.
- MicroPython side reads or writes either table at any time, registers
  notify callbacks for writes to a byte range; callbacks fire on a
  non-ISR FreeRTOS task.
- Mutual exclusion enforced in the C module: starting one personality
  while the other is active returns `ESP_ERR_INVALID_STATE`.

## 2. Peripheral and pin choice

Per Appendix A §A.5.1:

| Personality | Peripheral | S3 GPIOs                                              | DIR    |
|-------------|------------|-------------------------------------------------------|--------|
| I2C-slave   | I2C1       | SDA=GPIO17 (DIR-controlled translator), SCL=GPIO18    | GPIO21 |
| SPI-slave   | GPSPI3     | MOSI=GPIO17, SCK=GPIO18, MISO=GPIO38, CS=GPIO39       | GPIO21 |

Rationale:
- I2C0 is reserved for the local 3v3 bus (INA228s and carrier-ID
  EEPROM, GPIO8/9), so DUT-facing I2C-slave lands on I2C1.
- SPI2 (FSPI) is reserved by WS-D for the SWD engine, so SPI-slave
  lands on SPI3 (GPSPI3).
- GPIO17 and GPIO18 are physically shared between the two
  personalities through carrier pins 13 and 14 (Appendix A §A.3 notes
  the muxing); only one peripheral can route to those pads at a time.
  The mutual-exclusion rule in this module enforces the muxing
  constraint at the firmware level.
- DIR (GPIO21) drives the level-shifter direction for the shared
  SDA/MOSI carrier line. For I2C-slave SDA is genuinely bidirectional,
  but for rev1 we leave DIR strapped low (DUT->S3) at idle; the I2C
  peripheral's open-drain ACK pulse drives the line through the
  always-input strap. For SPI-slave DIR stays low for the entire
  personality (MOSI is master driven). A future revision may switch
  DIR per phase from the I2C ISR; that change is local to `slaveio.c`.

## 3. IDF API selection (v5.5.1)

The MicroPython esp32 port's `boards/sdkconfig.base` already enables
the I2C slave driver V2:

```
CONFIG_I2C_ENABLE_SLAVE_DRIVER_VERSION_2=y
```

This module targets V2 only. V1 is not supported because:

- V1's `on_recv_done` callback delivers a buffer pointer but no
  length; the actually-received byte count is held in the V1 driver's
  private `i2c_slave_dev_t.already_receive_len` field, so user code
  cannot recover it without reaching into private headers.
- V1 requires the user to pre-submit a receive buffer via
  `i2c_slave_receive()`. After every callback the user must
  re-submit, with all the race-condition surface that implies.
- V2 fixes both: `on_receive` delivers (buffer, length); the driver
  manages the receive ringbuffer; `on_request` fires when the master
  reads with no TX data primed.

The earlier WS-F dispatch attempted to keep a V1 fallback path (with
`i2c_slave_transmit`, `i2c_slave_receive`, and an `on_recv_done`
callback). With `CONFIG_I2C_ENABLE_SLAVE_DRIVER_VERSION_2=y` set in
the MP esp32 port, those V1-only symbols are not declared in
`driver/i2c_slave.h` and the build failed at compile time. The current
implementation drops the V1 path and the build succeeds.

## 4. Register-table state machine

Implemented in `slaveio_regtable.{c,h}` as a pure-C state machine with
no IDF or FreeRTOS dependency. The driver code feeds it received bytes
via `slaveio_regtable_on_rx_*()` and pulls TX bytes via
`slaveio_regtable_get_tx_*()`. The host-side unit test
(`test/unit/slaveio/`) exercises this directly without any peripheral.

Protocol:

- A write transaction begins with `slaveio_regtable_begin_write()`,
  the first byte received is interpreted as the register pointer, and
  subsequent bytes are stored at `write_table[reg_ptr+]`. Wrapping is
  modulo the table size.
- A read transaction serves bytes from `read_table[reg_ptr+]`,
  advancing the pointer per byte. Reads do not modify the pointer
  starting offset; the master is expected to set the pointer with a
  preceding write of `[reg_ptr]` only (zero-byte payload write).
- A combined write-then-read (I2C repeated-start; SPI continuous
  clock with the master changing direction via separate transactions
  on a CS edge) sets the pointer in the write phase and serves bytes
  from that pointer in the read phase.

Notify callbacks:

- Up to 8 ranges per personality, each `[start, end)`.
- On `end_write()` the regtable walks the notify list and posts the
  callback id for any range that overlapped the data-byte span
  (xact_first_offset to xact_first_offset + data_bytes).
- Callback ids are queued in a 16-deep ring; overflow increments
  `notify_queue_dropped` rather than blocking the ISR.

## 5. ISR contract and dispatch path

I2C-slave (V2):

1. ISR `on_receive` fires after the master finishes a write
   transaction. The IDF driver hands us a buffer pointer and a
   length. We copy the bytes into a small queue entry and post to a
   FreeRTOS queue.
2. ISR `on_request` fires when the master clocks a read with no TX
   data primed. We post a marker entry of type request.
3. The `slaveio_i2c` pump task drains the queue. On a write event it
   feeds the bytes through the regtable (`begin_write` ->
   `on_rx_chunk` -> `end_write`), which posts notify ids. On a
   request event it tops up the I2C TX ring via `i2c_slave_write()`
   with `read_table[reg_ptr+]` so the master's read clocks out the
   right bytes; the V2 driver's clock-stretching keeps the master
   waiting until bytes arrive.
4. The pump task signals the dispatch task via a binary semaphore
   when notifies are pending.
5. The dispatch task pops notify ids and calls the registered C
   dispatch function (set by `modslaveio.c` to a thunk that schedules
   the Python callable via `mp_sched_schedule`).

SPI-slave:

1. The pump task pre-fills `tx_scratch` with `read_table[reg_ptr+]`
   for one transaction unit, then issues `spi_slave_transmit()`. This
   is a blocking call that returns when the master clocks the
   transaction.
2. ISR `post_trans_cb` fires from the driver. We do not use it for
   wakeups because the pump task already blocks on
   `spi_slave_transmit()`; the callback is wired only to satisfy the
   IDF API.
3. The pump task post-processes `rx_scratch` (which holds whatever
   the master clocked on MOSI) by feeding it through the regtable as
   a write transaction. The first byte is the register pointer,
   subsequent bytes are payload.
4. Notify ids posted during `end_write` are picked up by the dispatch
   task as in the I2C path.

The dispatch task runs at priority 5 on APP_CPU and only calls into a
C function pointer set by the MP binding. The MP binding's thunk
schedules the Python callable via `mp_sched_schedule`, which queues
the call on the MicroPython main task. The MP callable therefore runs
on the MP main task, never inside an ISR or while the regtable mutex
is held.

## 6. Mutual-exclusion enforcement

The `slaveio_state_t` struct holds a `slaveio_personality_t active`
field. `slaveio_i2c_start()` and `slaveio_spi_start()` both lock the
state mutex, check `active`, and return `ESP_ERR_INVALID_STATE` if the
other personality is currently up. Calling start twice on the
same-already-active personality is a no-op (idempotent). Stop returns
`ESP_OK` when called on an already-stopped personality.

Tearing down one personality before bringing up the other is the
required pattern; `annealage_pod.slave.{i2c,spi}.stop()` exposes this from
MP.

## 7. Status snapshot

`slaveio.{i2c,spi}_status()` returns a dict:

| key                | meaning                                              |
|--------------------|------------------------------------------------------|
| `active`           | true if this personality is up                       |
| `transfers_total`  | combined read + write transactions counted           |
| `overflow_count`   | bytes dropped past either table size                 |
| `last_offset`      | starting offset of the most recent transaction       |
| `last_length`      | byte count of the most recent transaction            |
| `last_was_write`   | true if the most recent transaction was a write      |
| `reg_ptr`          | current register pointer                             |
| `notify_dropped`   | callbacks dropped due to dispatch queue overflow     |

## 8. Constraints from IDF v5.5.1

The V2 I2C-slave driver in IDF v5.5.x runs the master/slave handshake
fully in hardware and posts `on_receive` per transaction. We rely on
its software ringbuffer semantics: the master can write without
synchronisation, the V2 driver buffers up to `receive_buf_depth`
bytes, and our ISR callback runs once per master STOP with the
correct length.

The `SOC_I2C_SLAVE_SUPPORT_I2CRAM_ACCESS` capability on ESP32-S3
exposes a 32-byte hardware-RAM model where the master can read or
write the RAM directly. We do not use it: rev1 spec demands
configurable buffer sizes (default 256 bytes per spec §4.8 example),
which exceeds the 32-byte RAM. The software regtable lives in
`slaveio_regtable.c` instead.

The SPI slave driver does not expose a register-table model out of the
box; it presents one transaction at a time. The pump task synthesises
the register-table semantics by reusing the `[reg_ptr][data...]`
protocol on each transaction. This mirrors how real SPI-slave parts
(e.g. EEPROMs) are addressed and matches the v0.7 carrier expectations.

## 9. Testing

Phase 2 exit is build-clean plus host-side unit tests of the
register-table state machine without hardware:

- `test/unit/slaveio/test_regtable.c` exercises:
  - simple register-write then register-read
  - register-pointer update only (zero-byte payload)
  - wrap-around past the end of either table
  - notify range registration, dispatch on overlapping write,
    callback id lookup
  - notify queue overflow handling
  - overflow_count incremented on undersized buffers

Run via `test/unit/slaveio/run.sh`.

Phase 3 brings up second-MCU-as-master integration tests against real
I2C and SPI transactions, exercising both 100 kHz and 400 kHz I2C and
several SPI mode/clock combinations. Those tests are out of scope for
WS-F which is bounded to build-clean and unit-test-pass.

## 10. Out of scope for rev1

- DIR per-phase toggling for I2C-slave SDA. Rev1 strap is enough for
  3v3 logic, master writes dominate the bus, and the I2C peripheral's
  open-drain output handles ACK without translator flipping.
- The MicroPython buffer view returned by `read_table()` /
  `write_table()` is zero-copy. MP-side writes update the underlying
  byte array seen by the next master read directly. There is no
  cross-thread atomicity beyond byte-level access; callers requiring
  multi-byte atomic snapshots must coordinate with the notify
  callback for the relevant range.
