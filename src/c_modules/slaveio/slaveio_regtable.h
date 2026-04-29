// Annealage Pod: slaveio register-table state machine.
//
// Pure C, no IDF or FreeRTOS dependency. Implements the byte-level
// protocol both the I2C-slave and SPI-slave personalities expose to a
// DUT-side master:
//
//   write transaction: [reg_ptr][data0][data1]...[dataN]
//                      -> reg_ptr stored, data appended into write_table
//                         starting at write_table[reg_ptr], reg_ptr
//                         advances by N for the next op.
//
//   read transaction:  master clocks bytes out
//                      -> bytes served from read_table[reg_ptr+], reg_ptr
//                         advances per byte served.
//
// The state machine owns no peripheral. Hardware drivers (I2C-slave
// in slaveio.c, SPI-slave in slaveio.c) feed received bytes via
// slaveio_regtable_on_rx_*() and pull TX bytes via
// slaveio_regtable_get_tx_byte(). Wraparound on either table indexes
// modulo the table size; this matches the v0.7 RP_PROBE behaviour for
// out-of-range register addresses.
//
// Notify ranges are inclusive-exclusive [start, end). When a write
// transaction completes that touched any byte inside any registered
// range, the corresponding callback id is appended to a small fixed
// dispatch queue. The hardware-driver thread reads the queue and
// dispatches to MicroPython on a non-ISR FreeRTOS task. The state
// machine itself does not invoke callbacks; it only collects ids.

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SLAVEIO_REGTABLE_MAX_NOTIFIES 8
#define SLAVEIO_REGTABLE_NOTIFY_QUEUE  16

typedef struct {
    bool active;
    uint32_t start_offset;
    uint32_t end_offset;        // exclusive
    uint32_t callback_id;
} slaveio_regtable_notify_t;

typedef struct {
    // Backing buffers; not owned. Writer (driver) supplies them.
    uint8_t *read_buf;
    size_t read_size;
    uint8_t *write_buf;
    size_t write_size;

    // Address pointer. Updated on the first byte of each write
    // transaction, advanced as bytes are written or read.
    uint32_t reg_ptr;

    // True between the first byte of a write transaction and the
    // transaction-complete event. The first byte taken in this state
    // is interpreted as the register pointer; subsequent bytes go
    // into write_buf.
    bool in_write_xact;
    uint32_t bytes_in_xact;
    uint32_t xact_first_offset; // start address of the current write

    // Statistics.
    uint64_t transfers_total;
    uint32_t overflow_count;    // writes past write_size or reads past read_size

    // Last-transaction info for slaveio_status().
    uint32_t last_xact_offset;
    uint32_t last_xact_length;
    bool last_xact_was_write;

    // Notify ranges.
    slaveio_regtable_notify_t notifies[SLAVEIO_REGTABLE_MAX_NOTIFIES];

    // Dispatch queue: ring of pending callback ids to fire from a
    // non-ISR context. The hardware-driver thread drains this.
    uint32_t notify_queue[SLAVEIO_REGTABLE_NOTIFY_QUEUE];
    uint16_t notify_queue_head;
    uint16_t notify_queue_tail;
    uint16_t notify_queue_used;
    uint32_t notify_queue_dropped;
} slaveio_regtable_t;

// Reset all state and bind the buffers. Called from the driver when
// the personality starts.
void slaveio_regtable_init(slaveio_regtable_t *st,
                           uint8_t *read_buf, size_t read_size,
                           uint8_t *write_buf, size_t write_size);

// Begin a new write transaction. The next call to
// slaveio_regtable_on_rx_byte() interprets its argument as the register
// pointer.
void slaveio_regtable_begin_write(slaveio_regtable_t *st);

// Feed one received byte from the master. Returns false if the byte
// caused a write past the end of write_buf (overflow_count incremented
// and the byte dropped).
bool slaveio_regtable_on_rx_byte(slaveio_regtable_t *st, uint8_t b);

// Feed a chunk of received bytes from the master. Returns the number
// of bytes consumed; bytes that would overflow are dropped and
// overflow_count is incremented per dropped byte.
size_t slaveio_regtable_on_rx_chunk(slaveio_regtable_t *st,
                                    const uint8_t *buf, size_t len);

// End the current write transaction. Posts notify-callback ids for any
// range that the transaction touched. Returns the number of notifies
// posted (0 to SLAVEIO_REGTABLE_MAX_NOTIFIES).
unsigned int slaveio_regtable_end_write(slaveio_regtable_t *st);

// Pull one byte to send to the master from the read table at the
// current register pointer. Advances the pointer. Returns false on
// overflow (read past read_size; overflow_count incremented and *out
// set to 0xFF).
bool slaveio_regtable_get_tx_byte(slaveio_regtable_t *st, uint8_t *out);

// Pull a chunk of bytes for the master. Returns the number of bytes
// written into buf; partial reads occur on overflow.
size_t slaveio_regtable_get_tx_chunk(slaveio_regtable_t *st,
                                     uint8_t *buf, size_t cap);

// Begin and end a read transaction. begin marks the read xact in
// progress; end finalises last_xact_* stats. Notifies are not posted
// for reads (only writes notify per spec §4.8 i2c.on_write).
void slaveio_regtable_begin_read(slaveio_regtable_t *st);
void slaveio_regtable_end_read(slaveio_regtable_t *st);

// Notify management. Returns -1 if the table is full or the slot is
// already in use. Slot indices 0..SLAVEIO_REGTABLE_MAX_NOTIFIES-1.
int slaveio_regtable_register_notify(slaveio_regtable_t *st,
                                     uint32_t start_offset,
                                     uint32_t end_offset,
                                     uint32_t callback_id);

// Clear the notify slot for a callback id. Returns 0 on success, -1
// if no matching slot.
int slaveio_regtable_unregister_notify(slaveio_regtable_t *st,
                                       uint32_t callback_id);

// Drain one notify dispatch from the queue. Returns false if the
// queue is empty.
bool slaveio_regtable_pop_notify(slaveio_regtable_t *st, uint32_t *out_id);

#ifdef __cplusplus
}
#endif
