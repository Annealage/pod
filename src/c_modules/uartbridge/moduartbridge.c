// Annealage Pod: MicroPython binding for the uartbridge C module.
//
// MP-side surface:
//
//   uartbridge.start(uart=2, port=2000, baud=115200, bits=8,
//                    parity=None, stop=1, flow=False, telnet=False,
//                    tx=-1, rx=-1, rts=-1, cts=-1,
//                    replace_client=True, core=0,
//                    rx_buf=0, tx_buf=0)
//       -> None
//   uartbridge.stop()             -> None
//   uartbridge.config_get()       -> dict
//   uartbridge.client_count()     -> int (0 or 1)
//
// On any C-side failure the call raises OSError with the ESP error
// code as its argument (matching the MP esp32 port idiom).

#include <string.h>

#include "py/obj.h"
#include "py/runtime.h"

#include "uart_bridge.h"

static void raise_on_err(esp_err_t err) {
    if (err != ESP_OK) {
        mp_raise_OSError((int)err);
    }
}

static mp_obj_t mod_uartbridge_start(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args) {
    enum {
        ARG_uart, ARG_port, ARG_baud, ARG_bits, ARG_parity, ARG_stop,
        ARG_flow, ARG_telnet,
        ARG_tx, ARG_rx, ARG_rts, ARG_cts,
        ARG_replace_client, ARG_core,
        ARG_rx_buf, ARG_tx_buf,
    };
    static const mp_arg_t allowed_args[] = {
        { MP_QSTR_uart,           MP_ARG_INT,  {.u_int = 2} },
        { MP_QSTR_port,           MP_ARG_INT,  {.u_int = 2000} },
        { MP_QSTR_baud,           MP_ARG_INT,  {.u_int = 115200} },
        { MP_QSTR_bits,           MP_ARG_INT,  {.u_int = 8} },
        { MP_QSTR_parity,         MP_ARG_OBJ,  {.u_rom_obj = MP_ROM_NONE} },
        { MP_QSTR_stop,           MP_ARG_INT,  {.u_int = 1} },
        { MP_QSTR_flow,           MP_ARG_BOOL, {.u_bool = false} },
        { MP_QSTR_telnet,         MP_ARG_BOOL, {.u_bool = false} },
        { MP_QSTR_tx,             MP_ARG_INT,  {.u_int = -1} },
        { MP_QSTR_rx,             MP_ARG_INT,  {.u_int = -1} },
        { MP_QSTR_rts,            MP_ARG_INT,  {.u_int = -1} },
        { MP_QSTR_cts,            MP_ARG_INT,  {.u_int = -1} },
        { MP_QSTR_replace_client, MP_ARG_BOOL, {.u_bool = true} },
        { MP_QSTR_core,           MP_ARG_INT,  {.u_int = 0} },
        { MP_QSTR_rx_buf,         MP_ARG_INT,  {.u_int = 0} },
        { MP_QSTR_tx_buf,         MP_ARG_INT,  {.u_int = 0} },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];
    mp_arg_parse_all(n_args, pos_args, kw_args,
                     MP_ARRAY_SIZE(allowed_args), allowed_args, args);

    uart_bridge_config_t cfg;
    uart_bridge_config_default(&cfg);
    cfg.uart_num = args[ARG_uart].u_int;
    cfg.tcp_port = args[ARG_port].u_int;
    cfg.baud = args[ARG_baud].u_int;
    cfg.data_bits = args[ARG_bits].u_int;
    cfg.stop_bits = args[ARG_stop].u_int;
    cfg.flow_control = args[ARG_flow].u_bool;
    cfg.telnet = args[ARG_telnet].u_bool;
    cfg.tx_pin = args[ARG_tx].u_int;
    cfg.rx_pin = args[ARG_rx].u_int;
    cfg.rts_pin = args[ARG_rts].u_int;
    cfg.cts_pin = args[ARG_cts].u_int;
    cfg.replace_client = args[ARG_replace_client].u_bool;
    cfg.task_core = args[ARG_core].u_int;
    cfg.rx_buf_size = args[ARG_rx_buf].u_int;
    cfg.tx_buf_size = args[ARG_tx_buf].u_int;

    mp_obj_t parity_obj = args[ARG_parity].u_obj;
    if (parity_obj == mp_const_none) {
        cfg.parity = UART_BRIDGE_PARITY_NONE;
    } else {
        mp_int_t p = mp_obj_get_int(parity_obj);
        if (p == 0) { cfg.parity = UART_BRIDGE_PARITY_EVEN; }
        else if (p == 1) { cfg.parity = UART_BRIDGE_PARITY_ODD; }
        else { mp_raise_ValueError(MP_ERROR_TEXT("parity must be None, 0 (even) or 1 (odd)")); }
    }

    raise_on_err(uart_bridge_start(&cfg));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(mod_uartbridge_start_obj, 0, mod_uartbridge_start);

static mp_obj_t mod_uartbridge_stop(void) {
    raise_on_err(uart_bridge_stop());
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_uartbridge_stop_obj, mod_uartbridge_stop);

static mp_obj_t mod_uartbridge_config_get(void) {
    uart_bridge_config_t cfg;
    raise_on_err(uart_bridge_get_config(&cfg));

    mp_obj_t parity_obj;
    switch (cfg.parity) {
        case UART_BRIDGE_PARITY_EVEN: parity_obj = MP_OBJ_NEW_SMALL_INT(0); break;
        case UART_BRIDGE_PARITY_ODD:  parity_obj = MP_OBJ_NEW_SMALL_INT(1); break;
        default: parity_obj = mp_const_none; break;
    }

    mp_obj_t d = mp_obj_new_dict(0);
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_uart),   MP_OBJ_NEW_SMALL_INT(cfg.uart_num));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_port),   MP_OBJ_NEW_SMALL_INT(cfg.tcp_port));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_baud),   MP_OBJ_NEW_SMALL_INT(cfg.baud));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_bits),   MP_OBJ_NEW_SMALL_INT(cfg.data_bits));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_parity), parity_obj);
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_stop),   MP_OBJ_NEW_SMALL_INT(cfg.stop_bits));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_flow),   mp_obj_new_bool(cfg.flow_control));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_telnet), mp_obj_new_bool(cfg.telnet));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_tx),     MP_OBJ_NEW_SMALL_INT(cfg.tx_pin));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_rx),     MP_OBJ_NEW_SMALL_INT(cfg.rx_pin));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_rts),    MP_OBJ_NEW_SMALL_INT(cfg.rts_pin));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_cts),    MP_OBJ_NEW_SMALL_INT(cfg.cts_pin));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_replace_client),
                     mp_obj_new_bool(cfg.replace_client));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_core),   MP_OBJ_NEW_SMALL_INT(cfg.task_core));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_rx_buf), MP_OBJ_NEW_SMALL_INT(cfg.rx_buf_size));
    mp_obj_dict_store(d, MP_OBJ_NEW_QSTR(MP_QSTR_tx_buf), MP_OBJ_NEW_SMALL_INT(cfg.tx_buf_size));
    return d;
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_uartbridge_config_get_obj, mod_uartbridge_config_get);

static mp_obj_t mod_uartbridge_client_count(void) {
    return MP_OBJ_NEW_SMALL_INT(uart_bridge_client_count());
}
static MP_DEFINE_CONST_FUN_OBJ_0(mod_uartbridge_client_count_obj, mod_uartbridge_client_count);

static const mp_rom_map_elem_t mod_uartbridge_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),     MP_ROM_QSTR(MP_QSTR_uartbridge) },
    { MP_ROM_QSTR(MP_QSTR_start),        MP_ROM_PTR(&mod_uartbridge_start_obj) },
    { MP_ROM_QSTR(MP_QSTR_stop),         MP_ROM_PTR(&mod_uartbridge_stop_obj) },
    { MP_ROM_QSTR(MP_QSTR_config_get),   MP_ROM_PTR(&mod_uartbridge_config_get_obj) },
    { MP_ROM_QSTR(MP_QSTR_client_count), MP_ROM_PTR(&mod_uartbridge_client_count_obj) },
};
static MP_DEFINE_CONST_DICT(mod_uartbridge_globals, mod_uartbridge_globals_table);

const mp_obj_module_t mod_uartbridge_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_uartbridge_globals,
};

MP_REGISTER_MODULE(MP_QSTR_uartbridge, mod_uartbridge_module);
