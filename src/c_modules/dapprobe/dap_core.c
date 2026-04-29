// Annealage Pod: CMSIS-DAP core skeleton implementation.
//
// Phase 1: log only.

#include "dap_core.h"
#include "esp_log.h"

static const char *TAG = "dapprobe";

void dap_core_start(void) {
    ESP_LOGI(TAG, "skeleton start (Phase 1, no DAP/SWO backend yet)");
}
