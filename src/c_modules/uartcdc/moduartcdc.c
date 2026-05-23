/* Annealage Pod: MicroPython binding for the uartcdc C module.
 *
 * Surface:
 *   import uartcdc
 *   uartcdc.attach(uart_num, tx_pin, rx_pin[, baud=115200])
 *   uartcdc.detach()
 *   uartcdc.is_attached()   -> bool
 *   uartcdc.set_verbose(bool)
 *   uartcdc.is_verbose()    -> bool
 *
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#include "py/runtime.h"

#include "uart_cdc_device.h"

static mp_obj_t mod_uartcdc_attach(size_t n_args, const mp_obj_t *args)
{
    mp_int_t uart_num = mp_obj_get_int(args[0]);
    mp_int_t tx_pin   = mp_obj_get_int(args[1]);
    mp_int_t rx_pin   = mp_obj_get_int(args[2]);
    mp_int_t baud     = (n_args >= 4) ? mp_obj_get_int(args[3]) : 115200;
    int rc = uart_cdc_attach((int)uart_num, (int)tx_pin, (int)rx_pin, (int)baud);
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_uartcdc_attach_obj, 3, 4,
                                            mod_uartcdc_attach);

static mp_obj_t mod_uartcdc_detach(void)
{
    int rc = uart_cdc_detach();
    if (rc != 0) {
        mp_raise_OSError(-rc);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_uartcdc_detach_obj, mod_uartcdc_detach);

static mp_obj_t mod_uartcdc_is_attached(void)
{
    return mp_obj_new_bool(uart_cdc_is_attached());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_uartcdc_is_attached_obj, mod_uartcdc_is_attached);

static mp_obj_t mod_uartcdc_set_verbose(mp_obj_t v)
{
    uart_cdc_set_verbose(mp_obj_is_true(v));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_uartcdc_set_verbose_obj, mod_uartcdc_set_verbose);

static mp_obj_t mod_uartcdc_is_verbose(void)
{
    return mp_obj_new_bool(uart_cdc_is_verbose());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_uartcdc_is_verbose_obj, mod_uartcdc_is_verbose);

static mp_obj_t mod_uartcdc_flush_rx(void)
{
    uart_cdc_flush_rx();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_uartcdc_flush_rx_obj, mod_uartcdc_flush_rx);

static const mp_rom_map_elem_t mod_uartcdc_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),    MP_ROM_QSTR(MP_QSTR_uartcdc)           },
    { MP_ROM_QSTR(MP_QSTR_attach),      MP_ROM_PTR(&mod_uartcdc_attach_obj)    },
    { MP_ROM_QSTR(MP_QSTR_detach),      MP_ROM_PTR(&mod_uartcdc_detach_obj)    },
    { MP_ROM_QSTR(MP_QSTR_is_attached), MP_ROM_PTR(&mod_uartcdc_is_attached_obj) },
    { MP_ROM_QSTR(MP_QSTR_set_verbose), MP_ROM_PTR(&mod_uartcdc_set_verbose_obj) },
    { MP_ROM_QSTR(MP_QSTR_is_verbose),  MP_ROM_PTR(&mod_uartcdc_is_verbose_obj)  },
    { MP_ROM_QSTR(MP_QSTR_flush_rx),    MP_ROM_PTR(&mod_uartcdc_flush_rx_obj)    },
};
static MP_DEFINE_CONST_DICT(mod_uartcdc_globals, mod_uartcdc_globals_table);

const mp_obj_module_t mod_uartcdc_module = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_uartcdc_globals,
};

MP_REGISTER_MODULE(MP_QSTR_uartcdc, mod_uartcdc_module);
