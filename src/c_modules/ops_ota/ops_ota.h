// Annealage Pod: OTA C shim public API.
//
// Wraps esp_https_ota for use from MicroPython. Synchronous: the
// caller's task blocks for the duration of the OTA download. Caller
// is the MP main task; long-running C tasks must not call this.
//
// All entry points return ESP_OK on success and an esp_err_t error
// code otherwise (negative integers are passed through unchanged).
//
// See docs/design/ops.md for the spec mapping (§4.3 in spec.md).

#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Run a single esp_https_ota cycle. `url` must be a NUL-terminated
// HTTPS or HTTP URL (HTTPS is preferred per spec; HTTP is allowed for
// closed-network deployments). `cert_pem` is the optional PEM-encoded
// CA bundle pointer; pass NULL to use the IDF "skip server cert" path
// (unsafe, allowed only when CONFIG_ESP_HTTPS_OTA_ALLOW_HTTP is on).
//
// Returns ESP_OK if the new image was successfully written and the
// boot partition switched. Caller is responsible for calling
// esp_restart() afterwards (the MP wrapper does this from boot).
esp_err_t ops_ota_update(const char *url, const char *cert_pem);

// Mark the running image as valid, cancelling any pending rollback.
// No-op if the running partition is `factory` (rollback only applies
// to ota_0/ota_1). Returns ESP_OK on success or
// ESP_ERR_OTA_ROLLBACK_INVALID_STATE if the running app is not in the
// pending-verify state.
esp_err_t ops_ota_mark_app_valid(void);

#ifdef __cplusplus
}
#endif
