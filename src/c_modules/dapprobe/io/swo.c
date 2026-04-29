// Annealage Pod: SWO trace pipeline.
//
// Architecture (spec.md §4.7):
//
//   UART1 RX (NRZ) -> UHCI DMA -> tier-1 DRAM ring (64 KB)
//                                       |
//                                       v   drain task on APP_CPU
//                              tier-2 PSRAM ring (8 MB)
//                                       |
//                                       v
//                                 swo_read() (consumer)
//
// The IDF UHCI driver (driver/uhci.h, IDF v5.5+) exposes a non-blocking
// uhci_receive() that mounts a user buffer to DMA. We use a ping-pong of
// two equally-sized DRAM chunks under the controller's max_receive_internal_mem
// to keep the DMA continuously fed: as one chunk completes (rx event "totally
// received"), we re-mount it; the other chunk continues capturing in the
// background.
//
// On every RX completion the ISR callback also wakes the drain task, which
// memcpy's the just-completed chunk into the tier-2 PSRAM ring. If tier-2
// is full, the ISR sets s_state.overrun_latched and increments overruns_total
// per CMSIS-DAP DAPLink semantics: keep streaming, set a flag, never inject
// asterisks.

#include "swo.h"

#include <string.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "freertos/idf_additions.h"

#include "driver/gpio.h"
#include "driver/uart.h"
#include "driver/uhci.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "soc/soc_caps.h"

static const char *TAG = "swo";

// ----------------------------------------------------------------------------
// Sizing / layout constants.
// ----------------------------------------------------------------------------

#define SWO_DEFAULT_PIN_RX            13
#define SWO_DEFAULT_UART_PORT         UART_NUM_1
#define SWO_DEFAULT_TIER1_BYTES       (64 * 1024)
#define SWO_DEFAULT_TIER2_BYTES       (8 * 1024 * 1024)
#define SWO_DEFAULT_DRAIN_CORE        1     // APP_CPU
#define SWO_DEFAULT_DRAIN_PRIORITY    7

// UHCI mounts user buffers; we ping-pong between two halves of the tier-1
// DRAM ring. The driver's max_receive_internal_mem and max_packet_receive
// effectively decide how much DMA buffering it allocates internally; we
// double-buffer at our level on top.
#define SWO_TIER1_CHUNKS              2

// ----------------------------------------------------------------------------
// Module state.
// ----------------------------------------------------------------------------

typedef struct {
    bool initialised;
    bool capturing;

    swo_config_t cfg;

    // Tier-1 DRAM ping-pong DMA buffers; each chunk is cfg.tier1_dram_bytes / 2.
    uint8_t *tier1_chunk[SWO_TIER1_CHUNKS];
    size_t tier1_chunk_size;

    // Tier-2 PSRAM ring.
    uint8_t *tier2;
    size_t tier2_size;
    volatile size_t tier2_head;        // write pointer (drain task)
    volatile size_t tier2_tail;        // read pointer (consumer)
    volatile size_t tier2_count;       // bytes currently buffered

    // Synchronisation.
    SemaphoreHandle_t tier2_lock;       // protects head/tail/count from races between drain task and consumer
    QueueHandle_t completion_queue;     // ISR -> drain task: int chunk index ready to copy

    // Drain task.
    TaskHandle_t drain_task;

    // UHCI / UART.
    uhci_controller_handle_t uhci;
    uart_port_t uart_port;

    // Counters.
    volatile uint32_t overruns_total;
    volatile bool overrun_latched;
    volatile size_t bytes_received_total;
} swo_state_t;

static swo_state_t s_state;

// ----------------------------------------------------------------------------
// ISR and drain task.
// ----------------------------------------------------------------------------

typedef struct {
    int chunk_idx;
    size_t length;
} drain_event_t;

