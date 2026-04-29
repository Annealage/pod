// Annealage Pod: MicroPython binding for the slaveio C module.
//
// Surface (matches src/mpy/annealage_pod/slave.py thin wrapper expectations):
//
//   slaveio.start()
//       One-shot init (called by the MP wrapper's _ensure_started()).
//       Idempotent.
//
//   slaveio.i2c_start(addr, read_size=256, write_size=256,
//                     sda=-1, scl=-1, dir=-1)              -> None
//   slaveio.i2c_stop()                                      -> None
//   slaveio.i2c_read_table()       -> bytearray            (zero-copy view)
//   slaveio.i2c_write_table()      -> bytearray            (zero-copy view)
//   slaveio.i2c_on_write(start, end, callback)              -> None
//   slaveio.i2c_off_write(callback)                         -> None
//   slaveio.i2c_status()                                    -> dict
//
//   slaveio.spi_start(mode=0, freq_max=10_000_000,
//                     read_size=256, write_size=256,
//                     miso=-1, mosi=-1, sck=-1, cs=-1, dir=-1) -> None
//   slaveio.spi_stop()                                      -> None
//   slaveio.spi_read_table()                                -> bytearray
//   slaveio.spi_write_table()                               -> bytearray
//   slaveio.spi_on_read(start, end, callback)               -> None
//   slaveio.spi_on_write(start, end, callback)              -> None
//   slaveio.spi_off_write(callback)                         -> None
//   slaveio.spi_status()                                    -> dict
//
// On any C-side failure we raise OSError with the ESP error code.
//
// Notify dispatch path
// --------------------
// slaveio.c posts callback ids onto a non-ISR FreeRTOS task. The task
// calls back into a registered C function (slaveio_dispatch_thunk
// below) which schedules the MP callback via mp_sched_schedule(). This
// keeps the MP runtime out of any ISR or critical section.

#include <string.h>

#include "py/obj.h"
#include "py/objarray.h"
#include "py/runtime.h"
#include "py/mphal.h"
#include "py/mperrno.h"
#include "py/binary.h"

#include "slaveio.h"

// ---------------------------------------------------------------------
// Callback registry: maps callback_id <-> mp_obj_t (Python callable)
// ---------------------------------------------------------------------

#define MOD_SLAVEIO_MAX_CB 16

typedef struct {
    bool used;
    uint32_t id;
    mp_obj_t callable;
} mod_slaveio_cb_slot_t;

static mod_slaveio_cb_slot_t s_cb_slots[MOD_SLAVEIO_MAX_CB];
static uint32_t s_next_cb_id = 1;

static int cb_alloc_slot(mp_obj_t fn, uint32_t *out_id) {
    for (int i = 0; i < MOD_SLAVEIO_MAX_CB; ++i) {
        if (s_cb_slots[i].used && s_cb_slots[i].callable == fn) {
            *out_id = s_cb_slots[i].id;
            return i;
        }
    }
    for (int i = 0; i < MOD_SLAVEIO_MAX_CB; ++i) {
        if (!s_cb_slots[i].used) {
            s_cb_slots[i].used = true;
            s_cb_slots[i].id = s_next_cb_id++;
            s_cb_slots[i].callable = fn;
            *out_id = s_cb_slots[i].id;
            return i;
        }
    }
    return -1;
}

static int cb_find_id_by_callable(mp_obj_t fn, uint32_t *out_id) {
    for (int i = 0; i < MOD_SLAVEIO_MAX_CB; ++i) {
        if (s_cb_slots[i].used && s_cb_slots[i].callable == fn) {
            *out_id = s_cb_slots[i].id;
            return i;
        }
    }
    return -1;
}

static mp_obj_t cb_lookup(uint32_t id) {
    for (int i = 0; i < MOD_SLAVEIO_MAX_CB; ++i) {
        if (s_cb_slots[i].used && s_cb_slots[i].id == id) {
            return s_cb_slots[i].callable;
        }
    }
    return MP_OBJ_NULL;
}

static void cb_release(uint32_t id) {
    for (int i = 0; i < MOD_SLAVEIO_MAX_CB; ++i) {
        if (s_cb_slots[i].used && s_cb_slots[i].id == id) {
            s_cb_slots[i].used = false;
            s_cb_slots[i].callable = MP_OBJ_NULL;
            return;
        }
    }
}

// ---------------------------------------------------------------------
// Dispatch wiring
// ---------------------------------------------------------------------

