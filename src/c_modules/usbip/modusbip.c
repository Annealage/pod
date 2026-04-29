/* Annealage Pod: MicroPython binding for the usbip C module.
 *
 * Phase 2 surface:
 *   usbip.start(port=3240)        -> None  (idempotent)
 *   usbip.stop()                  -> None  (idempotent)
 *   usbip.attached_devices()      -> list[str]   currently-imported busids
 *
 * Phase 3 P3.0.1 additions:
 *   usbip.set_verbose(enable=True) -> None
 *      Toggles per-URB ESP_LOGI tracing across all three layers
 *      (usbip server, usbhost backend, dapprobe synthetic responder).
 *      Default off so production logs stay quiet.
 *   usbip.is_verbose()            -> bool
 *
 * Larger administration (registering synthetic devices) is the
 * dapprobe module's concern in WS-C; that path goes through the C
 * `usbip_server_register_virtual_device()` entry, not Python.
 */

#include "py/runtime.h"
#include "py/objstr.h"

#include "usbip_protocol.h"
#include "usbip_server.h"
#include "virtual_device.h"

#include "../usbhost/usbhost.h"
#include "../dapprobe/synthetic_device.h"

static mp_obj_t mod_usbip_start(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args)
{
    static const mp_arg_t allowed[] = {
        { MP_QSTR_port, MP_ARG_INT, { .u_int = USBIP_TCP_PORT } },
    };
    mp_arg_val_t vals[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, vals);

    int port = vals[0].u_int;
    if (port < 1 || port > 65535) {
        mp_raise_ValueError(MP_ERROR_TEXT("port out of range"));
    }
    int rc = usbip_server_start((uint16_t)port);
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(mod_usbip_start_obj, 0, mod_usbip_start);

static mp_obj_t mod_usbip_stop(void)
{
    int rc = usbip_server_stop();
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbip_stop_obj, mod_usbip_stop);

static mp_obj_t mod_usbip_is_running(void)
{
    return mp_obj_new_bool(usbip_server_is_running());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbip_is_running_obj, mod_usbip_is_running);

static mp_obj_t mod_usbip_attached_devices(void)
{
    char busids[8][USBIP_BUSID_SIZE];
    size_t n = usbip_server_attached_busids(busids,
                                            sizeof(busids) / sizeof(busids[0]));
    mp_obj_t list = mp_obj_new_list(0, NULL);
    for (size_t i = 0; i < n; i++) {
        /* busid strings are NUL-padded to 32 bytes; strnlen avoids
         * including the trailing zeros in the Python string. */
        size_t len = strnlen(busids[i], USBIP_BUSID_SIZE);
        mp_obj_list_append(list, mp_obj_new_str(busids[i], len));
    }
    return list;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbip_attached_devices_obj, mod_usbip_attached_devices);

static mp_obj_t mod_usbip_set_verbose(size_t n_args, const mp_obj_t *args)
{
    bool enable = (n_args == 0) ? true : mp_obj_is_true(args[0]);
    /* Toggle the trace flag at all three layers: usbip server, the
     * usbhost backend (real DUT URB path), and the dapprobe synthetic
     * responder (CMSIS-DAP-v2 path). */
    usbip_server_set_verbose(enable);
    usbhost_set_verbose(enable);
    synthetic_device_set_verbose(enable);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_usbip_set_verbose_obj, 0, 1, mod_usbip_set_verbose);

static mp_obj_t mod_usbip_is_verbose(void)
{
    return mp_obj_new_bool(usbip_server_is_verbose());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbip_is_verbose_obj, mod_usbip_is_verbose);

static const mp_rom_map_elem_t mod_usbip_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),          MP_ROM_QSTR(MP_QSTR_usbip) },
    { MP_ROM_QSTR(MP_QSTR_start),             MP_ROM_PTR(&mod_usbip_start_obj) },
    { MP_ROM_QSTR(MP_QSTR_stop),              MP_ROM_PTR(&mod_usbip_stop_obj) },
    { MP_ROM_QSTR(MP_QSTR_is_running),        MP_ROM_PTR(&mod_usbip_is_running_obj) },
    { MP_ROM_QSTR(MP_QSTR_attached_devices),  MP_ROM_PTR(&mod_usbip_attached_devices_obj) },
    { MP_ROM_QSTR(MP_QSTR_set_verbose),       MP_ROM_PTR(&mod_usbip_set_verbose_obj) },
    { MP_ROM_QSTR(MP_QSTR_is_verbose),        MP_ROM_PTR(&mod_usbip_is_verbose_obj) },
};
static MP_DEFINE_CONST_DICT(mod_usbip_globals, mod_usbip_globals_table);

const mp_obj_module_t mod_usbip_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_usbip_globals,
};

MP_REGISTER_MODULE(MP_QSTR_usbip, mod_usbip_module);
