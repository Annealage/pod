// Annealage Pod: MicroPython binding for the TCP log shim.
//
// MP-side surface:
//   ops_log.start(port=514, core=-1)  -> None (raises OSError on err)
//   ops_log.stop()                    -> None
//   ops_log.client_count()            -> int (0 or 1)

#include "py/obj.h"
#include "py/runtime.h"

#include "ops_log.h"

static void raise_on_err(esp_err_t err) {
    if (err != ESP_OK) {
        mp_raise_OSError((int)err);
    }
}

static mp_obj_t mod_ops_log_start(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args) {
    enum { ARG_port, ARG_core };
    static const mp_arg_t allowed_args[] = {
        { MP_QSTR_port, MP_ARG_INT, {.u_int = OPS_LOG_DEFAULT_PORT} },
        { MP_QSTR_core, MP_ARG_INT, {.u_int = -1} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];
    mp_arg_parse_all(n_args, pos_args, kw_args,
                     MP_ARRAY_SIZE(allowed_args), allowed_args, args);

    raise_on_err(ops_log_start((uint16_t)args[ARG_port].u_int,
                               args[ARG_core].u_int));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(mod_ops_log_start_obj, 0, mod_ops_log_start);

static mp_obj_t mod_ops_log_stop(void) {
    raise_on_err(ops_log_stop());
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_ops_log_stop_obj, mod_ops_log_stop);

static mp_obj_t mod_ops_log_client_count(void) {
    return MP_OBJ_NEW_SMALL_INT(ops_log_client_count());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_ops_log_client_count_obj, mod_ops_log_client_count);

static const mp_rom_map_elem_t mod_ops_log_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),     MP_ROM_QSTR(MP_QSTR_ops_log) },
    { MP_ROM_QSTR(MP_QSTR_start),        MP_ROM_PTR(&mod_ops_log_start_obj) },
    { MP_ROM_QSTR(MP_QSTR_stop),         MP_ROM_PTR(&mod_ops_log_stop_obj) },
    { MP_ROM_QSTR(MP_QSTR_client_count), MP_ROM_PTR(&mod_ops_log_client_count_obj) },
    { MP_ROM_QSTR(MP_QSTR_DEFAULT_PORT), MP_ROM_INT(OPS_LOG_DEFAULT_PORT) },
};
static MP_DEFINE_CONST_DICT(mod_ops_log_globals, mod_ops_log_globals_table);

const mp_obj_module_t mod_ops_log_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_ops_log_globals,
};

MP_REGISTER_MODULE(MP_QSTR_ops_log, mod_ops_log_module);
