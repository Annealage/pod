/* Annealage Pod: CMSIS-DAP protocol-layer entry points (WS-C).
 *
 * Bridges the vendored ARM-software CMSIS-DAP `DAP.c` (Apache-2.0) to:
 *  - WS-D's swd.h SWD I/O engine (SPI2 + GDMA + dedic_gpio DIR strobe)
 *  - WS-D's swo.h SWO trace pipeline (UART1 + UHCI two-tier ring)
 *  - WS-A's USB/IP virtual-device registry (synthetic_device.c shim)
 *  - identity strings consumed by DAP_Info via the DAP_Get*String
 *    inlines in port/DAP_config.h
 *
 * dap_core_init() runs once at module attach: brings up the engines at
 * spec defaults, calls DAP_Setup() to populate the vendored layer's
 * internal state, and computes the iSerial string from the ESP32-S3
 * efuse MAC. dap_core_attach() registers the synthetic device with the
 * usbip server.
 *
 * dap_core_process() is the EP1 OUT entry point: forward to
 * DAP_ProcessCommand, return the response length the host should drain
 * on the matching EP2 IN URB.
 *
 * Host-build behaviour (test/unit/dapprobe/): the WS-D engine is
 * stubbed via support/io_stubs.c. ESP-IDF symbols (esp_log,
 * esp_efuse_mac_get_default) are guarded by ESP_PLATFORM. The host
 * serial string is a fixed "0123456789AB" so the DAP_Info(SER_NUM)
 * tests are deterministic.
 *
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#include "dap_core.h"

#include "DAP_config.h"
#include "DAP.h"

#include "swd.h"
#include "swo.h"

#include "synthetic_device.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>

#ifdef ESP_PLATFORM
#include "esp_log.h"
#include "esp_mac.h"
#endif

#ifdef ESP_PLATFORM
static const char *TAG = "dapprobe";
#endif

/* ---------------------------------------------------------------------
 * Identity strings (consumed by port/DAP_config.h's DAP_Get*String
 * inlines via these extern pointers).
 * ------------------------------------------------------------------ */

static char s_serial_string[16];

const char *dap_port_vendor_string     = "mpy-pod";
const char *dap_port_product_string    = "mpy-pod synthetic CMSIS-DAP";
const char *dap_port_serial_string     = s_serial_string;
const char *dap_port_fw_version_string = DAP_FW_VER;

/* ---------------------------------------------------------------------
 * Module state
 * ------------------------------------------------------------------ */

static bool s_initialised;
static bool s_attached;

/* ---------------------------------------------------------------------
 * Serial string population
 *
 * On real hardware: lower 6 bytes of the efuse MAC, encoded as 12 hex
 * digits + NUL. On host: fixed "0123456789ab" so unit tests are
 * deterministic.
 * ------------------------------------------------------------------ */

static void populate_serial_string(void) {
#ifdef ESP_PLATFORM
    uint8_t mac[8] = {0};
    if (esp_efuse_mac_get_default(mac) == ESP_OK) {
        snprintf(s_serial_string, sizeof(s_serial_string),
                 "%02x%02x%02x%02x%02x%02x",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    } else {
        strncpy(s_serial_string, "000000000000", sizeof(s_serial_string) - 1U);
    }
#else
    strncpy(s_serial_string, "0123456789ab", sizeof(s_serial_string) - 1U);
#endif
    s_serial_string[sizeof(s_serial_string) - 1U] = '\0';
}

/* ---------------------------------------------------------------------
 * dap_core_init / _deinit / _attach
 * ------------------------------------------------------------------ */

int dap_core_init(void) {
    if (s_initialised) {
        return 0;
    }

    populate_serial_string();

    /* Engine bring-up. On host the support stubs always return 0. */
    swd_config_t scfg = swd_default_config();
    esp_err_t serr = swd_init(&scfg);
    if (serr != 0) {
#ifdef ESP_PLATFORM
        ESP_LOGW(TAG, "swd_init failed: %d", (int)serr);
#endif
        return -EIO;
    }

    /* SWO is started lazily on the first SWO_Mode/SWO_Control command;
     * swo_init brings the pipeline up but leaves capture idle. The
     * host requests a baud later via DAP_SWO_Baudrate / DAP_SWO_Control. */
    esp_err_t oerr = swo_init(0U, SWO_MODE_OFF);
    if (oerr != 0) {
#ifdef ESP_PLATFORM
        ESP_LOGW(TAG, "swo_init failed: %d", (int)oerr);
#endif
        /* Non-fatal: SWD-only debug works without SWO. */
    }

    /* Initialise the vendored CMSIS-DAP protocol-layer state. */
    DAP_Setup();

    s_initialised = true;
#ifdef ESP_PLATFORM
    ESP_LOGI(TAG, "initialised: vendor='%s' product='%s' serial='%s' fw=%s",
             dap_port_vendor_string, dap_port_product_string,
             dap_port_serial_string, dap_port_fw_version_string);
#endif
    return 0;
}

int dap_core_deinit(void) {
    if (!s_initialised) {
        return 0;
    }
    (void)swo_stop();
    (void)swo_deinit();
    (void)swd_deinit();
    s_initialised = false;
    s_attached = false;
    return 0;
}

int dap_core_attach(void) {
    if (s_attached) {
        return 0;
    }
    int rc = dap_core_init();
    if (rc != 0) {
        return rc;
    }
    rc = synthetic_device_register();
    if (rc != 0) {
#ifdef ESP_PLATFORM
        ESP_LOGW(TAG, "synthetic_device_register: rc=%d", rc);
#endif
        return rc;
    }
    s_attached = true;
    return 0;
}

/* ---------------------------------------------------------------------
 * Wire-level processing
 * ------------------------------------------------------------------ */

size_t dap_core_process(const uint8_t *request, size_t request_len,
                        uint8_t *response, size_t response_capacity) {
    (void)request_len;
    (void)response_capacity;

    /* Lazy-init: tests can call dap_core_process without an explicit
     * dap_core_init, so we self-initialise. The check is cheap. */
    if (!s_initialised) {
        (void)dap_core_init();
    }

    /* DAP_ProcessCommand returns:
     *   high 16 bits: number of request bytes consumed
     *   low  16 bits: number of response bytes produced
     * The Bulk-OUT URB is a single command per packet, so we trust the
     * low half as the response length. */
    uint32_t r = DAP_ProcessCommand(request, response);
    return (size_t)(r & 0xFFFFU);
}

ssize_t dap_core_swo_read(uint8_t *buf, size_t max_len) {
    ssize_t n = swo_read(buf, max_len);
    return (n < 0) ? 0 : n;
}

/* ---------------------------------------------------------------------
 * Telemetry / introspection
 * ------------------------------------------------------------------ */

void dap_core_telemetry(dap_core_telemetry_t *out) {
    if (out == NULL) {
        return;
    }
    out->initialised         = s_initialised;
    out->attached            = s_attached;
    out->swd_clock_hz        = swd_get_clock_hz();
    out->swd_transfers_total = swd_transfers_total();
    out->swo_overruns_total  = swo_overruns_total();
    out->swo_bytes_buffered  = swo_bytes_buffered();
}

const char *dap_core_serial_string(void)     { return s_serial_string; }
const char *dap_core_vendor_string(void)     { return dap_port_vendor_string; }
const char *dap_core_product_string(void)    { return dap_port_product_string; }
const char *dap_core_fw_version_string(void) { return dap_port_fw_version_string; }
bool dap_core_is_initialised(void)           { return s_initialised; }
bool dap_core_is_attached(void)              { return s_attached; }
