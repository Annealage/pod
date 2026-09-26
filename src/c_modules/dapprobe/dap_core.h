/* Annealage Pod: CMSIS-DAP protocol-layer entry points (WS-C).
 *
 * dap_core wraps the vendored ARM-software CMSIS-DAP `DAP.c` (Apache-2.0)
 * with the engine bring-up, identity strings, and SWO drain hook that
 * the synthetic-device URB handlers call into. The CMSIS-DAP wire-level
 * functions (DAP_ProcessCommand, SWO_Status, etc.) live in vendor/ and
 * port/ glue; this header is the small surface MicroPython, the
 * synthetic_device, and the unit tests touch.
 *
 * Phase 2 scope: synchronous CMSIS-DAP request -> response. The host
 * issues one Bulk-OUT per command, our EP1 handler calls
 * dap_core_process which runs DAP_ProcessCommand to completion and
 * returns the response length. The host then issues a matching Bulk-IN
 * to drain the response.
 *
 * SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Annealage-firmware-exception
 */

#ifndef MPY_POD_DAPPROBE_DAP_CORE_H
#define MPY_POD_DAPPROBE_DAP_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Telemetry snapshot returned to MP via dapprobe.telemetry(). */
typedef struct {
    bool     initialised;
    bool     attached;
    uint32_t swd_clock_hz;
    uint32_t swd_transfers_total;
    uint32_t swo_overruns_total;
    size_t   swo_bytes_buffered;
} dap_core_telemetry_t;

/* Bring up the WS-D SWD engine, the WS-D SWO pipeline at the rev1
 * defaults, run DAP_Setup() to initialise the vendored protocol layer's
 * internal state, and populate the DAP_Info identity strings.
 *
 * Idempotent: re-invoking returns 0 with no further side effects.
 *
 * Returns 0 on success, negative errno on failure. */
int dap_core_init(void);

/* Tear down: stops SWO capture, deinitialises the engines, marks the
 * module not-initialised. The synthetic-device registration is not
 * affected (the WS-A registry is append-only at present). */
int dap_core_deinit(void);

/* Hook into WS-A's USB/IP virtual-device registry: ensures dap_core_init
 * has run, then registers the synthetic CMSIS-DAP-v2 device on busid 2.
 * Idempotent. */
int dap_core_attach(void);

/* Process a single CMSIS-DAP wire-level request. Forwards to
 * DAP_ProcessCommand from the vendored DAP.c. Returns the number of
 * bytes written to `response`. */
size_t dap_core_process(const uint8_t *request, size_t request_len,
                        uint8_t *response, size_t response_capacity);

/* Drain bytes from the SWO tier-2 PSRAM ring. Wraps WS-D's swo_read.
 * Returns the number of bytes copied (>= 0); never -1 here, the
 * synthetic-device EP3 handler relies on this. */
ssize_t dap_core_swo_read(uint8_t *buf, size_t max_len);

/* Telemetry / introspection. */
void dap_core_telemetry(dap_core_telemetry_t *out);
const char *dap_core_serial_string(void);
const char *dap_core_vendor_string(void);
const char *dap_core_product_string(void);
const char *dap_core_fw_version_string(void);
bool dap_core_is_initialised(void);
bool dap_core_is_attached(void);

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_DAPPROBE_DAP_CORE_H */
