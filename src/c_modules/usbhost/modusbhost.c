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

#include <errno.h>
#include <string.h>

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

#define HPRT_TRACE_BUF_LEN 1024

static mp_obj_t mod_usbhost_hprt_trace(size_t n_args, const mp_obj_t *pos_args,
                                       mp_map_t *kw_args)
{
    enum { ARG_duration_ms, ARG_period_us, ARG_force_every };
    static const mp_arg_t allowed_args[] = {
        { MP_QSTR_duration_ms, MP_ARG_INT, { .u_int = 3000 } },
        { MP_QSTR_period_us,   MP_ARG_INT, { .u_int = 200 } },
        { MP_QSTR_force_every, MP_ARG_INT, { .u_int = 0 } },
    };
    mp_arg_val_t args[MP_ARRAY_SIZE(allowed_args)];
    mp_arg_parse_all(n_args, pos_args, kw_args,
                     MP_ARRAY_SIZE(allowed_args), allowed_args, args);

    /* m_new uses MicroPython GC; 1024 samples * 8 bytes = 8 KB, fits
     * comfortably on the heap and avoids busting the C stack. */
    usbhost_hprt_sample_t *buf = m_new(usbhost_hprt_sample_t, HPRT_TRACE_BUF_LEN);
    size_t n = 0;
    int rc = usbhost_hprt_trace((uint32_t)args[ARG_duration_ms].u_int,
                                (uint32_t)args[ARG_period_us].u_int,
                                (uint32_t)args[ARG_force_every].u_int,
                                buf, HPRT_TRACE_BUF_LEN, &n);
    if (rc != 0) {
        m_del(usbhost_hprt_sample_t, buf, HPRT_TRACE_BUF_LEN);
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
    m_del(usbhost_hprt_sample_t, buf, HPRT_TRACE_BUF_LEN);
    return list;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(mod_usbhost_hprt_trace_obj, 0, mod_usbhost_hprt_trace);

static mp_obj_t mod_usbhost_ep_stats(mp_obj_t busid_obj)
{
    const char *busid_s = mp_obj_str_get_str(busid_obj);
    char busid[USBIP_BUSID_SIZE] = {0};
    size_t len = strlen(busid_s);
    if (len > USBIP_BUSID_SIZE) {
        len = USBIP_BUSID_SIZE;
    }
    memcpy(busid, busid_s, len);

    usbhost_ep_stats_t stats[32];
    if (!usbhost_get_ep_stats(busid, stats)) {
        mp_raise_OSError(ENODEV);
    }

    /* Return dict keyed by ep_addr (0x00..0x0F for OUT/CTRL, 0x80..0x8F
     * for IN); only include entries with non-zero activity. */
    mp_obj_t d = mp_obj_new_dict(0);
    for (uint8_t idx = 0; idx < 32; idx++) {
        const usbhost_ep_stats_t *e = &stats[idx];
        if (e->submitted == 0 && e->completed == 0 &&
            e->errored == 0 && e->cancelled == 0) {
            continue;
        }
        /* Reverse ep_mutex_index: low 4 bits = EP num, bit 4 = direction. */
        uint8_t ep_num = idx & 0x0F;
        uint8_t ep_dir = (idx & 0x10) ? 0x80 : 0x00;
        uint8_t ep_addr = ep_num | ep_dir;

        mp_obj_t tup[4] = {
            mp_obj_new_int_from_uint(e->submitted),
            mp_obj_new_int_from_uint(e->completed),
            mp_obj_new_int_from_uint(e->errored),
            mp_obj_new_int_from_uint(e->cancelled),
        };
        mp_obj_dict_store(d, mp_obj_new_int_from_uint(ep_addr),
                          mp_obj_new_tuple(4, tup));
    }
    return d;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_usbhost_ep_stats_obj, mod_usbhost_ep_stats);

static mp_obj_t mod_usbhost_ep0_errors(mp_obj_t busid_obj)
{
    const char *busid_s = mp_obj_str_get_str(busid_obj);
    char busid[USBIP_BUSID_SIZE] = {0};
    size_t len = strlen(busid_s);
    if (len > USBIP_BUSID_SIZE) {
        len = USBIP_BUSID_SIZE;
    }
    memcpy(busid, busid_s, len);

    usbhost_ep0_error_t errs[USBHOST_EP0_ERROR_LOG_SIZE];
    int rc = usbhost_get_ep0_errors(busid, errs);
    if (rc < 0) {
        mp_raise_OSError(-rc);
    }

    /* Return list of (t_us, setup_bytes, result_code, result_str) tuples,
     * oldest first. setup_bytes is a 8-byte bytes object; result_str is
     * one of "STALLED" / "TIMEOUT" / "FAILED" / "OTHER" for human read. */
    mp_obj_t list = mp_obj_new_list(0, NULL);
    for (int i = 0; i < rc; i++) {
        const usbhost_ep0_error_t *e = &errs[i];
        const char *rstr = "OTHER";
        if (e->result == 2) rstr = "STALLED";
        else if (e->result == 3) rstr = "TIMEOUT";
        else if (e->result == 4) rstr = "FAILED";
        mp_obj_t tup[4] = {
            mp_obj_new_int_from_uint(e->t_us),
            mp_obj_new_bytes(e->setup, 8),
            mp_obj_new_int(e->result),
            mp_obj_new_str(rstr, strlen(rstr)),
        };
        mp_obj_list_append(list, mp_obj_new_tuple(4, tup));
    }
    return list;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_usbhost_ep0_errors_obj, mod_usbhost_ep0_errors);

static const mp_rom_map_elem_t mod_usbhost_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),   MP_ROM_QSTR(MP_QSTR_usbhost) },
    { MP_ROM_QSTR(MP_QSTR_flush),      MP_ROM_PTR(&mod_usbhost_flush_obj) },
    { MP_ROM_QSTR(MP_QSTR_bus_reset),  MP_ROM_PTR(&mod_usbhost_bus_reset_obj) },
    { MP_ROM_QSTR(MP_QSTR_dwc2_hprt),  MP_ROM_PTR(&mod_usbhost_dwc2_hprt_obj) },
    { MP_ROM_QSTR(MP_QSTR_hprt_trace), MP_ROM_PTR(&mod_usbhost_hprt_trace_obj) },
    { MP_ROM_QSTR(MP_QSTR_ep_stats),   MP_ROM_PTR(&mod_usbhost_ep_stats_obj) },
    { MP_ROM_QSTR(MP_QSTR_ep0_errors), MP_ROM_PTR(&mod_usbhost_ep0_errors_obj) },
};
static MP_DEFINE_CONST_DICT(mod_usbhost_globals, mod_usbhost_globals_table);

const mp_obj_module_t mod_usbhost_module = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_usbhost_globals,
};

MP_REGISTER_MODULE(MP_QSTR_usbhost, mod_usbhost_module);
