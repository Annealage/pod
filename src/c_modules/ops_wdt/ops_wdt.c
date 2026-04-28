// Annealage Pod: Task watchdog C shim implementation.
//
// The TWDT itself is initialised by IDF at boot when
// CONFIG_ESP_TASK_WDT_INIT=y (set in the board's sdkconfig.board).
// This shim only owns subscribe/kick/unsubscribe for the MP main
// task; long-running C tasks use esp_task_wdt_add() directly.

#include "ops_wdt.h"

#include "esp_err.h"
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ops_wdt";

// Track the currently-applied timeout so we only call
// esp_task_wdt_reconfigure() when the caller actually requests a
// change. A zero value means "use the IDF default from sdkconfig".
static uint32_t s_current_timeout_ms = 0;

esp_err_t ops_wdt_subscribe(uint32_t timeout_ms)
{
    // Reconfigure if the caller asked for a non-default timeout that
    // differs from the currently-applied value. The IDF will reject
    // reconfigure() before init(); since CONFIG_ESP_TASK_WDT_INIT=y
    // ensures init runs at boot, this is safe.
    if (timeout_ms != 0 && timeout_ms != s_current_timeout_ms) {
        esp_task_wdt_config_t cfg = {
            .timeout_ms = timeout_ms,
            .idle_core_mask = 0, // do not auto-subscribe idle tasks
            .trigger_panic = true,
        };
        esp_err_t err = esp_task_wdt_reconfigure(&cfg);
        if (err == ESP_ERR_INVALID_STATE) {
            // TWDT not yet initialised (sdkconfig override?). Try to
            // bring it up here; this is the same shape as the IDF
            // boot path.
            err = esp_task_wdt_init(&cfg);
        }
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "reconfigure failed: %s", esp_err_to_name(err));
            return err;
        }
        s_current_timeout_ms = timeout_ms;
    }

    esp_err_t err = esp_task_wdt_add(NULL);
    if (err == ESP_ERR_INVALID_ARG) {
        // Already subscribed: idempotent path.
        return ESP_OK;
    }
    return err;
}

esp_err_t ops_wdt_kick(void)
{
    return esp_task_wdt_reset();
}

esp_err_t ops_wdt_unsubscribe(void)
{
    return esp_task_wdt_delete(NULL);
}

bool ops_wdt_is_subscribed(void)
{
    return esp_task_wdt_status(NULL) == ESP_OK;
}