IRAM_ATTR static bool s_uhci_rx_cb(uhci_controller_handle_t uhci_ctrl, const uhci_rx_event_data_t *edata, void *user_ctx)
{
    (void)uhci_ctrl;
    (void)user_ctx;

    BaseType_t hp_woken = pdFALSE;

    // Identify which chunk completed by pointer comparison.
    int chunk_idx = -1;
    for (int i = 0; i < SWO_TIER1_CHUNKS; i++) {
        if (edata->data == s_state.tier1_chunk[i]) {
            chunk_idx = i;
            break;
        }
    }
    if (chunk_idx < 0) {
        // Unknown buffer (shouldn't happen). Drop.
        return false;
    }

    drain_event_t evt = {
        .chunk_idx = chunk_idx,
        .length = edata->recv_size,
    };
    xQueueSendFromISR(s_state.completion_queue, &evt, &hp_woken);

    // Re-arm only on totally_received events, since partial events still
    // continue using the same buffer until the EOF condition triggers.
    if (edata->flags.totally_received) {
        // Re-mount this chunk for further DMA. uhci_receive() is supposed to be
        // safe from ISR if the driver was built with the appropriate cache-safe
        // flag, but to be conservative we let the drain task re-arm it.
    }
    return hp_woken == pdTRUE;
}

