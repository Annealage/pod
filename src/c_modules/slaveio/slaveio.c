// Annealage Pod: I2C-slave / SPI-slave personality implementation.
//
// See slaveio.h for the public API and docs/design/slaveio.md for the
// design notes (peripheral choice, ISR contract, notify dispatch path,
// mutual exclusion).
//
// Hardware peripherals (per Appendix A §A.5.1):
//   I2C-slave : I2C1 on GPIO17 (SDA, DIR-controlled) / GPIO18 (SCL, in)
//               DIR pin on GPIO21
//   SPI-slave : GPSPI3 on GPIO17 (MOSI) / GPIO18 (SCK) / GPIO38 (MISO)
//               / GPIO39 (CS); DIR pin on GPIO21 strapped fixed-in for
//               SDA/MOSI when SPI is active.
//
// The two personalities are mutually exclusive: starting one while the
// other is active returns ESP_ERR_INVALID_STATE. The DIR pin (GPIO21)
// drives the level-shifter for the shared SDA/MOSI line; for I2C-slave
// the SDA direction is controlled per-phase by the I2C peripheral
// itself (the LVC1T45 follows the open-drain ACK pulse via the OE
// strap); for SPI-slave it stays low (translator B->A, MOSI is master
// driven, S3 only inputs the line).
//
// IDF API notes (v5.5.1):
//   - i2c_slave V2 driver is enabled in the MP esp32 port's
//     sdkconfig.base (CONFIG_I2C_ENABLE_SLAVE_DRIVER_VERSION_2=y).
//     The V2 receive callback delivers (buffer, length); the V2
//     request callback fires when the master wants data. TX bytes
//     are pushed via i2c_slave_write().
//   - SPI3 (GPSPI3) is used because SPI2 (FSPI) is owned by WS-D.

#include "slaveio.h"
#include "slaveio_regtable.h"

#include <stdlib.h>
#include <string.h>

// Build-mode guard. The host-side unit test builds a TU that never
// includes this file (it stops at slaveio_regtable.c) so we keep the
// IDF-dependent path simple here.
#if defined(CONFIG_IDF_TARGET_ESP32S3) || defined(CONFIG_IDF_TARGET_ESP32S2) \
    || defined(CONFIG_IDF_TARGET_ESP32) || defined(CONFIG_IDF_TARGET_ESP32C3) \
    || defined(CONFIG_IDF_TARGET_ESP32C6) || defined(CONFIG_IDF_TARGET_ESP32H2) \
    || defined(CONFIG_IDF_TARGET_ESP32P4)
#  define SLAVEIO_HAS_IDF 1
#else
#  define SLAVEIO_HAS_IDF 0
#endif

#if SLAVEIO_HAS_IDF
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "driver/gpio.h"
#include "driver/i2c_slave.h"
#include "driver/spi_slave.h"
#include "driver/spi_common.h"
#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#endif

#if SLAVEIO_HAS_IDF
static const char *TAG = "slaveio";
#endif

// ---------------------------------------------------------------------
// Default pins (Appendix A §A.5.1)
// ---------------------------------------------------------------------

#define SLAVEIO_DEFAULT_I2C_PORT       1
#define SLAVEIO_DEFAULT_I2C_SDA_PIN    17
#define SLAVEIO_DEFAULT_I2C_SCL_PIN    18
#define SLAVEIO_DEFAULT_DIR_PIN        21
#define SLAVEIO_DEFAULT_SPI_MOSI_PIN   17
#define SLAVEIO_DEFAULT_SPI_SCK_PIN    18
#define SLAVEIO_DEFAULT_SPI_MISO_PIN   38
#define SLAVEIO_DEFAULT_SPI_CS_PIN     39

#define SLAVEIO_DISPATCH_TASK_STACK    4096
#define SLAVEIO_DISPATCH_TASK_PRIO     5
#define SLAVEIO_DISPATCH_TASK_CORE     1   // APP_CPU per architecture.md §3
#define SLAVEIO_I2C_PUMP_TASK_STACK    4096
#define SLAVEIO_I2C_PUMP_TASK_PRIO     6
#define SLAVEIO_SPI_PUMP_TASK_STACK    4096
#define SLAVEIO_SPI_PUMP_TASK_PRIO     6
#define SLAVEIO_NOTIFY_PUMP_DEPTH      8
#define SLAVEIO_I2C_RX_CHUNK           64
#define SLAVEIO_I2C_RING_DEPTH         512

