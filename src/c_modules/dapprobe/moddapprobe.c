/* Annealage Pod: MicroPython binding for the dapprobe C module.
 *
 * Surface (per docs/design/cmsis-dap.md §7):
 *
 *   import dapprobe
 *   dapprobe.attach()                # registers synthetic device on bus 2
 *   dapprobe.is_attached()           # bool
 *   dapprobe.is_initialised()        # bool
 *   dapprobe.swd_clock_hz()          # current realised SWD clock
 *   dapprobe.swo_overruns()          # tier-1 -> tier-2 overrun count
 *   dapprobe.transfers_total()       # SWD transfer count
 *   dapprobe.swo_bytes_buffered()    # tier-1 + tier-2 unread bytes
 *   dapprobe.serial()                # 12 hex chars derived from MAC
 *   dapprobe.detach()                # soft detach (flag-flip; registry
 *                                    #  is append-only in rev1)
 *
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#include "py/runtime.h"
#include "py/objstr.h"

#include "dap_core.h"
#include "io/swd.h"

static mp_obj_t mod_dapprobe_attach(void) {
    int rc = dap_core_attach();
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_dapprobe_attach_obj, mod_dapprobe_attach);

static mp_obj_t mod_dapprobe_detach(void) {
    /* The WS-A virtual_device registry is append-only in rev1. We
     * simply mark the module as detached at the dap_core level; the
     * synthetic device stays registered until reboot. */
    (void)dap_core_deinit();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_dapprobe_detach_obj, mod_dapprobe_detach);

static mp_obj_t mod_dapprobe_is_attached(void) {
    return mp_obj_new_bool(dap_core_is_attached());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_dapprobe_is_attached_obj, mod_dapprobe_is_attached);

static mp_obj_t mod_dapprobe_is_initialised(void) {
    return mp_obj_new_bool(dap_core_is_initialised());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_dapprobe_is_initialised_obj, mod_dapprobe_is_initialised);

static mp_obj_t mod_dapprobe_swd_clock_hz(void) {
    dap_core_telemetry_t t = {0};
    dap_core_telemetry(&t);
    return mp_obj_new_int_from_uint(t.swd_clock_hz);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_dapprobe_swd_clock_hz_obj, mod_dapprobe_swd_clock_hz);

static mp_obj_t mod_dapprobe_swo_overruns(void) {
    dap_core_telemetry_t t = {0};
    dap_core_telemetry(&t);
    return mp_obj_new_int_from_uint(t.swo_overruns_total);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_dapprobe_swo_overruns_obj, mod_dapprobe_swo_overruns);

static mp_obj_t mod_dapprobe_transfers_total(void) {
    dap_core_telemetry_t t = {0};
    dap_core_telemetry(&t);
    return mp_obj_new_int_from_uint(t.swd_transfers_total);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_dapprobe_transfers_total_obj, mod_dapprobe_transfers_total);

static mp_obj_t mod_dapprobe_swo_bytes_buffered(void) {
    dap_core_telemetry_t t = {0};
    dap_core_telemetry(&t);
    return mp_obj_new_int_from_uint((mp_uint_t)t.swo_bytes_buffered);
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_dapprobe_swo_bytes_buffered_obj, mod_dapprobe_swo_bytes_buffered);

static mp_obj_t mod_dapprobe_serial(void) {
    const char *s = dap_core_serial_string();
    if (s == NULL) {
        return mp_const_none;
    }
    return mp_obj_new_str(s, strlen(s));
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_dapprobe_serial_obj, mod_dapprobe_serial);

static mp_obj_t mod_dapprobe_set_swd_trace(mp_obj_t count_obj) {
    mp_int_t count = mp_obj_get_int(count_obj);
    if (count < 0) {
        count = 0;
    }
    swd_set_trace((uint32_t)count);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_dapprobe_set_swd_trace_obj, mod_dapprobe_set_swd_trace);

static const mp_rom_map_elem_t mod_dapprobe_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),            MP_ROM_QSTR(MP_QSTR_dapprobe) },
    { MP_ROM_QSTR(MP_QSTR_attach),              MP_ROM_PTR(&mod_dapprobe_attach_obj) },
    { MP_ROM_QSTR(MP_QSTR_detach),              MP_ROM_PTR(&mod_dapprobe_detach_obj) },
    { MP_ROM_QSTR(MP_QSTR_is_attached),         MP_ROM_PTR(&mod_dapprobe_is_attached_obj) },
    { MP_ROM_QSTR(MP_QSTR_is_initialised),      MP_ROM_PTR(&mod_dapprobe_is_initialised_obj) },
    { MP_ROM_QSTR(MP_QSTR_swd_clock_hz),        MP_ROM_PTR(&mod_dapprobe_swd_clock_hz_obj) },
    { MP_ROM_QSTR(MP_QSTR_swo_overruns),        MP_ROM_PTR(&mod_dapprobe_swo_overruns_obj) },
    { MP_ROM_QSTR(MP_QSTR_transfers_total),     MP_ROM_PTR(&mod_dapprobe_transfers_total_obj) },
    { MP_ROM_QSTR(MP_QSTR_swo_bytes_buffered),  MP_ROM_PTR(&mod_dapprobe_swo_bytes_buffered_obj) },
    { MP_ROM_QSTR(MP_QSTR_serial),              MP_ROM_PTR(&mod_dapprobe_serial_obj) },
    { MP_ROM_QSTR(MP_QSTR_set_swd_trace),       MP_ROM_PTR(&mod_dapprobe_set_swd_trace_obj) },
};
static MP_DEFINE_CONST_DICT(mod_dapprobe_globals, mod_dapprobe_globals_table);

const mp_obj_module_t mod_dapprobe_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_dapprobe_globals,
};

MP_REGISTER_MODULE(MP_QSTR_dapprobe, mod_dapprobe_module);
