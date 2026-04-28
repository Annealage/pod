// Annealage Pod: MicroPython binding for the task watchdog shim.
//
// MP-side surface:
//   ops_wdt.subscribe(timeout_ms=30000) -> None (raises OSError on err)
//   ops_wdt.kick()                      -> None (raises OSError on err)
//   ops_wdt.unsubscribe()               -> None (raises OSError on err)
//   ops_wdt.is_subscribed()             -> bool

#include "py/obj.h"
#include "py/runtime.h"

#include "ops_wdt.h"

static void raise_on_err(esp_err_t err) {
    if (err != ESP_OK) {
        mp_raise_OSError((int)err);
    }
}

static mp_obj_t mod_ops_wdt_subscribe(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args) {
    enum { ARG_timeout_ms };
    static const mp_arg_t allowed_args[] = {
        { MP_QSTR_timeout_ms, MP_ARG_INT, {.u_int = 30000} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];
    mp_arg_parse_all(n_args, pos_args, kw_args,
                     MP_ARRAY_SIZE(allowed_args), allowed_args, args);

    raise_on_err(ops_wdt_subscribe((uint32_t)args[ARG_timeout_ms].u_int));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(mod_ops_wdt_subscribe_obj, 0, mod_ops_wdt_subscribe);

static mp_obj_t mod_ops_wdt_kick(void) {
    raise_on_err(ops_wdt_kick());
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_ops_wdt_kick_obj, mod_ops_wdt_kick);

static mp_obj_t mod_ops_wdt_unsubscribe(void) {
    raise_on_err(ops_wdt_unsubscribe());
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_ops_wdt_unsubscribe_obj, mod_ops_wdt_unsubscribe);

static mp_obj_t mod_ops_wdt_is_subscribed(void) {
    return mp_obj_new_bool(ops_wdt_is_subscribed());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_ops_wdt_is_subscribed_obj, mod_ops_wdt_is_subscribed);

static const mp_rom_map_elem_t mod_ops_wdt_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),       MP_ROM_QSTR(MP_QSTR_ops_wdt) },
    { MP_ROM_QSTR(MP_QSTR_subscribe),      MP_ROM_PTR(&mod_ops_wdt_subscribe_obj) },
    { MP_ROM_QSTR(MP_QSTR_kick),           MP_ROM_PTR(&mod_ops_wdt_kick_obj) },
    { MP_ROM_QSTR(MP_QSTR_unsubscribe),    MP_ROM_PTR(&mod_ops_wdt_unsubscribe_obj) },
    { MP_ROM_QSTR(MP_QSTR_is_subscribed),  MP_ROM_PTR(&mod_ops_wdt_is_subscribed_obj) },
};
static MP_DEFINE_CONST_DICT(mod_ops_wdt_globals, mod_ops_wdt_globals_table);

const mp_obj_module_t mod_ops_wdt_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_ops_wdt_globals,
};

MP_REGISTER_MODULE(MP_QSTR_ops_wdt, mod_ops_wdt_module);
