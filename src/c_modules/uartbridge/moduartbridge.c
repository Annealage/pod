// Annealage Pod: MicroPython binding for the uartbridge C module.

#include "py/runtime.h"
#include "uart_bridge.h"

static mp_obj_t mod_uartbridge_start(void) {
    uart_bridge_start();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_uartbridge_start_obj, mod_uartbridge_start);

static const mp_rom_map_elem_t mod_uartbridge_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_uartbridge) },
    { MP_ROM_QSTR(MP_QSTR_start), MP_ROM_PTR(&mod_uartbridge_start_obj) },
};
static MP_DEFINE_CONST_DICT(mod_uartbridge_globals, mod_uartbridge_globals_table);

const mp_obj_module_t mod_uartbridge_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_uartbridge_globals,
};

MP_REGISTER_MODULE(MP_QSTR_uartbridge, mod_uartbridge_module);
