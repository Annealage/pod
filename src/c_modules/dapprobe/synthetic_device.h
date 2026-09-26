/* Annealage Pod: synthetic CMSIS-DAP-v2 USB device.
 *
 * Implements the virtual_device_t expected by the usbip multiplexer
 * (WS-A, src/c_modules/usbip/virtual_device.h). The descriptor stack
 * follows research/usbip-multiplexing-design.md §2 verbatim:
 *  - bDeviceClass = 0xEF / 0x02 / 0x01 (IAD-friendly composite triple).
 *  - bcdUSB = 0x0210 (BOS-capable; required for MS-OS-2.0 on Windows).
 *  - One configuration, one interface (bInterfaceClass=0xFF, sub=0,
 *    proto=0, iInterface = "CMSIS-DAP" exactly because pyOCD and
 *    probe-rs match on that string).
 *  - Three Bulk endpoints in the canonical order:
 *      EP1 OUT (DAP cmds)
 *      EP2 IN  (DAP responses)
 *      EP3 IN  (SWO trace)
 *
 * Public surface (called from dap_core.c only):
 *   synthetic_device_register() - hand the virtual_device_t to the
 *                                 usbip server's registry
 *   synthetic_device_get()      - test hook; returns a pointer to the
 *                                 single static instance for unit-test
 *                                 introspection
 *
 * SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Annealage-firmware-exception
 */

#ifndef MPY_POD_DAPPROBE_SYNTHETIC_DEVICE_H
#define MPY_POD_DAPPROBE_SYNTHETIC_DEVICE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "../usbip/virtual_device.h"

#ifdef __cplusplus
extern "C" {
#endif

/* USB descriptor blob constants exposed for unit tests / inspection.
 *
 * The blobs themselves live in synthetic_device.c. Sizes match the
 * design doc:
 *   device descriptor       : 18 bytes
 *   configuration descriptor: 32 bytes (9 + 9 + 7 + 7 + 7 - wait, this
 *                                       is 39; recheck)
 *
 * Actual sizes (validated by static asserts in the .c file):
 *   device:        18 bytes (0x12)
 *   config total:  32 bytes (9 config + 9 interface + 3*7 endpoints)
 *
 * The design doc uses 39 = 9+9+7+7+7, which matches; 9+9+(3*7) = 39.
 * 32 is the EP2-only (no SWO) variant. We always emit 3 EPs.
 */

#define SYNTHETIC_DAP_DEVICE_DESC_LEN   18u
#define SYNTHETIC_DAP_CONFIG_DESC_LEN   39u  /* 9 + 9 + 3*7 */

#define SYNTHETIC_DAP_VID               0xC251u
#define SYNTHETIC_DAP_PID               0xF00Au

/* USB endpoint addresses (with direction bit). */
#define SYNTHETIC_DAP_EP_OUT_CMD        0x01u  /* EP1 Bulk-OUT */
#define SYNTHETIC_DAP_EP_IN_RESP        0x82u  /* EP2 Bulk-IN  */
#define SYNTHETIC_DAP_EP_IN_SWO         0x83u  /* EP3 Bulk-IN  */

#define SYNTHETIC_DAP_BULK_MAX_PACKET   64u

/* Register the synthetic device with the usbip server's virtual-device
 * registry. Must be called after dap_core_init(). Returns 0 on success
 * or the registry's negative errno (-EEXIST if already registered,
 * -ENOMEM if the registry is full). */
int synthetic_device_register(void);

/* Test hook: returns the static virtual_device_t. The descriptor and
 * ops are populated at first call to synthetic_device_register(); the
 * blobs themselves are populated at module init time and are stable
 * thereafter. */
virtual_device_t *synthetic_device_get(void);

/* Test hook: returns the device descriptor blob. */
const uint8_t *synthetic_device_get_device_desc(size_t *out_len);

/* Test hook: returns the configuration-descriptor blob (config + iface +
 * EP1 + EP2 + EP3). */
const uint8_t *synthetic_device_get_config_desc(size_t *out_len);

/* Test hook: returns a UTF-16LE string descriptor at the given index.
 * Index 0 returns the LANGID list. Index 4 returns "CMSIS-DAP". The
 * other indices are populated at init from the build-time identity
 * strings. Returns NULL if the index is out of range. */
const uint8_t *synthetic_device_get_string_desc(uint8_t index, size_t *out_len);

/* Toggle per-URB observability logging on the synthetic CMSIS-DAP-v2
 * responder. When enabled every Bulk-OUT command, every queued
 * Bulk-IN response, and every non-empty SWO read emits one ESP_LOGI
 * line under the "dapprobe" tag. Default off. */
void synthetic_device_set_verbose(bool enable);

/* True if per-URB verbose logging is currently enabled. */
bool synthetic_device_is_verbose(void);

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_DAPPROBE_SYNTHETIC_DEVICE_H */
