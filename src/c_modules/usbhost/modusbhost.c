/* Annealage Pod: MicroPython binding for the usbhost C module.
 *
 * Surface:
 *
 *   import usbhost
 *   usbhost.flush(force_bus_reset=True)
 *     Invalidate the descriptor cache and per-device slot table, then
 *     optionally drive a USB bus reset on the host root-hub port.
 *     Used to recover from a stuck D+ pull-up when a DUT changes USB
 *     identity in place (e.g. boot1 -> user firmware on Baochip dabao).
 *
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#include "py/runtime.h"

#include "usbhost.h"

static mp_obj_t mod_usbhost_flush(size_t n_args, const mp_obj_t *pos_args,
                                  mp_map_t *kw_args)
{
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

static mp_obj_t mod_usbhost_dwc2_hprt(void)
{
    return mp_obj_new_int_from_uint(usbhost_dwc2_hprt());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbhost_dwc2_hprt_obj, mod_usbhost_dwc2_hprt);

static mp_obj_t mod_usbhost_bus_reset(void)
{
    int rc = usbhost_bus_reset();
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbhost_bus_reset_obj, mod_usbhost_bus_reset);

#define HPRT_TRACE_BUF_LEN 256

static mp_obj_t mod_usbhost_hprt_trace(size_t n_args, const mp_obj_t *pos_args,
                                       mp_map_t *kw_args)
{
    enum { ARG_duration_ms, ARG_period_us };
    static const mp_arg_t allowed_args[] = {
        { MP_QSTR_duration_ms, MP_ARG_INT, { .u_int = 3000 } },
        { MP_QSTR_period_us,   MP_ARG_INT, { .u_int = 200 } },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];
    mp_arg_parse_all(n_args, pos_args, kw_args,
                     MP_ARRAY_SIZE(allowed_args), allowed_args, args);

    usbhost_hprt_sample_t buf[HPRT_TRACE_BUF_LEN];
    size_t n = 0;
    int rc = usbhost_hprt_trace((uint32_t)args[ARG_duration_ms].u_int,
                                (uint32_t)args[ARG_period_us].u_int,
                                buf, HPRT_TRACE_BUF_LEN, &n);
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }

    mp_obj_t list = mp_obj_new_list(0, NULL);
    for (size_t i = 0; i < n; i++) {
        mp_obj_t tup[2] = {
            mp_obj_new_int_from_uint(buf[i].t_us),
            mp_obj_new_int_from_uint(buf[i].hprt),
        };
        mp_obj_list_append(list, mp_obj_new_tuple(2, tup));
    }
    return list;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(mod_usbhost_hprt_trace_obj, 0, mod_usbhost_hprt_trace);

static const mp_rom_map_elem_t mod_usbhost_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),   MP_ROM_QSTR(MP_QSTR_usbhost) },
    { MP_ROM_QSTR(MP_QSTR_flush),      MP_ROM_PTR(&mod_usbhost_flush_obj) },
    { MP_ROM_QSTR(MP_QSTR_bus_reset),  MP_ROM_PTR(&mod_usbhost_bus_reset_obj) },
    { MP_ROM_QSTR(MP_QSTR_dwc2_hprt),  MP_ROM_PTR(&mod_usbhost_dwc2_hprt_obj) },
    { MP_ROM_QSTR(MP_QSTR_hprt_trace), MP_ROM_PTR(&mod_usbhost_hprt_trace_obj) },
};
static MP_DEFINE_CONST_DICT(mod_usbhost_globals, mod_usbhost_globals_table);

const mp_obj_module_t mod_usbhost_module = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_usbhost_globals,
};

MP_REGISTER_MODULE(MP_QSTR_usbhost, mod_usbhost_module);