static mp_obj_t mod_slaveio_dispatch_one(mp_obj_t id_obj) {
    uint32_t id = (uint32_t)mp_obj_get_int(id_obj);
    mp_obj_t cb = cb_lookup(id);
    if (cb != MP_OBJ_NULL) {
        mp_call_function_0(cb);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_slaveio_dispatch_one_obj, mod_slaveio_dispatch_one);

static void slaveio_dispatch_thunk(uint32_t id) {
    // Called from the slaveio dispatch task (non-ISR). Schedule the
    // Python callable on the MP main task; do not invoke directly so
    // we are not constrained by GIL state on this thread.
    mp_sched_schedule((mp_obj_t)&mod_slaveio_dispatch_one_obj,
                       mp_obj_new_int_from_uint(id));
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

static void raise_on_err(esp_err_t err) {
    if (err != ESP_OK) {
        mp_raise_OSError((int)err);
    }
}

// Build a bytearray that aliases the C-side buffer (no copy). The
// returned object reads and writes the same memory the slave ISR uses.
static mp_obj_t buf_to_bytearray(uint8_t *buf, size_t size) {
    if (buf == NULL || size == 0) {
        return mp_obj_new_bytearray(0, NULL);
    }
    return mp_obj_new_bytearray_by_ref(size, buf);
}

static mp_obj_t status_to_dict(const slaveio_status_t *s) {
    mp_obj_t d = mp_obj_new_dict(0);
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_active),
                     mp_obj_new_bool(s->active));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_transfers_total),
                     mp_obj_new_int_from_ull(s->transfers_total));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_overflow_count),
                     mp_obj_new_int_from_uint(s->overflow_count));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_last_offset),
                     mp_obj_new_int_from_uint(s->last_offset));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_last_length),
                     mp_obj_new_int_from_uint(s->last_length));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_last_was_write),
                     mp_obj_new_bool(s->last_was_write));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_reg_ptr),
                     mp_obj_new_int_from_uint(s->reg_ptr));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_notify_dropped),
                     mp_obj_new_int_from_uint(s->notify_queue_dropped));
    return d;
}

// ---------------------------------------------------------------------
// MP entry points
// ---------------------------------------------------------------------

static mp_obj_t mod_slaveio_start(void) {
    raise_on_err(slaveio_init());
    slaveio_set_dispatch_fn(slaveio_dispatch_thunk);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_slaveio_start_obj, mod_slaveio_start);

// I2C ------------------------------------------------------------------

