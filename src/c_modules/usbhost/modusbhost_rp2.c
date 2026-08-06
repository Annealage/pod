/* Annealage Pod RP2350: MicroPython binding for the usbhost C module
 * (lwIP-RAW forwarder variant).
 *
 * Trimmed from modusbhost.c: only the recovery ops are exposed. The ESP32-S3
 * DWC2 diagnostics (dwc2_hprt / hprt_trace / ep_stats / ep0_errors) were bring-up
 * triage tools for the FreeRTOS lane/responder model and have no place in the
 * cooperative single-thread rp2 backend.
 *
 * Surface:
 *   usbhost.flush(force_bus_reset=True) -> None
 *   usbhost.bus_reset()                 -> None
 *   usbhost.reprobe()                   -> None
 *   usbhost.mounted()                   -> int  (tuh_mounted address bitmask)
 *   usbhost.cache_valid()               -> int  (desc-cache-valid address bitmask)
 */

#include "py/runtime.h"

#include <errno.h>

#include "usbhost.h"

static mp_obj_t mod_usbhost_flush(size_t n_args, const mp_obj_t *pos_args,
                                  mp_map_t *kw_args) {
    enum { ARG_force_bus_reset };
    static const mp_arg_t allowed_args[] = {
        { MP_QSTR_force_bus_reset, MP_ARG_BOOL, { .u_bool = true } },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];
    mp_arg_parse_all(n_args, pos_args, kw_args,
                     MP_ARRAY_SIZE(allowed_args), allowed_args, args);

    int rc = usbhost_flush(args[ARG_force_bus_reset].u_bool);
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(mod_usbhost_flush_obj, 0, mod_usbhost_flush);

static mp_obj_t mod_usbhost_bus_reset(void) {
    int rc = usbhost_bus_reset();
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbhost_bus_reset_obj, mod_usbhost_bus_reset);

static mp_obj_t mod_usbhost_reprobe(void) {
    int rc = usbhost_reprobe();
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbhost_reprobe_obj, mod_usbhost_reprobe);

static mp_obj_t mod_usbhost_mounted(void) {
    return mp_obj_new_int_from_uint(usbhost_mounted_mask());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbhost_mounted_obj, mod_usbhost_mounted);

static mp_obj_t mod_usbhost_cache_valid(void) {
    return mp_obj_new_int_from_uint(usbhost_cache_valid_mask());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbhost_cache_valid_obj, mod_usbhost_cache_valid);

static const mp_rom_map_elem_t mod_usbhost_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),    MP_ROM_QSTR(MP_QSTR_usbhost) },
    { MP_ROM_QSTR(MP_QSTR_flush),       MP_ROM_PTR(&mod_usbhost_flush_obj) },
    { MP_ROM_QSTR(MP_QSTR_bus_reset),   MP_ROM_PTR(&mod_usbhost_bus_reset_obj) },
    { MP_ROM_QSTR(MP_QSTR_reprobe),     MP_ROM_PTR(&mod_usbhost_reprobe_obj) },
    { MP_ROM_QSTR(MP_QSTR_mounted),     MP_ROM_PTR(&mod_usbhost_mounted_obj) },
    { MP_ROM_QSTR(MP_QSTR_cache_valid), MP_ROM_PTR(&mod_usbhost_cache_valid_obj) },
};
static MP_DEFINE_CONST_DICT(mod_usbhost_globals, mod_usbhost_globals_table);

const mp_obj_module_t mod_usbhost_module = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_usbhost_globals,
};

MP_REGISTER_MODULE(MP_QSTR_usbhost, mod_usbhost_module);
