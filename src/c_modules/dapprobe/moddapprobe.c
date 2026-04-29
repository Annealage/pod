// Annealage Pod: MicroPython binding for the dapprobe C module.
//
// Phase 1 surface: `dapprobe.start()` logs a line and returns None.

#include "py/runtime.h"
#include "dap_core.h"

static mp_obj_t mod_dapprobe_start(void) {
    dap_core_start();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_dapprobe_start_obj, mod_dapprobe_start);

static const mp_rom_map_elem_t mod_dapprobe_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_dapprobe) },
    { MP_ROM_QSTR(MP_QSTR_start), MP_ROM_PTR(&mod_dapprobe_start_obj) },
};
static MP_DEFINE_CONST_DICT(mod_dapprobe_globals, mod_dapprobe_globals_table);

const mp_obj_module_t mod_dapprobe_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_dapprobe_globals,
};

MP_REGISTER_MODULE(MP_QSTR_dapprobe, mod_dapprobe_module);