static void s_drain_task(void *arg)
{
    (void)arg;
    drain_event_t evt;
    while (1) {
        if (xQueueReceive(s_state.completion_queue, &evt, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (evt.chunk_idx < 0 || evt.chunk_idx >= SWO_TIER1_CHUNKS) {
            continue;
        }
        if (evt.length == 0) {
            // Re-arm and loop.
            uhci_receive(s_state.uhci, s_state.tier1_chunk[evt.chunk_idx], s_state.tier1_chunk_size);
            continue;
        }

        const uint8_t *src = s_state.tier1_chunk[evt.chunk_idx];
        size_t to_copy = evt.length;

        // Copy into tier-2 PSRAM ring under the lock.
        if (xSemaphoreTake(s_state.tier2_lock, portMAX_DELAY) == pdTRUE) {
            size_t free_space = s_state.tier2_size - s_state.tier2_count;
            if (to_copy > free_space) {
                // Tier-2 overflow. DAPLink semantics: drop oldest, keep streaming, set flag.
                size_t drop = to_copy - free_space;
                s_state.tier2_tail = (s_state.tier2_tail + drop) % s_state.tier2_size;
                s_state.tier2_count -= drop;
                s_state.overruns_total++;
                s_state.overrun_latched = true;
            }
            // Now copy as a wrapped write.
            size_t first = s_state.tier2_size - s_state.tier2_head;
            if (first > to_copy) first = to_copy;
            memcpy(s_state.tier2 + s_state.tier2_head, src, first);
            if (to_copy > first) {
                memcpy(s_state.tier2, src + first, to_copy - first);
            }
            s_state.tier2_head = (s_state.tier2_head + to_copy) % s_state.tier2_size;
            s_state.tier2_count += to_copy;
            s_state.bytes_received_total += to_copy;
            xSemaphoreGive(s_state.tier2_lock);
        }

        // Re-mount this chunk for the next round.
        uhci_receive(s_state.uhci, s_state.tier1_chunk[evt.chunk_idx], s_state.tier1_chunk_size);
    }
}

// ----------------------------------------------------------------------------
// Public API.
// ----------------------------------------------------------------------------

swo_config_t swo_default_config(void)
{
    swo_config_t cfg = {
        .pin_swo_rx = SWO_DEFAULT_PIN_RX,
        .uart_port = SWO_DEFAULT_UART_PORT,
        .tier1_dram_bytes = SWO_DEFAULT_TIER1_BYTES,
        .tier2_psram_bytes = SWO_DEFAULT_TIER2_BYTES,
        .drain_task_core = SWO_DEFAULT_DRAIN_CORE,
        .drain_task_priority = SWO_DEFAULT_DRAIN_PRIORITY,
    };
    return cfg;
}

esp_err_t swo_init(uint32_t baud, swo_mode_t mode)
{
    if (s_state.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    swo_config_t cfg = swo_default_config();
    s_state.cfg = cfg;
    s_state.uart_port = (uart_port_t)cfg.uart_port;

    // Allocate tier-1 DRAM ping-pong buffers. UHCI requires DMA-capable internal RAM.
    s_state.tier1_chunk_size = cfg.tier1_dram_bytes / SWO_TIER1_CHUNKS;
    for (int i = 0; i < SWO_TIER1_CHUNKS; i++) {
        s_state.tier1_chunk[i] = heap_caps_malloc(s_state.tier1_chunk_size,
            MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        if (!s_state.tier1_chunk[i]) {
            ESP_LOGE(TAG, "tier-1 chunk %d alloc failed (%u bytes)", i, (unsigned)s_state.tier1_chunk_size);
            for (int j = 0; j < i; j++) {
                heap_caps_free(s_state.tier1_chunk[j]);
                s_state.tier1_chunk[j] = NULL;
            }
            return ESP_ERR_NO_MEM;
        }
    }

    // Allocate tier-2 PSRAM ring.
    s_state.tier2 = heap_caps_malloc(cfg.tier2_psram_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_state.tier2) {
        ESP_LOGW(TAG, "tier-2 PSRAM ring alloc failed (%u bytes); falling back to internal", (unsigned)cfg.tier2_psram_bytes);
        // Fallback to internal RAM for boards without PSRAM enabled (e.g. unit-test rig).
        size_t fallback_size = 32 * 1024;
        s_state.tier2 = heap_caps_malloc(fallback_size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        if (!s_state.tier2) {
            for (int i = 0; i < SWO_TIER1_CHUNKS; i++) {
                heap_caps_free(s_state.tier1_chunk[i]);
            }
            return ESP_ERR_NO_MEM;
        }
        s_state.tier2_size = fallback_size;
    } else {
        s_state.tier2_size = cfg.tier2_psram_bytes;
    }
    s_state.tier2_head = 0;
    s_state.tier2_tail = 0;
    s_state.tier2_count = 0;

    s_state.tier2_lock = xSemaphoreCreateMutex();
    s_state.completion_queue = xQueueCreate(8, sizeof(drain_event_t));
    if (!s_state.tier2_lock || !s_state.completion_queue) {
        ESP_LOGE(TAG, "lock/queue alloc failed");
        if (s_state.tier2_lock) vSemaphoreDelete(s_state.tier2_lock);
        if (s_state.completion_queue) vQueueDelete(s_state.completion_queue);
        heap_caps_free(s_state.tier2);
        for (int i = 0; i < SWO_TIER1_CHUNKS; i++) heap_caps_free(s_state.tier1_chunk[i]);
        memset(&s_state, 0, sizeof(s_state));
        return ESP_ERR_NO_MEM;
    }

    BaseType_t ok = xTaskCreatePinnedToCore(s_drain_task, "swo_drain",
        4096, NULL, cfg.drain_task_priority, &s_state.drain_task, cfg.drain_task_core);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "drain task create failed");
        vSemaphoreDelete(s_state.tier2_lock);
        vQueueDelete(s_state.completion_queue);
        heap_caps_free(s_state.tier2);
        for (int i = 0; i < SWO_TIER1_CHUNKS; i++) heap_caps_free(s_state.tier1_chunk[i]);
        memset(&s_state, 0, sizeof(s_state));
        return ESP_FAIL;
    }

    s_state.initialised = true;
    s_state.overruns_total = 0;
    s_state.overrun_latched = false;
    s_state.bytes_received_total = 0;

    ESP_LOGI(TAG, "init: pin=GPIO%d uart=%d tier1=%uKB(2x%uKB) tier2=%uKB",
             cfg.pin_swo_rx, (int)cfg.uart_port,
             (unsigned)(cfg.tier1_dram_bytes / 1024),
             (unsigned)(s_state.tier1_chunk_size / 1024),
             (unsigned)(s_state.tier2_size / 1024));

    if (mode != SWO_MODE_OFF && baud > 0) {
        return swo_start(baud, mode);
    }
    return ESP_OK;
}

esp_err_t swo_start(uint32_t baud, swo_mode_t mode)
{
    if (!s_state.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_state.capturing) {
        // Re-configure baud requires stop first.
        ESP_RETURN_ON_ERROR(swo_stop(), TAG, "swo_stop");
    }
    if (mode != SWO_MODE_UART) {
        // Manchester not supported in this rev.
        return ESP_ERR_NOT_SUPPORTED;
    }

    uart_config_t uart_cfg = {
        .baud_rate = (int)baud,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_RETURN_ON_ERROR(uart_param_config(s_state.uart_port, &uart_cfg), TAG, "uart_param_config");
    // RX-only; pin TX to -1 so we don't burn an output.
    ESP_RETURN_ON_ERROR(uart_set_pin(s_state.uart_port, -1, s_state.cfg.pin_swo_rx, -1, -1), TAG, "uart_set_pin");

    uhci_controller_config_t uhci_cfg = {
        .uart_port = s_state.uart_port,
        .tx_trans_queue_depth = 1,
        .max_transmit_size = 64,
        .max_receive_internal_mem = s_state.tier1_chunk_size,
        .dma_burst_size = 32,
        .max_packet_receive = s_state.tier1_chunk_size,
        .rx_eof_flags = {
            .idle_eof = 1,
            .length_eof = 1,
        },
    };
    ESP_RETURN_ON_ERROR(uhci_new_controller(&uhci_cfg, &s_state.uhci), TAG, "uhci_new_controller");

    uhci_event_callbacks_t cbs = { .on_rx_trans_event = s_uhci_rx_cb };
    ESP_RETURN_ON_ERROR(uhci_register_event_callbacks(s_state.uhci, &cbs, NULL), TAG, "uhci_register_event_callbacks");

    // Mount both ping-pong chunks. The first uhci_receive is the active DMA;
    // the second sits in the controller's queue waiting for the first to EOF.
    // (The IDF driver chains them automatically when there's queue space.)
    for (int i = 0; i < SWO_TIER1_CHUNKS; i++) {
        ESP_RETURN_ON_ERROR(uhci_receive(s_state.uhci, s_state.tier1_chunk[i], s_state.tier1_chunk_size),
                             TAG, "uhci_receive[%d]", i);
    }

    s_state.capturing = true;
    ESP_LOGI(TAG, "start: baud=%" PRIu32, baud);
    return ESP_OK;
}

esp_err_t swo_stop(void)
{
    if (!s_state.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_state.capturing) {
        return ESP_OK;
    }
    if (s_state.uhci) {
        uhci_del_controller(s_state.uhci);
        s_state.uhci = NULL;
    }
    uart_driver_delete(s_state.uart_port);
    s_state.capturing = false;
    return ESP_OK;
}

esp_err_t swo_deinit(void)
{
    if (!s_state.initialised) {
        return ESP_ERR_INVALID_STATE;
    }
    swo_stop();
    if (s_state.drain_task) {
        vTaskDelete(s_state.drain_task);
        s_state.drain_task = NULL;
    }
    if (s_state.tier2_lock) vSemaphoreDelete(s_state.tier2_lock);
    if (s_state.completion_queue) vQueueDelete(s_state.completion_queue);
    if (s_state.tier2) heap_caps_free(s_state.tier2);
    for (int i = 0; i < SWO_TIER1_CHUNKS; i++) {
        if (s_state.tier1_chunk[i]) heap_caps_free(s_state.tier1_chunk[i]);
    }
    memset(&s_state, 0, sizeof(s_state));
    return ESP_OK;
}

ssize_t swo_read(uint8_t *buf, size_t max_len)
{
    if (!s_state.initialised || buf == NULL) {
        return -1;
    }
    if (max_len == 0) {
        return 0;
    }
    size_t copied = 0;
    if (xSemaphoreTake(s_state.tier2_lock, portMAX_DELAY) != pdTRUE) {
        return -1;
    }
    size_t available = s_state.tier2_count;
    size_t to_copy = (available < max_len) ? available : max_len;
    if (to_copy > 0) {
        size_t first = s_state.tier2_size - s_state.tier2_tail;
        if (first > to_copy) first = to_copy;
        memcpy(buf, s_state.tier2 + s_state.tier2_tail, first);
        if (to_copy > first) {
            memcpy(buf + first, s_state.tier2, to_copy - first);
        }
        s_state.tier2_tail = (s_state.tier2_tail + to_copy) % s_state.tier2_size;
        s_state.tier2_count -= to_copy;
        copied = to_copy;
    }
    xSemaphoreGive(s_state.tier2_lock);
    return (ssize_t)copied;
}

uint32_t swo_overruns_total(void)
{
    return s_state.overruns_total;
}

size_t swo_bytes_buffered(void)
{
    return s_state.tier2_count;
}

size_t swo_bytes_received(void)
{
    return s_state.bytes_received_total;
}

void swo_overrun_clear_latched(void)
{
    s_state.overrun_latched = false;
}

bool swo_overrun_latched(void)
{
    return s_state.overrun_latched;
}
