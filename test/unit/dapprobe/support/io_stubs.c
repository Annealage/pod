/* mpy-pod WS-C unit-test host stubs for the WS-D engine.
 *
 * The unit tests exercise the dapprobe protocol layer without hardware,
 * so the SWD/SWO API surface is stubbed here. swd_transfer always
 * returns SWD_STATUS_OK; swo_read returns 0 bytes; counters stay at
 * zero. This is enough to validate the descriptor blobs and the
 * URB-routing logic; SWD/SWO behaviour is covered by the on-target
 * tests under test/unit/dapprobe-io/ (WS-D scope).
 */

#include "swd.h"
#include "swo.h"

#include <string.h>
#include <sys/types.h>

/* esp_err_t / ESP_OK are not available off-target; the WS-D headers
 * declare functions returning esp_err_t but treating it as int is
 * fine because the host build never reads the value through IDF
 * macros. We provide a fallback typedef in the IDF-shim header. */

/* ---- swd ----------------------------------------------------------- */

static bool s_swd_initialised;

swd_config_t swd_default_config(void) {
    swd_config_t cfg = {0};
    cfg.pin_swclk = 10;
    cfg.pin_swdio = 11;
    cfg.pin_dir   = 12;
    cfg.pin_nrst  = 14;
    cfg.default_clock_hz = 1000000;
    cfg.dir_active_high  = true;
    return cfg;
}

esp_err_t swd_init(const swd_config_t *config) {
    (void)config;
    s_swd_initialised = true;
    return 0;
}

esp_err_t swd_deinit(void)            { s_swd_initialised = false; return 0; }
esp_err_t swd_set_clock_hz(uint32_t h){ (void)h; return 0; }
uint32_t  swd_get_clock_hz(void)      { return 1000000U; }
esp_err_t swd_set_nrst(bool released) { (void)released; return 0; }
esp_err_t swd_line_reset(void)        { return 0; }

swd_status_t swd_transfer(uint8_t header, const uint32_t *data_in, uint32_t *data_out) {
    (void)header;
    (void)data_in;
    if (data_out) { *data_out = 0; }
    return SWD_STATUS_OK;
}

bool     swd_is_initialised(void)   { return s_swd_initialised; }
uint32_t swd_transfers_total(void)  { return 0U; }

/* ---- swo ----------------------------------------------------------- */

static bool s_swo_initialised;

swo_config_t swo_default_config(void) {
    swo_config_t cfg = {0};
    cfg.pin_swo_rx        = 13;
    cfg.uart_port         = 1;
    cfg.tier1_dram_bytes  = 64 * 1024;
    cfg.tier2_psram_bytes = 8 * 1024 * 1024;
    cfg.drain_task_core   = 1;
    cfg.drain_task_priority = 7;
    return cfg;
}

esp_err_t swo_init(uint32_t baud, swo_mode_t mode) {
    (void)baud; (void)mode;
    s_swo_initialised = true;
    return 0;
}
esp_err_t swo_start(uint32_t baud, swo_mode_t mode) { (void)baud; (void)mode; return 0; }
esp_err_t swo_stop(void)   { return 0; }
esp_err_t swo_deinit(void) { s_swo_initialised = false; return 0; }

ssize_t swo_read(uint8_t *buf, size_t max_len) {
    (void)buf; (void)max_len;
    return 0;
}

uint32_t swo_overruns_total(void)  { return 0U; }
size_t   swo_bytes_buffered(void)  { return 0U; }
size_t   swo_bytes_received(void)  { return 0U; }

void swo_overrun_clear_latched(void) {}
bool swo_overrun_latched(void)       { return false; }