// ---------------------------------------------------------------------
// Optional MP-callback hook
// ---------------------------------------------------------------------

static slaveio_dispatch_fn_t g_dispatch_fn = NULL;

void slaveio_set_dispatch_fn(slaveio_dispatch_fn_t fn) {
    g_dispatch_fn = fn;
}

// ---------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------

#if SLAVEIO_HAS_IDF
typedef struct {
    uint8_t bytes[SLAVEIO_I2C_RX_CHUNK];
    uint32_t length;
    uint8_t event;  // 0 = receive, 1 = request
} i2c_evt_t;
#endif

#define SLAVEIO_I2C_EVT_RECEIVE 0
#define SLAVEIO_I2C_EVT_REQUEST 1

typedef struct {
    bool inited;
    slaveio_personality_t active;

#if SLAVEIO_HAS_IDF
    SemaphoreHandle_t lock;
    SemaphoreHandle_t dispatch_wake;
    TaskHandle_t dispatch_task;
    bool dispatch_running;
#endif

    struct {
        bool active;
        uint8_t *read_buf;
        uint8_t *write_buf;
        size_t read_size;
        size_t write_size;
        slaveio_regtable_t reg;
        int sda_pin;
        int scl_pin;
        int dir_pin;
        uint16_t addr;
#if SLAVEIO_HAS_IDF
        i2c_slave_dev_handle_t handle;
        QueueHandle_t evt_q;
        TaskHandle_t pump_task;
        bool pump_running;
#endif
    } i2c;

    struct {
        bool active;
        uint8_t *read_buf;
        uint8_t *write_buf;
        size_t read_size;
        size_t write_size;
        slaveio_regtable_t reg;
        int miso_pin;
        int mosi_pin;
        int sck_pin;
        int cs_pin;
        int dir_pin;
        uint8_t mode;
        uint32_t freq_max_hz;
#if SLAVEIO_HAS_IDF
        TaskHandle_t pump_task;
        bool pump_running;
        uint8_t *tx_scratch;
        uint8_t *rx_scratch;
        size_t scratch_size;
#endif
    } spi;
} slaveio_state_t;

static slaveio_state_t g_state;

// ---------------------------------------------------------------------
// Locking helpers (no-op on host build)
// ---------------------------------------------------------------------

#if SLAVEIO_HAS_IDF
static void state_lock(void)   { xSemaphoreTake(g_state.lock, portMAX_DELAY); }
static void state_unlock(void) { xSemaphoreGive(g_state.lock); }
#else
static void state_lock(void)   { }
static void state_unlock(void) { }
#endif

// ---------------------------------------------------------------------
// Notify dispatch task
// ---------------------------------------------------------------------

#if SLAVEIO_HAS_IDF
static void dispatch_task(void *arg) {
    (void)arg;
    while (g_state.dispatch_running) {
        xSemaphoreTake(g_state.dispatch_wake, pdMS_TO_TICKS(100));
        for (;;) {
            uint32_t id = 0;
            bool got = false;
            state_lock();
            if (g_state.i2c.active && slaveio_regtable_pop_notify(&g_state.i2c.reg, &id)) {
                got = true;
            } else if (g_state.spi.active && slaveio_regtable_pop_notify(&g_state.spi.reg, &id)) {
                got = true;
            }
            state_unlock();
            if (!got) { break; }
            if (g_dispatch_fn) {
                g_dispatch_fn(id);
            }
        }
    }
    vTaskDelete(NULL);
}

