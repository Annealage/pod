// Annealage Pod: MicroPython binding for the OTA shim.
//
// MP-side surface:
//   ops_ota.update(url, cert_pem=None)  -> True (raises OSError on failure)
//   ops_ota.mark_app_valid()            -> True/False
//
// Synchronous: the calling task blocks for the duration of the OTA.
// Caller is the MP main task; long-running C tasks must not call this.
// On success, returns True; the caller (MP boot.py) is expected to
// call machine.reset() to restart into the new slot. On failure,
// raises OSError(err) where err is the IDF esp_err_t.

#include "py/obj.h"
#include "py/runtime.h"

#include "ops_ota.h"

static mp_obj_t mod_ops_ota_update(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args) {
    enum { ARG_url, ARG_cert_pem };
    static const mp_arg_t allowed_args[] = {
        { MP_QSTR_url,      MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_rom_obj = MP_ROM_NONE} },
        { MP_QSTR_cert_pem, MP_ARG_OBJ,                   {.u_rom_obj = MP_ROM_NONE} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];
    mp_arg_parse_all(n_args, pos_args, kw_args,
                     MP_ARRAY_SIZE(allowed_args), allowed_args, args);

    const char *url = mp_obj_str_get_str(args[ARG_url].u_obj);
    const char *cert_pem = NULL;
    if (args[ARG_cert_pem].u_obj != mp_const_none) {
        cert_pem = mp_obj_str_get_str(args[ARG_cert_pem].u_obj);
    }

    esp_err_t err = ops_ota_update(url, cert_pem);
    if (err != ESP_OK) {
        mp_raise_OSError((int)err);
    }
    return mp_const_true;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(mod_ops_ota_update_obj, 1, mod_ops_ota_update);

static mp_obj_t mod_ops_ota_mark_app_valid(void) {
    esp_err_t err = ops_ota_mark_app_valid();
    return mp_obj_new_bool(err == ESP_OK);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_ops_ota_mark_app_valid_obj, mod_ops_ota_mark_app_valid);

static const mp_rom_map_elem_t mod_ops_ota_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),       MP_ROM_QSTR(MP_QSTR_ops_ota) },
    { MP_ROM_QSTR(MP_QSTR_update),         MP_ROM_PTR(&mod_ops_ota_update_obj) },
    { MP_ROM_QSTR(MP_QSTR_mark_app_valid), MP_ROM_PTR(&mod_ops_ota_mark_app_valid_obj) },
};
static MP_DEFINE_CONST_DICT(mod_ops_ota_globals, mod_ops_ota_globals_table);

const mp_obj_module_t mod_ops_ota_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_ops_ota_globals,
};

MP_REGISTER_MODULE(MP_QSTR_ops_ota, mod_ops_ota_module);
