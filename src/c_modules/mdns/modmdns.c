/* Annealage Pod: MicroPython binding for IDF mDNS.
 *
 * Surface:
 *   mdns.hostname(name: str) -> None
 *       Initialise the mDNS service and set the hostname.
 *       Idempotent: safe to call more than once (mdns_init is idempotent).
 *
 *   mdns.add_service(service_type: str, proto: str, port: int,
 *                    txt: dict) -> None
 *       Register a service under the current hostname.
 *       txt values must be str.  Max 16 TXT key/value pairs.
 *
 * SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Annealage-firmware-exception
 */

#include "py/obj.h"
#include "py/runtime.h"

#ifndef MPY_POD_HOST_TEST_BUILD
#include "mdns.h"

static void raise_on_err(esp_err_t err) {
    if (err != ESP_OK) {
        mp_raise_OSError((int)err);
    }
}

static mp_obj_t mod_mdns_hostname(mp_obj_t name_obj) {
    const char *name = mp_obj_str_get_str(name_obj);
    raise_on_err(mdns_init());
    raise_on_err(mdns_hostname_set(name));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_mdns_hostname_obj, mod_mdns_hostname);

#define MAX_TXT_ITEMS 16

static mp_obj_t mod_mdns_add_service(size_t n_args, const mp_obj_t *args) {
    const char *service_type = mp_obj_str_get_str(args[0]);
    const char *proto        = mp_obj_str_get_str(args[1]);
    uint16_t    port         = (uint16_t)mp_obj_get_int(args[2]);

    mp_map_t *map = mp_obj_dict_get_map(args[3]);
    size_t num_items = map->used;
    if (num_items > MAX_TXT_ITEMS) {
        mp_raise_ValueError(MP_ERROR_TEXT("too many TXT items (max 16)"));
    }

    mdns_txt_item_t txt[MAX_TXT_ITEMS];
    size_t idx = 0;
    for (size_t i = 0; i < map->alloc && idx < num_items; i++) {
        if (mp_map_slot_is_filled(map, i)) {
            txt[idx].key   = mp_obj_str_get_str(map->table[i].key);
            txt[idx].value = mp_obj_str_get_str(map->table[i].value);
            idx++;
        }
    }

    raise_on_err(mdns_service_add(NULL, service_type, proto, port, txt, num_items));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_mdns_add_service_obj, 4, 4, mod_mdns_add_service);

static const mp_rom_map_elem_t mod_mdns_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),    MP_ROM_QSTR(MP_QSTR_mdns) },
    { MP_ROM_QSTR(MP_QSTR_hostname),    MP_ROM_PTR(&mod_mdns_hostname_obj) },
    { MP_ROM_QSTR(MP_QSTR_add_service), MP_ROM_PTR(&mod_mdns_add_service_obj) },
};
static MP_DEFINE_CONST_DICT(mod_mdns_globals, mod_mdns_globals_table);

const mp_obj_module_t mod_mdns_module = {
    .base    = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&mod_mdns_globals,
};

MP_REGISTER_MODULE(MP_QSTR_mdns, mod_mdns_module);

#endif /* MPY_POD_HOST_TEST_BUILD */