static esp_err_t dispatch_task_start(void) {
    if (g_state.dispatch_running) { return ESP_OK; }
    g_state.dispatch_running = true;
    BaseType_t r = xTaskCreatePinnedToCore(dispatch_task, "slaveio_disp",
                                            SLAVEIO_DISPATCH_TASK_STACK, NULL,
                                            SLAVEIO_DISPATCH_TASK_PRIO,
                                            &g_state.dispatch_task,
                                            SLAVEIO_DISPATCH_TASK_CORE);
    if (r != pdPASS) {
        g_state.dispatch_running = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

static IRAM_ATTR void dispatch_wake_from_isr(BaseType_t *do_yield) {
    if (g_state.dispatch_wake) {
        xSemaphoreGiveFromISR(g_state.dispatch_wake, do_yield);
    }
}

static void dispatch_wake(void) {
    if (g_state.dispatch_wake) {
        xSemaphoreGive(g_state.dispatch_wake);
    }
}
#endif

// ---------------------------------------------------------------------
// Buffer alloc helpers
// ---------------------------------------------------------------------

static void free_i2c_buffers(void) {
    if (g_state.i2c.read_buf)  { free(g_state.i2c.read_buf);  g_state.i2c.read_buf  = NULL; }
    if (g_state.i2c.write_buf) { free(g_state.i2c.write_buf); g_state.i2c.write_buf = NULL; }
    g_state.i2c.read_size = 0;
    g_state.i2c.write_size = 0;
}

static void free_spi_buffers(void) {
    if (g_state.spi.read_buf)  { free(g_state.spi.read_buf);  g_state.spi.read_buf  = NULL; }
    if (g_state.spi.write_buf) { free(g_state.spi.write_buf); g_state.spi.write_buf = NULL; }
#if SLAVEIO_HAS_IDF
    if (g_state.spi.tx_scratch) { free(g_state.spi.tx_scratch); g_state.spi.tx_scratch = NULL; }
    if (g_state.spi.rx_scratch) { free(g_state.spi.rx_scratch); g_state.spi.rx_scratch = NULL; }
    g_state.spi.scratch_size = 0;
#endif
    g_state.spi.read_size = 0;
    g_state.spi.write_size = 0;
}

// ---------------------------------------------------------------------
// I2C personality
// ---------------------------------------------------------------------

#if SLAVEIO_HAS_IDF

// V2 receive callback. Fires from ISR after a master write completes
// (or buffer fills). evt_data->buffer points at the driver's internal
// receive buffer; length is the actual byte count.
static IRAM_ATTR bool i2c_on_receive_cb(i2c_slave_dev_handle_t handle,
                                        const i2c_slave_rx_done_event_data_t *evt,
                                        void *user_ctx) {
    (void)handle; (void)user_ctx;
    BaseType_t hpw = pdFALSE;
    i2c_evt_t e = { 0 };
    e.event = SLAVEIO_I2C_EVT_RECEIVE;
    uint32_t len = evt->length;
    if (len > SLAVEIO_I2C_RX_CHUNK) {
        len = SLAVEIO_I2C_RX_CHUNK;
    }
    e.length = len;
    if (evt->buffer && len > 0) {
        memcpy(e.bytes, evt->buffer, len);
    }
    if (g_state.i2c.evt_q) {
        xQueueSendFromISR(g_state.i2c.evt_q, &e, &hpw);
    }
    if (hpw == pdTRUE) {
        portYIELD_FROM_ISR();
    }
    return hpw == pdTRUE;
}

// V2 request callback. Fires from ISR when the master asks the slave
// for data and the TX FIFO is empty. We post an event for the pump
// task to top up the TX ring from read_table[reg_ptr+]; the master
// will clock-stretch (driver default) until bytes arrive.
static IRAM_ATTR bool i2c_on_request_cb(i2c_slave_dev_handle_t handle,
                                        const i2c_slave_request_event_data_t *evt,
                                        void *user_ctx) {
    (void)handle; (void)evt; (void)user_ctx;
    BaseType_t hpw = pdFALSE;
    i2c_evt_t e = { 0 };
    e.event = SLAVEIO_I2C_EVT_REQUEST;
    if (g_state.i2c.evt_q) {
        xQueueSendFromISR(g_state.i2c.evt_q, &e, &hpw);
    }
    if (hpw == pdTRUE) {
        portYIELD_FROM_ISR();
    }
    return hpw == pdTRUE;
}

static void i2c_pump_task(void *arg) {
    (void)arg;
    while (g_state.i2c.pump_running) {
        i2c_evt_t evt;
        if (xQueueReceive(g_state.i2c.evt_q, &evt, pdMS_TO_TICKS(100)) != pdTRUE) {
            continue;
        }

        if (evt.event == SLAVEIO_I2C_EVT_RECEIVE && evt.length > 0) {
            state_lock();
            slaveio_regtable_begin_write(&g_state.i2c.reg);
            slaveio_regtable_on_rx_chunk(&g_state.i2c.reg, evt.bytes, evt.length);
            slaveio_regtable_end_write(&g_state.i2c.reg);
            state_unlock();
            dispatch_wake();
        } else if (evt.event == SLAVEIO_I2C_EVT_REQUEST && g_state.i2c.handle) {
            // Master is reading. Pump bytes from read_table[reg_ptr+]
            // into the TX ring. The driver will clock-stretch until
            // bytes arrive.
            uint8_t tx_chunk[SLAVEIO_I2C_RX_CHUNK];
            state_lock();
            slaveio_regtable_begin_read(&g_state.i2c.reg);
            size_t n = slaveio_regtable_get_tx_chunk(&g_state.i2c.reg,
                                                     tx_chunk, sizeof(tx_chunk));
            slaveio_regtable_end_read(&g_state.i2c.reg);
            state_unlock();
            if (n > 0) {
                uint32_t wrote = 0;
                i2c_slave_write(g_state.i2c.handle, tx_chunk, (uint32_t)n, &wrote, 50);
            }
        }
    }
    vTaskDelete(NULL);
}

static esp_err_t configure_dir_pin(int dir_pin, int level) {
    gpio_config_t gc = {
        .pin_bit_mask = 1ULL << dir_pin,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&gc);
    if (err != ESP_OK) { return err; }
    return gpio_set_level(dir_pin, level);
}

#endif // SLAVEIO_HAS_IDF

esp_err_t slaveio_init(void) {
    if (g_state.inited) { return ESP_OK; }
#if SLAVEIO_HAS_IDF
    g_state.lock = xSemaphoreCreateMutex();
    g_state.dispatch_wake = xSemaphoreCreateBinary();
    if (!g_state.lock || !g_state.dispatch_wake) {
        return ESP_ERR_NO_MEM;
    }
    esp_err_t err = dispatch_task_start();
    if (err != ESP_OK) { return err; }
#endif
    g_state.inited = true;
    g_state.active = SLAVEIO_PERSONALITY_NONE;
    return ESP_OK;
}

slaveio_personality_t slaveio_active_personality(void) {
    return g_state.active;
}

esp_err_t slaveio_i2c_start(uint16_t addr,
                            int sda_pin, int scl_pin, int dir_pin,
                            size_t read_buf_size, size_t write_buf_size) {
    if (!g_state.inited) {
        esp_err_t err = slaveio_init();
        if (err != ESP_OK) { return err; }
    }
    if (read_buf_size == 0 || write_buf_size == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    state_lock();
    if (g_state.active == SLAVEIO_PERSONALITY_SPI) {
        state_unlock();
        return ESP_ERR_INVALID_STATE;
    }
    if (g_state.active == SLAVEIO_PERSONALITY_I2C) {
        state_unlock();
        return ESP_OK;
    }

    g_state.i2c.read_buf  = calloc(1, read_buf_size);
    g_state.i2c.write_buf = calloc(1, write_buf_size);
    if (!g_state.i2c.read_buf || !g_state.i2c.write_buf) {
        free_i2c_buffers();
        state_unlock();
        return ESP_ERR_NO_MEM;
    }
    g_state.i2c.read_size  = read_buf_size;
    g_state.i2c.write_size = write_buf_size;
    g_state.i2c.addr = addr;
    g_state.i2c.sda_pin = (sda_pin >= 0) ? sda_pin : SLAVEIO_DEFAULT_I2C_SDA_PIN;
    g_state.i2c.scl_pin = (scl_pin >= 0) ? scl_pin : SLAVEIO_DEFAULT_I2C_SCL_PIN;
    g_state.i2c.dir_pin = (dir_pin >= 0) ? dir_pin : SLAVEIO_DEFAULT_DIR_PIN;

    slaveio_regtable_init(&g_state.i2c.reg,
                          g_state.i2c.read_buf, g_state.i2c.read_size,
                          g_state.i2c.write_buf, g_state.i2c.write_size);

#if SLAVEIO_HAS_IDF
    esp_err_t derr = configure_dir_pin(g_state.i2c.dir_pin, 0);
    if (derr != ESP_OK) {
        free_i2c_buffers();
        state_unlock();
        return derr;
    }

    g_state.i2c.evt_q = xQueueCreate(SLAVEIO_NOTIFY_PUMP_DEPTH, sizeof(i2c_evt_t));
    if (!g_state.i2c.evt_q) {
        free_i2c_buffers();
        state_unlock();
        return ESP_ERR_NO_MEM;
    }

    i2c_slave_config_t cfg = {
        .i2c_port = SLAVEIO_DEFAULT_I2C_PORT,
        .sda_io_num = g_state.i2c.sda_pin,
        .scl_io_num = g_state.i2c.scl_pin,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .send_buf_depth = SLAVEIO_I2C_RING_DEPTH,
        .receive_buf_depth = SLAVEIO_I2C_RING_DEPTH,
        .slave_addr = addr,
        .addr_bit_len = I2C_ADDR_BIT_LEN_7,
        .intr_priority = 0,
    };

    esp_err_t err = i2c_new_slave_device(&cfg, &g_state.i2c.handle);
    if (err != ESP_OK) {
        vQueueDelete(g_state.i2c.evt_q);
        g_state.i2c.evt_q = NULL;
        free_i2c_buffers();
        state_unlock();
        return err;
    }

    i2c_slave_event_callbacks_t cbs = {
        .on_request = i2c_on_request_cb,
        .on_receive = i2c_on_receive_cb,
    };
    err = i2c_slave_register_event_callbacks(g_state.i2c.handle, &cbs, NULL);
    if (err != ESP_OK) {
        i2c_del_slave_device(g_state.i2c.handle);
        g_state.i2c.handle = NULL;
        vQueueDelete(g_state.i2c.evt_q);
        g_state.i2c.evt_q = NULL;
        free_i2c_buffers();
        state_unlock();
        return err;
    }

    g_state.i2c.pump_running = true;
    BaseType_t r = xTaskCreatePinnedToCore(i2c_pump_task, "slaveio_i2c",
                                            SLAVEIO_I2C_PUMP_TASK_STACK, NULL,
                                            SLAVEIO_I2C_PUMP_TASK_PRIO,
                                            &g_state.i2c.pump_task,
                                            SLAVEIO_DISPATCH_TASK_CORE);
    if (r != pdPASS) {
        g_state.i2c.pump_running = false;
        i2c_del_slave_device(g_state.i2c.handle);
        g_state.i2c.handle = NULL;
        vQueueDelete(g_state.i2c.evt_q);
        g_state.i2c.evt_q = NULL;
        free_i2c_buffers();
        state_unlock();
        return ESP_ERR_NO_MEM;
    }
#endif

    g_state.i2c.active = true;
    g_state.active = SLAVEIO_PERSONALITY_I2C;
    state_unlock();
#if SLAVEIO_HAS_IDF
    ESP_LOGI(TAG, "i2c-slave start addr=0x%02X sda=%d scl=%d dir=%d rd=%u wr=%u",
             addr, g_state.i2c.sda_pin, g_state.i2c.scl_pin, g_state.i2c.dir_pin,
             (unsigned)read_buf_size, (unsigned)write_buf_size);
#endif
    return ESP_OK;
}

esp_err_t slaveio_i2c_stop(void) {
    state_lock();
    if (!g_state.i2c.active) {
        state_unlock();
        return ESP_OK;
    }
#if SLAVEIO_HAS_IDF
    g_state.i2c.pump_running = false;
    state_unlock();
    for (int i = 0; i < 20 && g_state.i2c.pump_task; ++i) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    state_lock();
    if (g_state.i2c.handle) {
        i2c_del_slave_device(g_state.i2c.handle);
        g_state.i2c.handle = NULL;
    }
    if (g_state.i2c.evt_q) {
        vQueueDelete(g_state.i2c.evt_q);
        g_state.i2c.evt_q = NULL;
    }
    g_state.i2c.pump_task = NULL;
#endif
    free_i2c_buffers();
    g_state.i2c.active = false;
    g_state.active = SLAVEIO_PERSONALITY_NONE;
    state_unlock();
    return ESP_OK;
}

esp_err_t slaveio_i2c_get_read_buf(uint8_t **out_buf, size_t *out_size) {
    if (!g_state.i2c.active) { return ESP_ERR_INVALID_STATE; }
    if (out_buf)  { *out_buf  = g_state.i2c.read_buf;  }
    if (out_size) { *out_size = g_state.i2c.read_size; }
    return ESP_OK;
}

esp_err_t slaveio_i2c_get_write_buf(uint8_t **out_buf, size_t *out_size) {
    if (!g_state.i2c.active) { return ESP_ERR_INVALID_STATE; }
    if (out_buf)  { *out_buf  = g_state.i2c.write_buf;  }
    if (out_size) { *out_size = g_state.i2c.write_size; }
    return ESP_OK;
}

esp_err_t slaveio_i2c_register_notify(uint32_t start_offset,
                                       uint32_t end_offset,
                                       uint32_t callback_id) {
    if (!g_state.i2c.active) { return ESP_ERR_INVALID_STATE; }
    state_lock();
    int slot = slaveio_regtable_register_notify(&g_state.i2c.reg,
                                                 start_offset, end_offset,
                                                 callback_id);
    state_unlock();
    return (slot < 0) ? ESP_ERR_NO_MEM : ESP_OK;
}

esp_err_t slaveio_i2c_unregister_notify(uint32_t callback_id) {
    if (!g_state.i2c.active) { return ESP_ERR_INVALID_STATE; }
    state_lock();
    int rc = slaveio_regtable_unregister_notify(&g_state.i2c.reg, callback_id);
    state_unlock();
    return (rc < 0) ? ESP_ERR_NOT_FOUND : ESP_OK;
}

esp_err_t slaveio_i2c_pop_notify(uint32_t *out_id) {
    if (!g_state.i2c.active) { return ESP_ERR_INVALID_STATE; }
    state_lock();
    bool got = slaveio_regtable_pop_notify(&g_state.i2c.reg, out_id);
    state_unlock();
    return got ? ESP_OK : ESP_ERR_NOT_FOUND;
}

static void status_from_reg(const slaveio_regtable_t *r,
                            bool active,
                            slaveio_status_t *out) {
    memset(out, 0, sizeof(*out));
    out->active = active;
    if (!active) { return; }
    out->transfers_total = r->transfers_total;
    out->overflow_count = r->overflow_count;
    out->last_offset = r->last_xact_offset;
    out->last_length = r->last_xact_length;
    out->last_was_write = r->last_xact_was_write;
    out->reg_ptr = r->reg_ptr;
    out->notify_queue_dropped = r->notify_queue_dropped;
}

esp_err_t slaveio_i2c_status(slaveio_status_t *out) {
    if (!out) { return ESP_ERR_INVALID_ARG; }
    state_lock();
    status_from_reg(&g_state.i2c.reg, g_state.i2c.active, out);
    state_unlock();
    return ESP_OK;
}

// ---------------------------------------------------------------------
// SPI personality
// ---------------------------------------------------------------------

#if SLAVEIO_HAS_IDF

static void spi_post_trans_cb(spi_slave_transaction_t *trans) {
    (void)trans;
    // No-op. The SPI slave uses a worker task that blocks on
    // spi_slave_transmit(); we do not need an extra notify wake.
}

static void spi_pump_task(void *arg) {
    (void)arg;
    while (g_state.spi.pump_running) {
        size_t cap = g_state.spi.scratch_size;
        if (cap == 0 || !g_state.spi.tx_scratch || !g_state.spi.rx_scratch) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        // Pre-fill TX scratch with bytes from read_table[reg_ptr+].
        // The master may not clock the full buffer; trans_len reflects
        // what was actually clocked.
        state_lock();
        slaveio_regtable_begin_read(&g_state.spi.reg);
        size_t prefill = slaveio_regtable_get_tx_chunk(&g_state.spi.reg,
                                                        g_state.spi.tx_scratch, cap);
        state_unlock();
        if (prefill < cap) {
            memset(g_state.spi.tx_scratch + prefill, 0xFF, cap - prefill);
        }

        spi_slave_transaction_t t = {
            .length = cap * 8,
            .tx_buffer = g_state.spi.tx_scratch,
            .rx_buffer = g_state.spi.rx_scratch,
        };
        esp_err_t err = spi_slave_transmit(SPI3_HOST, &t, pdMS_TO_TICKS(100));
        if (err != ESP_OK) {
            continue;
        }
        size_t clocked_bytes = t.trans_len / 8;
        if (clocked_bytes == 0) {
            continue;
        }
        state_lock();
        slaveio_regtable_begin_write(&g_state.spi.reg);
        slaveio_regtable_on_rx_chunk(&g_state.spi.reg,
                                     g_state.spi.rx_scratch, clocked_bytes);
        slaveio_regtable_end_write(&g_state.spi.reg);
        slaveio_regtable_end_read(&g_state.spi.reg);
        state_unlock();
        dispatch_wake();
    }
    vTaskDelete(NULL);
}

#endif // SLAVEIO_HAS_IDF

esp_err_t slaveio_spi_start(uint8_t mode, uint32_t freq_max_hz,
                            int miso_pin, int mosi_pin, int sck_pin,
                            int cs_pin, int dir_pin,
                            size_t read_buf_size, size_t write_buf_size) {
    if (!g_state.inited) {
        esp_err_t err = slaveio_init();
        if (err != ESP_OK) { return err; }
    }
    if (read_buf_size == 0 || write_buf_size == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (mode > 3) { return ESP_ERR_INVALID_ARG; }

    state_lock();
    if (g_state.active == SLAVEIO_PERSONALITY_I2C) {
        state_unlock();
        return ESP_ERR_INVALID_STATE;
    }
    if (g_state.active == SLAVEIO_PERSONALITY_SPI) {
        state_unlock();
        return ESP_OK;
    }

    g_state.spi.read_buf  = calloc(1, read_buf_size);
    g_state.spi.write_buf = calloc(1, write_buf_size);
    if (!g_state.spi.read_buf || !g_state.spi.write_buf) {
        free_spi_buffers();
        state_unlock();
        return ESP_ERR_NO_MEM;
    }
    g_state.spi.read_size  = read_buf_size;
    g_state.spi.write_size = write_buf_size;
    g_state.spi.mode = mode;
    g_state.spi.freq_max_hz = freq_max_hz;
    g_state.spi.miso_pin = (miso_pin >= 0) ? miso_pin : SLAVEIO_DEFAULT_SPI_MISO_PIN;
    g_state.spi.mosi_pin = (mosi_pin >= 0) ? mosi_pin : SLAVEIO_DEFAULT_SPI_MOSI_PIN;
    g_state.spi.sck_pin  = (sck_pin  >= 0) ? sck_pin  : SLAVEIO_DEFAULT_SPI_SCK_PIN;
    g_state.spi.cs_pin   = (cs_pin   >= 0) ? cs_pin   : SLAVEIO_DEFAULT_SPI_CS_PIN;
    g_state.spi.dir_pin  = (dir_pin  >= 0) ? dir_pin  : SLAVEIO_DEFAULT_DIR_PIN;

    slaveio_regtable_init(&g_state.spi.reg,
                          g_state.spi.read_buf, g_state.spi.read_size,
                          g_state.spi.write_buf, g_state.spi.write_size);

#if SLAVEIO_HAS_IDF
    esp_err_t derr = configure_dir_pin(g_state.spi.dir_pin, 0);
    if (derr != ESP_OK) {
        free_spi_buffers();
        state_unlock();
        return derr;
    }

    size_t scratch = (read_buf_size + 3) & ~(size_t)3;
    if (scratch < 64) { scratch = 64; }
    if (scratch > 4096) { scratch = 4096; }
    g_state.spi.tx_scratch = heap_caps_aligned_alloc(4, scratch, MALLOC_CAP_DMA);
    g_state.spi.rx_scratch = heap_caps_aligned_alloc(4, scratch, MALLOC_CAP_DMA);
    if (!g_state.spi.tx_scratch || !g_state.spi.rx_scratch) {
        free_spi_buffers();
        state_unlock();
        return ESP_ERR_NO_MEM;
    }
    g_state.spi.scratch_size = scratch;

    spi_bus_config_t bus_cfg = {
        .mosi_io_num = g_state.spi.mosi_pin,
        .miso_io_num = g_state.spi.miso_pin,
        .sclk_io_num = g_state.spi.sck_pin,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = (int)scratch,
    };
    spi_slave_interface_config_t slv_cfg = {
        .spics_io_num = g_state.spi.cs_pin,
        .flags = 0,
        .queue_size = 2,
        .mode = mode,
        .post_setup_cb = NULL,
        .post_trans_cb = spi_post_trans_cb,
    };

    esp_err_t err = spi_slave_initialize(SPI3_HOST, &bus_cfg, &slv_cfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        free_spi_buffers();
        state_unlock();
        return err;
    }

    g_state.spi.pump_running = true;
    BaseType_t r = xTaskCreatePinnedToCore(spi_pump_task, "slaveio_spi",
                                            SLAVEIO_SPI_PUMP_TASK_STACK, NULL,
                                            SLAVEIO_SPI_PUMP_TASK_PRIO,
                                            &g_state.spi.pump_task,
                                            SLAVEIO_DISPATCH_TASK_CORE);
    if (r != pdPASS) {
        g_state.spi.pump_running = false;
        spi_slave_free(SPI3_HOST);
        free_spi_buffers();
        state_unlock();
        return ESP_ERR_NO_MEM;
    }
#endif

    g_state.spi.active = true;
    g_state.active = SLAVEIO_PERSONALITY_SPI;
    state_unlock();
#if SLAVEIO_HAS_IDF
    ESP_LOGI(TAG, "spi-slave start mode=%u fmax=%u miso=%d mosi=%d sck=%d cs=%d rd=%u wr=%u",
             (unsigned)mode, (unsigned)freq_max_hz,
             g_state.spi.miso_pin, g_state.spi.mosi_pin,
             g_state.spi.sck_pin, g_state.spi.cs_pin,
             (unsigned)read_buf_size, (unsigned)write_buf_size);
#endif
    return ESP_OK;
}

esp_err_t slaveio_spi_stop(void) {
    state_lock();
    if (!g_state.spi.active) {
        state_unlock();
        return ESP_OK;
    }
#if SLAVEIO_HAS_IDF
    g_state.spi.pump_running = false;
    state_unlock();
    for (int i = 0; i < 20 && g_state.spi.pump_task; ++i) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    state_lock();
    spi_slave_free(SPI3_HOST);
    g_state.spi.pump_task = NULL;
#endif
    free_spi_buffers();
    g_state.spi.active = false;
    g_state.active = SLAVEIO_PERSONALITY_NONE;
    state_unlock();
    return ESP_OK;
}

esp_err_t slaveio_spi_get_read_buf(uint8_t **out_buf, size_t *out_size) {
    if (!g_state.spi.active) { return ESP_ERR_INVALID_STATE; }
    if (out_buf)  { *out_buf  = g_state.spi.read_buf;  }
    if (out_size) { *out_size = g_state.spi.read_size; }
    return ESP_OK;
}

esp_err_t slaveio_spi_get_write_buf(uint8_t **out_buf, size_t *out_size) {
    if (!g_state.spi.active) { return ESP_ERR_INVALID_STATE; }
    if (out_buf)  { *out_buf  = g_state.spi.write_buf;  }
    if (out_size) { *out_size = g_state.spi.write_size; }
    return ESP_OK;
}

esp_err_t slaveio_spi_register_notify(uint32_t start_offset,
                                       uint32_t end_offset,
                                       uint32_t callback_id) {
    if (!g_state.spi.active) { return ESP_ERR_INVALID_STATE; }
    state_lock();
    int slot = slaveio_regtable_register_notify(&g_state.spi.reg,
                                                 start_offset, end_offset,
                                                 callback_id);
    state_unlock();
    return (slot < 0) ? ESP_ERR_NO_MEM : ESP_OK;
}

esp_err_t slaveio_spi_unregister_notify(uint32_t callback_id) {
    if (!g_state.spi.active) { return ESP_ERR_INVALID_STATE; }
    state_lock();
    int rc = slaveio_regtable_unregister_notify(&g_state.spi.reg, callback_id);
    state_unlock();
    return (rc < 0) ? ESP_ERR_NOT_FOUND : ESP_OK;
}

esp_err_t slaveio_spi_pop_notify(uint32_t *out_id) {
    if (!g_state.spi.active) { return ESP_ERR_INVALID_STATE; }
    state_lock();
    bool got = slaveio_regtable_pop_notify(&g_state.spi.reg, out_id);
    state_unlock();
    return got ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t slaveio_spi_status(slaveio_status_t *out) {
    if (!out) { return ESP_ERR_INVALID_ARG; }
    state_lock();
    status_from_reg(&g_state.spi.reg, g_state.spi.active, out);
    state_unlock();
    return ESP_OK;
}

void slaveio_start(void) {
    slaveio_init();
#if SLAVEIO_HAS_IDF
    ESP_LOGI(TAG, "slaveio module ready (no personality active)");
#endif
}
