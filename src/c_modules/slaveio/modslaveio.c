// Annealage Pod: MicroPython binding for the slaveio C module.

#include "py/runtime.h"
#include "slaveio.h"

static mp_obj_t mod_slaveio_start(void) {
    slaveio_start();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_slaveio_start_obj, mod_slaveio_start);

static const mp_rom_map_elem_t mod_slaveio_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_slaveio) },
    { MP_ROM_QSTR(MP_QSTR_start), MP_ROM_PTR(&mod_slaveio_start_obj) },
};
static MP_DEFINE_CONST_DICT(mod_slaveio_globals, mod_slaveio_globals_table);

const mp_obj_module_t mod_slaveio_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_slaveio_globals,
};

MP_REGISTER_MODULE(MP_QSTR_slaveio, mod_slaveio_module);
