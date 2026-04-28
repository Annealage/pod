// Annealage Pod: MicroPython binding for the usbip C module.
//
// Phase 1 surface: `usbip.start()` logs a line and returns None.

#include "py/runtime.h"
#include "usbip_server.h"

static mp_obj_t mod_usbip_start(void) {
    usbip_server_start();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_usbip_start_obj, mod_usbip_start);

static const mp_rom_map_elem_t mod_usbip_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_usbip) },
    { MP_ROM_QSTR(MP_QSTR_start), MP_ROM_PTR(&mod_usbip_start_obj) },
};
static MP_DEFINE_CONST_DICT(mod_usbip_globals, mod_usbip_globals_table);

const mp_obj_module_t mod_usbip_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_usbip_globals,
};

MP_REGISTER_MODULE(MP_QSTR_usbip, mod_usbip_module);
