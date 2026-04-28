// Annealage Pod: OTA C shim implementation.
//
// Wraps esp_https_ota for synchronous use from the MP main task.
// See ops_ota.h for the public surface and docs/design/ops.md for
// the partition-handoff sequence (esp_https_ota does the
// inactive-slot write + boot-partition switch internally; we only
// supply the URL, optional cert_pem, and trigger esp_restart() from
// the MP side after success).

#include "ops_ota.h"

#include <string.h>

#include "esp_err.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"

static const char *TAG = "ops_ota";

esp_err_t ops_ota_update(const char *url, const char *cert_pem)
{
    if (url == NULL || url[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }

    esp_http_client_config_t http_cfg = {
        .url = url,
        .cert_pem = cert_pem,
        .keep_alive_enable = true,
        .timeout_ms = 30000,
    };

    esp_https_ota_config_t ota_cfg = {
        .http_config = &http_cfg,
    };

    ESP_LOGI(TAG, "starting OTA from %s", url);
    esp_err_t err = esp_https_ota(&ota_cfg);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "OTA finished, next boot will use the new slot");
    } else {
        ESP_LOGE(TAG, "OTA failed: %s (0x%x)", esp_err_to_name(err), err);
    }
    return err;
}

esp_err_t ops_ota_mark_app_valid(void)
{
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "running image marked valid");
    } else if (err == ESP_ERR_OTA_ROLLBACK_INVALID_STATE) {
        // Not pending verify: typical on factory boots and on
        // already-validated images. Surface to the caller, do not
        // log noisy on this expected branch.
        ESP_LOGD(TAG, "mark_app_valid: not pending verify");
    } else {
        ESP_LOGW(TAG, "mark_app_valid failed: %s (0x%x)",
                 esp_err_to_name(err), err);
    }
    return err;
}