static mp_obj_t mod_slaveio_i2c_start(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args) {
    enum { ARG_addr, ARG_read_size, ARG_write_size, ARG_sda, ARG_scl, ARG_dir };
    static const mp_arg_t allowed[] = {
        { MP_QSTR_addr,       MP_ARG_INT | MP_ARG_REQUIRED, {.u_int = 0} },
        { MP_QSTR_read_size,  MP_ARG_INT, {.u_int = 256} },
        { MP_QSTR_write_size, MP_ARG_INT, {.u_int = 256} },
        { MP_QSTR_sda,        MP_ARG_INT, {.u_int = -1} },
        { MP_QSTR_scl,        MP_ARG_INT, {.u_int = -1} },
        { MP_QSTR_dir,        MP_ARG_INT, {.u_int = -1} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, args);

    raise_on_err(slaveio_i2c_start((uint16_t)args[ARG_addr].u_int,
                                    args[ARG_sda].u_int,
                                    args[ARG_scl].u_int,
                                    args[ARG_dir].u_int,
                                    (size_t)args[ARG_read_size].u_int,
                                    (size_t)args[ARG_write_size].u_int));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(mod_slaveio_i2c_start_obj, 1, mod_slaveio_i2c_start);

static mp_obj_t mod_slaveio_i2c_stop(void) {
    raise_on_err(slaveio_i2c_stop());
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_slaveio_i2c_stop_obj, mod_slaveio_i2c_stop);

static mp_obj_t mod_slaveio_i2c_read_table(void) {
    uint8_t *buf = NULL; size_t size = 0;
    raise_on_err(slaveio_i2c_get_read_buf(&buf, &size));
    return buf_to_bytearray(buf, size);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_slaveio_i2c_read_table_obj, mod_slaveio_i2c_read_table);

static mp_obj_t mod_slaveio_i2c_write_table(void) {
    uint8_t *buf = NULL; size_t size = 0;
    raise_on_err(slaveio_i2c_get_write_buf(&buf, &size));
    return buf_to_bytearray(buf, size);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_slaveio_i2c_write_table_obj, mod_slaveio_i2c_write_table);

static mp_obj_t mod_slaveio_i2c_on_write(mp_obj_t start_o, mp_obj_t end_o, mp_obj_t cb_o) {
    if (!mp_obj_is_callable(cb_o)) {
        mp_raise_TypeError(MP_ERROR_TEXT("callback must be callable"));
    }
    uint32_t id;
    if (cb_alloc_slot(cb_o, &id) < 0) {
        mp_raise_OSError(MP_ENOMEM);
    }
    esp_err_t err = slaveio_i2c_register_notify((uint32_t)mp_obj_get_int(start_o),
                                                 (uint32_t)mp_obj_get_int(end_o),
                                                 id);
    if (err != ESP_OK) {
        cb_release(id);
        raise_on_err(err);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(mod_slaveio_i2c_on_write_obj, mod_slaveio_i2c_on_write);

static mp_obj_t mod_slaveio_i2c_off_write(mp_obj_t cb_o) {
    uint32_t id;
    if (cb_find_id_by_callable(cb_o, &id) < 0) {
        return mp_const_none;
    }
    slaveio_i2c_unregister_notify(id);
    cb_release(id);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_slaveio_i2c_off_write_obj, mod_slaveio_i2c_off_write);

static mp_obj_t mod_slaveio_i2c_status(void) {
    slaveio_status_t s;
    raise_on_err(slaveio_i2c_status(&s));
    return status_to_dict(&s);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_slaveio_i2c_status_obj, mod_slaveio_i2c_status);

// SPI ------------------------------------------------------------------

static mp_obj_t mod_slaveio_spi_start(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args) {
    enum {
        ARG_mode, ARG_freq_max, ARG_read_size, ARG_write_size,
        ARG_miso, ARG_mosi, ARG_sck, ARG_cs, ARG_dir,
    };
    static const mp_arg_t allowed[] = {
        { MP_QSTR_mode,       MP_ARG_INT, {.u_int = 0} },
        { MP_QSTR_freq_max,   MP_ARG_INT, {.u_int = 10000000} },
        { MP_QSTR_read_size,  MP_ARG_INT, {.u_int = 256} },
        { MP_QSTR_write_size, MP_ARG_INT, {.u_int = 256} },
        { MP_QSTR_miso,       MP_ARG_INT, {.u_int = -1} },
        { MP_QSTR_mosi,       MP_ARG_INT, {.u_int = -1} },
        { MP_QSTR_sck,        MP_ARG_INT, {.u_int = -1} },
        { MP_QSTR_cs,         MP_ARG_INT, {.u_int = -1} },
        { MP_QSTR_dir,        MP_ARG_INT, {.u_int = -1} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, args);

    raise_on_err(slaveio_spi_start((uint8_t)args[ARG_mode].u_int,
                                    (uint32_t)args[ARG_freq_max].u_int,
                                    args[ARG_miso].u_int,
                                    args[ARG_mosi].u_int,
                                    args[ARG_sck].u_int,
                                    args[ARG_cs].u_int,
                                    args[ARG_dir].u_int,
                                    (size_t)args[ARG_read_size].u_int,
                                    (size_t)args[ARG_write_size].u_int));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(mod_slaveio_spi_start_obj, 0, mod_slaveio_spi_start);

static mp_obj_t mod_slaveio_spi_stop(void) {
    raise_on_err(slaveio_spi_stop());
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_slaveio_spi_stop_obj, mod_slaveio_spi_stop);

static mp_obj_t mod_slaveio_spi_read_table(void) {
    uint8_t *buf = NULL; size_t size = 0;
    raise_on_err(slaveio_spi_get_read_buf(&buf, &size));
    return buf_to_bytearray(buf, size);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_slaveio_spi_read_table_obj, mod_slaveio_spi_read_table);

static mp_obj_t mod_slaveio_spi_write_table(void) {
    uint8_t *buf = NULL; size_t size = 0;
    raise_on_err(slaveio_spi_get_write_buf(&buf, &size));
    return buf_to_bytearray(buf, size);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_slaveio_spi_write_table_obj, mod_slaveio_spi_write_table);

static mp_obj_t mod_slaveio_spi_on_write(mp_obj_t start_o, mp_obj_t end_o, mp_obj_t cb_o) {
    if (!mp_obj_is_callable(cb_o)) {
        mp_raise_TypeError(MP_ERROR_TEXT("callback must be callable"));
    }
    uint32_t id;
    if (cb_alloc_slot(cb_o, &id) < 0) {
        mp_raise_OSError(MP_ENOMEM);
    }
    esp_err_t err = slaveio_spi_register_notify((uint32_t)mp_obj_get_int(start_o),
                                                 (uint32_t)mp_obj_get_int(end_o),
                                                 id);
    if (err != ESP_OK) {
        cb_release(id);
        raise_on_err(err);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_3(mod_slaveio_spi_on_write_obj, mod_slaveio_spi_on_write);

// SPI shape mirrors I2C; the WS-E wrapper also exposes spi.on_read for
// notifies on the read-table (master-clocks-out path). The current
// implementation does not post read-side notifies (per spec §4.8 the
// callback fires on writes only); on_read is wired here for symmetry
// and treats the registered range as a write notify so the callback
// mechanism is uniform.
static mp_obj_t mod_slaveio_spi_on_read(mp_obj_t start_o, mp_obj_t end_o, mp_obj_t cb_o) {
    return mod_slaveio_spi_on_write(start_o, end_o, cb_o);
}
static MP_DEFINE_CONST_FUN_OBJ_3(mod_slaveio_spi_on_read_obj, mod_slaveio_spi_on_read);

static mp_obj_t mod_slaveio_spi_off_write(mp_obj_t cb_o) {
    uint32_t id;
    if (cb_find_id_by_callable(cb_o, &id) < 0) {
        return mp_const_none;
    }
    slaveio_spi_unregister_notify(id);
    cb_release(id);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_slaveio_spi_off_write_obj, mod_slaveio_spi_off_write);

static mp_obj_t mod_slaveio_spi_status(void) {
    slaveio_status_t s;
    raise_on_err(slaveio_spi_status(&s));
    return status_to_dict(&s);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_slaveio_spi_status_obj, mod_slaveio_spi_status);

// ---------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------

static const mp_rom_map_elem_t mod_slaveio_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),         MP_ROM_QSTR(MP_QSTR_slaveio) },

    { MP_ROM_QSTR(MP_QSTR_start),            MP_ROM_PTR(&mod_slaveio_start_obj) },

    { MP_ROM_QSTR(MP_QSTR_i2c_start),        MP_ROM_PTR(&mod_slaveio_i2c_start_obj) },
    { MP_ROM_QSTR(MP_QSTR_i2c_stop),         MP_ROM_PTR(&mod_slaveio_i2c_stop_obj) },
    { MP_ROM_QSTR(MP_QSTR_i2c_read_table),   MP_ROM_PTR(&mod_slaveio_i2c_read_table_obj) },
    { MP_ROM_QSTR(MP_QSTR_i2c_write_table),  MP_ROM_PTR(&mod_slaveio_i2c_write_table_obj) },
    { MP_ROM_QSTR(MP_QSTR_i2c_on_write),     MP_ROM_PTR(&mod_slaveio_i2c_on_write_obj) },
    { MP_ROM_QSTR(MP_QSTR_i2c_off_write),    MP_ROM_PTR(&mod_slaveio_i2c_off_write_obj) },
    { MP_ROM_QSTR(MP_QSTR_i2c_status),       MP_ROM_PTR(&mod_slaveio_i2c_status_obj) },

    { MP_ROM_QSTR(MP_QSTR_spi_start),        MP_ROM_PTR(&mod_slaveio_spi_start_obj) },
    { MP_ROM_QSTR(MP_QSTR_spi_stop),         MP_ROM_PTR(&mod_slaveio_spi_stop_obj) },
    { MP_ROM_QSTR(MP_QSTR_spi_read_table),   MP_ROM_PTR(&mod_slaveio_spi_read_table_obj) },
    { MP_ROM_QSTR(MP_QSTR_spi_write_table),  MP_ROM_PTR(&mod_slaveio_spi_write_table_obj) },
    { MP_ROM_QSTR(MP_QSTR_spi_on_write),     MP_ROM_PTR(&mod_slaveio_spi_on_write_obj) },
    { MP_ROM_QSTR(MP_QSTR_spi_on_read),      MP_ROM_PTR(&mod_slaveio_spi_on_read_obj) },
    { MP_ROM_QSTR(MP_QSTR_spi_off_write),    MP_ROM_PTR(&mod_slaveio_spi_off_write_obj) },
    { MP_ROM_QSTR(MP_QSTR_spi_status),       MP_ROM_PTR(&mod_slaveio_spi_status_obj) },
};
static MP_DEFINE_CONST_DICT(mod_slaveio_globals, mod_slaveio_globals_table);

const mp_obj_module_t mod_slaveio_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_slaveio_globals,
};

MP_REGISTER_MODULE(MP_QSTR_slaveio, mod_slaveio_module);
