// Annealage Pod: register-table state machine for slaveio.
//
// See slaveio_regtable.h for the protocol. No IDF or FreeRTOS
// dependency; deliberately portable so the protocol logic is exercised
// by host-side unit tests without a target.

#include "slaveio_regtable.h"

#include <string.h>

void slaveio_regtable_init(slaveio_regtable_t *st,
                           uint8_t *read_buf, size_t read_size,
                           uint8_t *write_buf, size_t write_size) {
    memset(st, 0, sizeof(*st));
    st->read_buf = read_buf;
    st->read_size = read_size;
    st->write_buf = write_buf;
    st->write_size = write_size;
}

void slaveio_regtable_begin_write(slaveio_regtable_t *st) {
    st->in_write_xact = true;
    st->bytes_in_xact = 0;
    st->xact_first_offset = st->reg_ptr;
}

bool slaveio_regtable_on_rx_byte(slaveio_regtable_t *st, uint8_t b) {
    if (!st->in_write_xact) {
        // Stray byte outside a transaction; treat as a register-pointer
        // setup with no following data.
        st->reg_ptr = b;
        return true;
    }
    if (st->bytes_in_xact == 0) {
        st->reg_ptr = b;
        st->xact_first_offset = b;
        st->bytes_in_xact = 1;
        return true;
    }
    if (st->write_size == 0) {
        st->overflow_count++;
        st->bytes_in_xact++;
        return false;
    }
    if (st->reg_ptr >= st->write_size) {
        // Wrap. The caller side keeps incrementing reg_ptr unbounded;
        // we wrap modulo write_size for the storage write, and treat
        // it as a non-overflow case to mirror typical slave EEPROM
        // behaviour.
        st->reg_ptr = st->reg_ptr % st->write_size;
    }
    st->write_buf[st->reg_ptr] = b;
    st->reg_ptr++;
    st->bytes_in_xact++;
    return true;
}

size_t slaveio_regtable_on_rx_chunk(slaveio_regtable_t *st,
                                    const uint8_t *buf, size_t len) {
    size_t n = 0;
    for (size_t i = 0; i < len; ++i) {
        if (slaveio_regtable_on_rx_byte(st, buf[i])) {
            n++;
        }
    }
    return n;
}

static bool ranges_overlap(uint32_t a_start, uint32_t a_end,
                           uint32_t b_start, uint32_t b_end) {
    if (a_end <= a_start) { return false; }
    if (b_end <= b_start) { return false; }
    return (a_start < b_end) && (b_start < a_end);
}

static void notify_queue_push(slaveio_regtable_t *st, uint32_t id) {
    if (st->notify_queue_used >= SLAVEIO_REGTABLE_NOTIFY_QUEUE) {
        st->notify_queue_dropped++;
        return;
    }
    st->notify_queue[st->notify_queue_head] = id;
    st->notify_queue_head = (uint16_t)((st->notify_queue_head + 1) % SLAVEIO_REGTABLE_NOTIFY_QUEUE);
    st->notify_queue_used++;
}

unsigned int slaveio_regtable_end_write(slaveio_regtable_t *st) {
    unsigned int posted = 0;
    if (!st->in_write_xact) {
        return 0;
    }
    st->in_write_xact = false;
    st->transfers_total++;

    // Data bytes touched: bytes_in_xact - 1 (first byte was the
    // register pointer). Range is [xact_first_offset, xact_first_offset + n).
    uint32_t data_bytes = (st->bytes_in_xact >= 1) ? (st->bytes_in_xact - 1) : 0;
    uint32_t first = st->xact_first_offset;
    uint32_t last_end = first + data_bytes;

    st->last_xact_was_write = true;
    st->last_xact_offset = first;
    st->last_xact_length = data_bytes;

    if (data_bytes == 0) {
        return 0;
    }

    for (size_t i = 0; i < SLAVEIO_REGTABLE_MAX_NOTIFIES; ++i) {
        slaveio_regtable_notify_t *n = &st->notifies[i];
        if (!n->active) { continue; }
        if (ranges_overlap(first, last_end, n->start_offset, n->end_offset)) {
            notify_queue_push(st, n->callback_id);
            posted++;
        }
    }
    return posted;
}

bool slaveio_regtable_get_tx_byte(slaveio_regtable_t *st, uint8_t *out) {
    if (st->read_size == 0) {
        st->overflow_count++;
        if (out) { *out = 0xFF; }
        return false;
    }
    uint32_t idx = st->reg_ptr;
    if (idx >= st->read_size) {
        idx = idx % st->read_size;
        st->reg_ptr = idx;
    }
    if (out) { *out = st->read_buf[idx]; }
    st->reg_ptr++;
    return true;
}

size_t slaveio_regtable_get_tx_chunk(slaveio_regtable_t *st,
                                     uint8_t *buf, size_t cap) {
    size_t n = 0;
    while (n < cap) {
        if (!slaveio_regtable_get_tx_byte(st, &buf[n])) {
            break;
        }
        n++;
    }
    return n;
}

void slaveio_regtable_begin_read(slaveio_regtable_t *st) {
    st->bytes_in_xact = 0;
    st->xact_first_offset = st->reg_ptr;
}

void slaveio_regtable_end_read(slaveio_regtable_t *st) {
    st->transfers_total++;
    st->last_xact_was_write = false;
    st->last_xact_offset = st->xact_first_offset;
    if (st->reg_ptr >= st->xact_first_offset) {
        st->last_xact_length = st->reg_ptr - st->xact_first_offset;
    } else {
        st->last_xact_length = 0;
    }
}

int slaveio_regtable_register_notify(slaveio_regtable_t *st,
                                     uint32_t start_offset,
                                     uint32_t end_offset,
                                     uint32_t callback_id) {
    if (end_offset <= start_offset) { return -1; }
    for (size_t i = 0; i < SLAVEIO_REGTABLE_MAX_NOTIFIES; ++i) {
        if (st->notifies[i].active && st->notifies[i].callback_id == callback_id) {
            st->notifies[i].start_offset = start_offset;
            st->notifies[i].end_offset = end_offset;
            return (int)i;
        }
    }
    for (size_t i = 0; i < SLAVEIO_REGTABLE_MAX_NOTIFIES; ++i) {
        if (!st->notifies[i].active) {
            st->notifies[i].active = true;
            st->notifies[i].start_offset = start_offset;
            st->notifies[i].end_offset = end_offset;
            st->notifies[i].callback_id = callback_id;
            return (int)i;
        }
    }
    return -1;
}

int slaveio_regtable_unregister_notify(slaveio_regtable_t *st,
                                       uint32_t callback_id) {
    for (size_t i = 0; i < SLAVEIO_REGTABLE_MAX_NOTIFIES; ++i) {
        if (st->notifies[i].active && st->notifies[i].callback_id == callback_id) {
            memset(&st->notifies[i], 0, sizeof(st->notifies[i]));
            return 0;
        }
    }
    return -1;
}

bool slaveio_regtable_pop_notify(slaveio_regtable_t *st, uint32_t *out_id) {
    if (st->notify_queue_used == 0) {
        return false;
    }
    uint32_t id = st->notify_queue[st->notify_queue_tail];
    st->notify_queue_tail = (uint16_t)((st->notify_queue_tail + 1) % SLAVEIO_REGTABLE_NOTIFY_QUEUE);
    st->notify_queue_used--;
    if (out_id) { *out_id = id; }
    return true;
}
