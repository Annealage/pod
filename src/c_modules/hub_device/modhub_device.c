/* Annealage Pod: MicroPython binding for hub_device.
 *
 * Surface:
 *
 *   import hub_device
 *   hub_device.attach()    # register the beacon device with usbip
 *
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#include "py/runtime.h"

#include "hub_device.h"

static mp_obj_t mod_hub_device_attach(void) {
    int rc = hub_device_register();
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_hub_device_attach_obj, mod_hub_device_attach);

static const mp_rom_map_elem_t mod_hub_device_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_hub_device) },
    { MP_ROM_QSTR(MP_QSTR_attach),   MP_ROM_PTR(&mod_hub_device_attach_obj) },
};
static MP_DEFINE_CONST_DICT(mod_hub_device_globals, mod_hub_device_globals_table);

const mp_obj_module_t mod_hub_device_module = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_hub_device_globals,
};

MP_REGISTER_MODULE(MP_QSTR_hub_device, mod_hub_device_module);
