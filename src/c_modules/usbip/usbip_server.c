// Annealage Pod: USB/IP server skeleton implementation.
//
// Phase 1: log only. Phase 2 will replace this with the real TCP/3240
// listener, USB/IP protocol parser, and synthetic-device multiplexer
// that fronts both TinyUSB host (busid 1) and dapprobe (busid 2).

#include "usbip_server.h"
#include "esp_log.h"

static const char *TAG = "usbip";

void usbip_server_start(void) {
    ESP_LOGI(TAG, "skeleton start (Phase 1, no protocol yet)");
}
