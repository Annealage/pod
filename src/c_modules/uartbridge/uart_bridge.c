// Annealage Pod: UART bridge implementation.
//
// Concurrency model
// -----------------
//
// Three FreeRTOS tasks back the bridge once started:
//
//   1. accept task (always running): blocks in accept() on the
//      listening socket. On a new client it either rejects (replace
//      disabled and a session is live) or hands the socket to the
//      session, signalling the previous session to tear down.
//   2. tcp_rx task (per session): reads from the client socket, writes
//      to the UART TX FIFO. Tears down on socket EOF/error.
//   3. uart_rx task (per session): waits on the IDF UART event queue
//      (RINGBUF_TYPE_BYTEBUF) for UART_DATA / UART_BUFFER_FULL /
//      UART_FIFO_OVF, drains via uart_read_bytes(), pushes to client.
//
// All three are pinned to cfg.task_core (default PRO_CPU). The bridge
// is not latency-critical (spec.md §3 notes UART forwarder lives
// alongside MP), so co-locating with Wi-Fi/lwIP avoids the PRO->APP
// IPC hop.
//
// Shutdown semantics
// ------------------
//
// uart_bridge_stop() sets g_state.running = false, closes the listen
// socket and the active client socket (which unblocks accept/recv with
// errno=EBADF / 0), and posts a sentinel UART event so uart_rx wakes
// out of xQueueReceive. Each task runs its own cleanup path and signals
// done_sem on exit. uart_bridge_stop() blocks on done_sem with a
// bounded timeout, then deletes task handles.
//
// Buffering strategy
// ------------------
//
// IDF UART driver: rx_buf_size / tx_buf_size (default 2048 each, both
// at least UART_HW_FIFO_LEN(uart_num) + 1). UART RX overflow is
// reported via UART_BUFFER_FULL / UART_FIFO_OVF events; both flush
// the IDF ring and continue. The bridge does not impose its own
// host-side buffer beyond a 1 KB stack-allocated read window per
// task.
//
// DTR/RTS over the bridge
// -----------------------
//
// Per Appendix B's open question 4, DTR/RTS-over-the-bridge is not
// supported in rev1. Hardware RTS/CTS flow control on the local UART
// is supported (cfg.flow_control), but those are local-only.
// RFC2217-style modem control passthrough is explicitly excluded; the
// optional telnet mode (cfg.telnet) only carries baud/parity/data/stop
// renegotiation, not DTR/RTS state.

#include "uart_bridge.h"
#include "uart_bridge_telnet.h"

#include <arpa/inet.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/tcp.h>
#include <unistd.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "freertos/idf_additions.h"

#include "driver/uart.h"
#include "esp_log.h"

static const char *TAG = "uartbridge";

#define UART_BRIDGE_DEFAULT_PORT       2000
#define UART_BRIDGE_DEFAULT_UART       2
#define UART_BRIDGE_DEFAULT_BAUD       115200
#define UART_BRIDGE_DEFAULT_RX_BUF     2048
#define UART_BRIDGE_DEFAULT_TX_BUF     2048
#define UART_BRIDGE_UART_QUEUE_DEPTH   16
#define UART_BRIDGE_ACCEPT_BACKLOG     2
#define UART_BRIDGE_IO_CHUNK           512
#define UART_BRIDGE_TASK_STACK         4096
#define UART_BRIDGE_TASK_PRIO          5
#define UART_BRIDGE_STOP_TIMEOUT_MS    2000
#define UART_BRIDGE_TEST_FIFO_BYTES    8192

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------

typedef struct {
    bool running;
    bool test_mode;
    uart_bridge_config_t cfg;

    SemaphoreHandle_t lock;        // protects mutable fields below
    SemaphoreHandle_t accept_done; // posted when accept_task exits
    SemaphoreHandle_t tcp_rx_done; // posted when tcp_rx_task exits
    SemaphoreHandle_t uart_rx_done;// posted when uart_rx_task exits

    int listen_fd;
    int client_fd;
    bool session_stopping; // current session must tear down

    QueueHandle_t uart_evt_q;

    TaskHandle_t accept_task;
    TaskHandle_t tcp_rx_task;
    TaskHandle_t uart_rx_task;

    // Test-mode FIFOs (only allocated when uart_num == UART_BRIDGE_UART_TEST_LOOPBACK).
    SemaphoreHandle_t test_lock;
    SemaphoreHandle_t test_rx_avail;   // signalled when bytes arrive on the virtual UART
    SemaphoreHandle_t test_tx_avail;   // signalled when bytes are written by tcp_rx
    uint8_t *test_rx_fifo;             // bytes injected as UART RX
    size_t test_rx_head, test_rx_tail, test_rx_used;
    uint8_t *test_tx_fifo;             // bytes the tcp_rx side wrote toward UART
    size_t test_tx_head, test_tx_tail, test_tx_used;
} uart_bridge_state_t;

static uart_bridge_state_t g_state;

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

static void state_lock(void) { xSemaphoreTake(g_state.lock, portMAX_DELAY); }
static void state_unlock(void) { xSemaphoreGive(g_state.lock); }

static bool config_equal(const uart_bridge_config_t *a, const uart_bridge_config_t *b) {
    return memcmp(a, b, sizeof(*a)) == 0;
}

static esp_err_t validate_config(const uart_bridge_config_t *cfg) {
    if (cfg->uart_num != UART_BRIDGE_UART_TEST_LOOPBACK) {
        if (cfg->uart_num < 0 || cfg->uart_num >= UART_NUM_MAX) {
            return ESP_ERR_INVALID_ARG;
        }
    }
    if (cfg->tcp_port <= 0 || cfg->tcp_port > 65535) {
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->baud < 300 || cfg->baud > 921600) {
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->data_bits < 5 || cfg->data_bits > 8) {
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->stop_bits != 1 && cfg->stop_bits != 2) {
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->parity != UART_BRIDGE_PARITY_NONE
        && cfg->parity != UART_BRIDGE_PARITY_EVEN
        && cfg->parity != UART_BRIDGE_PARITY_ODD) {
        return ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
}

// Translate the bridge's data-bit count to ESP-IDF's uart_word_length_t.
static uart_word_length_t map_data_bits(int bits) {
    switch (bits) {
        case 5: return UART_DATA_5_BITS;
        case 6: return UART_DATA_6_BITS;
        case 7: return UART_DATA_7_BITS;
        default: return UART_DATA_8_BITS;
    }
}

static uart_stop_bits_t map_stop_bits(int stop) {
    return (stop == 2) ? UART_STOP_BITS_2 : UART_STOP_BITS_1;
}

static uart_parity_t map_parity(int parity) {
    switch (parity) {
        case UART_BRIDGE_PARITY_EVEN: return UART_PARITY_EVEN;
        case UART_BRIDGE_PARITY_ODD:  return UART_PARITY_ODD;
        default: return UART_PARITY_DISABLE;
    }
}

// ---------------------------------------------------------------------
// Test-mode FIFO
// ---------------------------------------------------------------------

static esp_err_t test_fifo_init(void) {
    g_state.test_lock = xSemaphoreCreateMutex();
    g_state.test_rx_avail = xSemaphoreCreateBinary();
    g_state.test_tx_avail = xSemaphoreCreateBinary();
    g_state.test_rx_fifo = malloc(UART_BRIDGE_TEST_FIFO_BYTES);
    g_state.test_tx_fifo = malloc(UART_BRIDGE_TEST_FIFO_BYTES);
    if (!g_state.test_lock || !g_state.test_rx_avail || !g_state.test_tx_avail
        || !g_state.test_rx_fifo || !g_state.test_tx_fifo) {
        return ESP_ERR_NO_MEM;
    }
    g_state.test_rx_head = g_state.test_rx_tail = g_state.test_rx_used = 0;
    g_state.test_tx_head = g_state.test_tx_tail = g_state.test_tx_used = 0;
    return ESP_OK;
}

static void test_fifo_release(void) {
    if (g_state.test_lock) { vSemaphoreDelete(g_state.test_lock); g_state.test_lock = NULL; }
    if (g_state.test_rx_avail) { vSemaphoreDelete(g_state.test_rx_avail); g_state.test_rx_avail = NULL; }
    if (g_state.test_tx_avail) { vSemaphoreDelete(g_state.test_tx_avail); g_state.test_tx_avail = NULL; }
    free(g_state.test_rx_fifo); g_state.test_rx_fifo = NULL;
    free(g_state.test_tx_fifo); g_state.test_tx_fifo = NULL;
    g_state.test_rx_head = g_state.test_rx_tail = g_state.test_rx_used = 0;
    g_state.test_tx_head = g_state.test_tx_tail = g_state.test_tx_used = 0;
}

static size_t test_rx_push(const uint8_t *buf, size_t len) {
    xSemaphoreTake(g_state.test_lock, portMAX_DELAY);
    size_t free_bytes = UART_BRIDGE_TEST_FIFO_BYTES - g_state.test_rx_used;
    size_t n = (len < free_bytes) ? len : free_bytes;
    for (size_t i = 0; i < n; ++i) {
        g_state.test_rx_fifo[g_state.test_rx_head] = buf[i];
        g_state.test_rx_head = (g_state.test_rx_head + 1) % UART_BRIDGE_TEST_FIFO_BYTES;
    }
    g_state.test_rx_used += n;
    xSemaphoreGive(g_state.test_lock);
    if (n > 0) {
        xSemaphoreGive(g_state.test_rx_avail);
    }
    return n;
}

static size_t test_rx_pop(uint8_t *buf, size_t cap, TickType_t timeout) {
    // Wait for data with timeout. The avail semaphore is binary so we
    // re-give it inside the lock when bytes remain.
    if (xSemaphoreTake(g_state.test_rx_avail, timeout) != pdTRUE) {
        return 0;
    }
    xSemaphoreTake(g_state.test_lock, portMAX_DELAY);
    size_t n = (g_state.test_rx_used < cap) ? g_state.test_rx_used : cap;
    for (size_t i = 0; i < n; ++i) {
        buf[i] = g_state.test_rx_fifo[g_state.test_rx_tail];
        g_state.test_rx_tail = (g_state.test_rx_tail + 1) % UART_BRIDGE_TEST_FIFO_BYTES;
    }
    g_state.test_rx_used -= n;
    if (g_state.test_rx_used > 0) {
        xSemaphoreGive(g_state.test_rx_avail);
    }
    xSemaphoreGive(g_state.test_lock);
    return n;
}

static size_t test_tx_push(const uint8_t *buf, size_t len) {
    xSemaphoreTake(g_state.test_lock, portMAX_DELAY);
    size_t free_bytes = UART_BRIDGE_TEST_FIFO_BYTES - g_state.test_tx_used;
    size_t n = (len < free_bytes) ? len : free_bytes;
    for (size_t i = 0; i < n; ++i) {
        g_state.test_tx_fifo[g_state.test_tx_head] = buf[i];
        g_state.test_tx_head = (g_state.test_tx_head + 1) % UART_BRIDGE_TEST_FIFO_BYTES;
    }
    g_state.test_tx_used += n;
    xSemaphoreGive(g_state.test_lock);
    if (n > 0) {
        xSemaphoreGive(g_state.test_tx_avail);
    }
    return n;
}

static size_t test_tx_pop(uint8_t *buf, size_t cap) {
    xSemaphoreTake(g_state.test_lock, portMAX_DELAY);
    size_t n = (g_state.test_tx_used < cap) ? g_state.test_tx_used : cap;
    for (size_t i = 0; i < n; ++i) {
        buf[i] = g_state.test_tx_fifo[g_state.test_tx_tail];
        g_state.test_tx_tail = (g_state.test_tx_tail + 1) % UART_BRIDGE_TEST_FIFO_BYTES;
    }
    g_state.test_tx_used -= n;
    xSemaphoreGive(g_state.test_lock);
    return n;
}

// ---------------------------------------------------------------------
// UART driver wrappers (production path)
// ---------------------------------------------------------------------

static esp_err_t uart_open(const uart_bridge_config_t *cfg) {
    if (g_state.test_mode) {
        return ESP_OK;
    }

    uart_config_t hw = {
        .baud_rate = cfg->baud,
        .data_bits = map_data_bits(cfg->data_bits),
        .parity = map_parity(cfg->parity),
        .stop_bits = map_stop_bits(cfg->stop_bits),
        .flow_ctrl = cfg->flow_control ? UART_HW_FLOWCTRL_CTS_RTS : UART_HW_FLOWCTRL_DISABLE,
        .rx_flow_ctrl_thresh = cfg->flow_control ? 122 : 0,
        .source_clk = UART_SCLK_DEFAULT,
    };

    int rxbuf = cfg->rx_buf_size > 0 ? cfg->rx_buf_size : UART_BRIDGE_DEFAULT_RX_BUF;
    int txbuf = cfg->tx_buf_size > 0 ? cfg->tx_buf_size : UART_BRIDGE_DEFAULT_TX_BUF;

    esp_err_t err = uart_driver_install(cfg->uart_num, rxbuf, txbuf,
                                         UART_BRIDGE_UART_QUEUE_DEPTH,
                                         &g_state.uart_evt_q, 0);
    if (err != ESP_OK) {
        return err;
    }
    err = uart_param_config(cfg->uart_num, &hw);
    if (err != ESP_OK) {
        uart_driver_delete(cfg->uart_num);
        return err;
    }
    int tx_pin = cfg->tx_pin >= 0 ? cfg->tx_pin : UART_PIN_NO_CHANGE;
    int rx_pin = cfg->rx_pin >= 0 ? cfg->rx_pin : UART_PIN_NO_CHANGE;
    int rts_pin = cfg->flow_control && cfg->rts_pin >= 0 ? cfg->rts_pin : UART_PIN_NO_CHANGE;
    int cts_pin = cfg->flow_control && cfg->cts_pin >= 0 ? cfg->cts_pin : UART_PIN_NO_CHANGE;
    err = uart_set_pin(cfg->uart_num, tx_pin, rx_pin, rts_pin, cts_pin);
    if (err != ESP_OK) {
        uart_driver_delete(cfg->uart_num);
        return err;
    }
    return ESP_OK;
}

static void uart_close(const uart_bridge_config_t *cfg) {
    if (g_state.test_mode) {
        return;
    }
    uart_driver_delete(cfg->uart_num);
    g_state.uart_evt_q = NULL;
}

// ---------------------------------------------------------------------
// Telnet / RFC2217 sub-stream handling
// ---------------------------------------------------------------------
//
// Pure decoder lives in uart_bridge_telnet.c so it can be exercised by
// host-side unit tests without an IDF. This wrapper applies the parsed
// subnegotiation parameters to the running UART configuration.

static void telnet_apply_sb_cb(const uart_bridge_telnet_sb_t *sb, void *user) {
    (void)user;
    uart_bridge_config_t patched = g_state.cfg;
    if (sb->baud >= 300 && sb->baud <= 921600) {
        patched.baud = sb->baud;
    }
    if (sb->data_bits >= 5 && sb->data_bits <= 8) {
        patched.data_bits = sb->data_bits;
    }
    if (sb->parity == 0 || sb->parity == 2 || sb->parity == 3) {
        patched.parity = sb->parity;
    }
    if (sb->stop_bits == 1 || sb->stop_bits == 2) {
        patched.stop_bits = sb->stop_bits;
    }
    if (validate_config(&patched) != ESP_OK) {
        return;
    }
    if (!g_state.test_mode) {
        if (patched.baud != g_state.cfg.baud) {
            uart_set_baudrate(patched.uart_num, patched.baud);
        }
        if (patched.data_bits != g_state.cfg.data_bits) {
            uart_set_word_length(patched.uart_num, map_data_bits(patched.data_bits));
        }
        if (patched.parity != g_state.cfg.parity) {
            uart_set_parity(patched.uart_num, map_parity(patched.parity));
        }
        if (patched.stop_bits != g_state.cfg.stop_bits) {
            uart_set_stop_bits(patched.uart_num, map_stop_bits(patched.stop_bits));
        }
    }
    state_lock();
    g_state.cfg = patched;
    state_unlock();
}

// ---------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------

static void close_client_locked(void) {
    if (g_state.client_fd >= 0) {
        shutdown(g_state.client_fd, SHUT_RDWR);
        close(g_state.client_fd);
        g_state.client_fd = -1;
    }
}

static int session_send_all(int fd, const uint8_t *buf, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t n = send(fd, buf + off, len - off, 0);
        if (n <= 0) {
            if (n < 0 && (errno == EINTR || errno == EAGAIN)) {
                continue;
            }
            return -1;
        }
        off += (size_t)n;
    }
    return 0;
}

static void tcp_rx_task(void *arg) {
    (void)arg;
    uint8_t buf[UART_BRIDGE_IO_CHUNK];
    uint8_t filt[UART_BRIDGE_IO_CHUNK];
    uart_bridge_telnet_t t;
    uart_bridge_telnet_init(&t, g_state.cfg.telnet, telnet_apply_sb_cb, NULL);

    int fd;
    state_lock();
    fd = g_state.client_fd;
    state_unlock();

    while (true) {
        if (!g_state.running || g_state.session_stopping || fd < 0) {
            break;
        }
        ssize_t n = recv(fd, buf, sizeof(buf), 0);
        if (n == 0) { break; }
        if (n < 0) {
            if (errno == EINTR) { continue; }
            break;
        }
        size_t flen = uart_bridge_telnet_filter(&t, buf, (size_t)n, filt, sizeof(filt));
        if (flen == 0) { continue; }
        if (g_state.test_mode) {
            test_tx_push(filt, flen);
        } else {
            int wrote = uart_write_bytes(g_state.cfg.uart_num, filt, flen);
            if (wrote < 0) { break; }
        }
    }

    state_lock();
    g_state.session_stopping = true;
    close_client_locked();
    state_unlock();
    xSemaphoreGive(g_state.tcp_rx_done);
    g_state.tcp_rx_task = NULL;
    vTaskDelete(NULL);
}

static void uart_rx_task(void *arg) {
    (void)arg;
    uint8_t buf[UART_BRIDGE_IO_CHUNK];

    int fd;
    state_lock();
    fd = g_state.client_fd;
    state_unlock();

    while (true) {
        if (!g_state.running || g_state.session_stopping) {
            break;
        }
        size_t n = 0;
        if (g_state.test_mode) {
            n = test_rx_pop(buf, sizeof(buf), pdMS_TO_TICKS(50));
            if (n == 0) { continue; }
        } else {
            uart_event_t evt;
            if (xQueueReceive(g_state.uart_evt_q, &evt, pdMS_TO_TICKS(50)) != pdTRUE) {
                continue;
            }
            switch (evt.type) {
                case UART_DATA: {
                    size_t avail = evt.size;
                    while (avail > 0) {
                        size_t chunk = avail > sizeof(buf) ? sizeof(buf) : avail;
                        int got = uart_read_bytes(g_state.cfg.uart_num, buf, chunk, pdMS_TO_TICKS(20));
                        if (got <= 0) { break; }
                        if (session_send_all(fd, buf, (size_t)got) != 0) {
                            goto session_end;
                        }
                        avail -= (size_t)got;
                    }
                    continue;
                }
                case UART_FIFO_OVF:
                case UART_BUFFER_FULL:
                    // DAPLink-style policy: log and continue.
                    ESP_LOGW(TAG, "UART overflow event %d on uart=%d",
                             (int)evt.type, g_state.cfg.uart_num);
                    uart_flush_input(g_state.cfg.uart_num);
                    xQueueReset(g_state.uart_evt_q);
                    continue;
                default:
                    continue;
            }
        }
        if (n > 0) {
            if (session_send_all(fd, buf, n) != 0) {
                break;
            }
        }
    }
session_end:
    state_lock();
    g_state.session_stopping = true;
    close_client_locked();
    state_unlock();
    xSemaphoreGive(g_state.uart_rx_done);
    g_state.uart_rx_task = NULL;
    vTaskDelete(NULL);
}

static esp_err_t spawn_session_tasks(void) {
    g_state.session_stopping = false;
    // Reset the done semaphores in case prior tasks left them signalled.
    xSemaphoreTake(g_state.tcp_rx_done, 0);
    xSemaphoreTake(g_state.uart_rx_done, 0);

    BaseType_t r = xTaskCreatePinnedToCore(tcp_rx_task, "ub_tcp_rx",
                                            UART_BRIDGE_TASK_STACK, NULL,
                                            UART_BRIDGE_TASK_PRIO,
                                            &g_state.tcp_rx_task,
                                            g_state.cfg.task_core);
    if (r != pdPASS) { return ESP_ERR_NO_MEM; }
    r = xTaskCreatePinnedToCore(uart_rx_task, "ub_uart_rx",
                                 UART_BRIDGE_TASK_STACK, NULL,
                                 UART_BRIDGE_TASK_PRIO,
                                 &g_state.uart_rx_task,
                                 g_state.cfg.task_core);
    if (r != pdPASS) { return ESP_ERR_NO_MEM; }
    return ESP_OK;
}

static void wait_session_tasks(void) {
    if (g_state.tcp_rx_task) {
        xSemaphoreTake(g_state.tcp_rx_done, pdMS_TO_TICKS(UART_BRIDGE_STOP_TIMEOUT_MS));
    }
    if (g_state.uart_rx_task) {
        xSemaphoreTake(g_state.uart_rx_done, pdMS_TO_TICKS(UART_BRIDGE_STOP_TIMEOUT_MS));
    }
    g_state.tcp_rx_task = NULL;
    g_state.uart_rx_task = NULL;
}

static int open_listener(int port) {
    int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (fd < 0) { return -1; }
    int yes = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons((uint16_t)port),
        .sin_addr = { .s_addr = htonl(INADDR_ANY) },
    };
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return -1;
    }
    if (listen(fd, UART_BRIDGE_ACCEPT_BACKLOG) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static void accept_task(void *arg) {
    (void)arg;
    while (g_state.running) {
        struct sockaddr_in cli;
        socklen_t cli_len = sizeof(cli);
        int new_fd = accept(g_state.listen_fd, (struct sockaddr *)&cli, &cli_len);
        if (!g_state.running) {
            if (new_fd >= 0) { close(new_fd); }
            break;
        }
        if (new_fd < 0) {
            if (errno == EINTR) { continue; }
            ESP_LOGW(TAG, "accept() errno=%d", errno);
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        // TCP_NODELAY for byte-at-a-time forwarding.
        int yes = 1;
        setsockopt(new_fd, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes));

        state_lock();
        bool busy = (g_state.client_fd >= 0);
        state_unlock();
        if (busy) {
            if (g_state.cfg.replace_client) {
                state_lock();
                g_state.session_stopping = true;
                close_client_locked();
                state_unlock();
                wait_session_tasks();
            } else {
                ESP_LOGI(TAG, "rejected: bridge already has a client");
                close(new_fd);
                continue;
            }
        }

        state_lock();
        g_state.client_fd = new_fd;
        g_state.session_stopping = false;
        state_unlock();

        if (spawn_session_tasks() != ESP_OK) {
            ESP_LOGE(TAG, "failed to spawn session tasks");
            state_lock();
            close_client_locked();
            state_unlock();
        }
    }
    state_lock();
    if (g_state.listen_fd >= 0) {
        close(g_state.listen_fd);
        g_state.listen_fd = -1;
    }
    close_client_locked();
    state_unlock();
    xSemaphoreGive(g_state.accept_done);
    g_state.accept_task = NULL;
    vTaskDelete(NULL);
}

// ---------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------

void uart_bridge_config_default(uart_bridge_config_t *cfg) {
    memset(cfg, 0, sizeof(*cfg));
    cfg->uart_num = UART_BRIDGE_DEFAULT_UART;
    cfg->tcp_port = UART_BRIDGE_DEFAULT_PORT;
    cfg->baud = UART_BRIDGE_DEFAULT_BAUD;
    cfg->data_bits = 8;
    cfg->parity = UART_BRIDGE_PARITY_NONE;
    cfg->stop_bits = 1;
    cfg->flow_control = false;
    cfg->tx_pin = -1;
    cfg->rx_pin = -1;
    cfg->rts_pin = -1;
    cfg->cts_pin = -1;
    cfg->replace_client = true;
    cfg->telnet = false;
    cfg->task_core = 0;
    cfg->rx_buf_size = 0;
    cfg->tx_buf_size = 0;
}

static esp_err_t state_init_once(void) {
    if (g_state.lock != NULL) { return ESP_OK; }
    g_state.lock = xSemaphoreCreateMutex();
    g_state.accept_done = xSemaphoreCreateBinary();
    g_state.tcp_rx_done = xSemaphoreCreateBinary();
    g_state.uart_rx_done = xSemaphoreCreateBinary();
    g_state.listen_fd = -1;
    g_state.client_fd = -1;
    if (!g_state.lock || !g_state.accept_done || !g_state.tcp_rx_done || !g_state.uart_rx_done) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t uart_bridge_start(const uart_bridge_config_t *cfg_in) {
    if (cfg_in == NULL) { return ESP_ERR_INVALID_ARG; }
    esp_err_t err = state_init_once();
    if (err != ESP_OK) { return err; }
    err = validate_config(cfg_in);
    if (err != ESP_OK) { return err; }

    if (g_state.running) {
        if (config_equal(&g_state.cfg, cfg_in)) {
            return ESP_OK;
        }
        return ESP_ERR_INVALID_STATE;
    }

    g_state.cfg = *cfg_in;
    g_state.test_mode = (cfg_in->uart_num == UART_BRIDGE_UART_TEST_LOOPBACK);

    if (g_state.test_mode) {
        err = test_fifo_init();
        if (err != ESP_OK) { return err; }
    } else {
        err = uart_open(&g_state.cfg);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "uart_open failed: %d", err);
            return err;
        }
    }

    g_state.listen_fd = open_listener(g_state.cfg.tcp_port);
    if (g_state.listen_fd < 0) {
        ESP_LOGE(TAG, "listener bind/listen failed: errno=%d", errno);
        if (g_state.test_mode) { test_fifo_release(); }
        else { uart_close(&g_state.cfg); }
        return ESP_FAIL;
    }

    g_state.running = true;
    xSemaphoreTake(g_state.accept_done, 0);
    BaseType_t r = xTaskCreatePinnedToCore(accept_task, "ub_accept",
                                            UART_BRIDGE_TASK_STACK, NULL,
                                            UART_BRIDGE_TASK_PRIO,
                                            &g_state.accept_task,
                                            g_state.cfg.task_core);
    if (r != pdPASS) {
        g_state.running = false;
        close(g_state.listen_fd);
        g_state.listen_fd = -1;
        if (g_state.test_mode) { test_fifo_release(); }
        else { uart_close(&g_state.cfg); }
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "started: uart=%d port=%d baud=%d %d%c%d flow=%d telnet=%d core=%d",
             g_state.cfg.uart_num, g_state.cfg.tcp_port, g_state.cfg.baud,
             g_state.cfg.data_bits,
             g_state.cfg.parity == UART_BRIDGE_PARITY_NONE ? 'N'
                : (g_state.cfg.parity == UART_BRIDGE_PARITY_EVEN ? 'E' : 'O'),
             g_state.cfg.stop_bits,
             (int)g_state.cfg.flow_control, (int)g_state.cfg.telnet,
             g_state.cfg.task_core);
    return ESP_OK;
}

esp_err_t uart_bridge_stop(void) {
    if (g_state.lock == NULL) { return ESP_OK; }
    if (!g_state.running) { return ESP_OK; }

    g_state.running = false;
    state_lock();
    g_state.session_stopping = true;
    if (g_state.listen_fd >= 0) {
        // shutdown unblocks any blocked accept() with errno=EBADF/0.
        shutdown(g_state.listen_fd, SHUT_RDWR);
    }
    close_client_locked();
    state_unlock();

    // Allow tasks to observe the flags and exit on their own.
    xSemaphoreTake(g_state.accept_done, pdMS_TO_TICKS(UART_BRIDGE_STOP_TIMEOUT_MS));
    wait_session_tasks();

    state_lock();
    if (g_state.listen_fd >= 0) {
        close(g_state.listen_fd);
        g_state.listen_fd = -1;
    }
    state_unlock();

    if (g_state.test_mode) {
        test_fifo_release();
    } else {
        uart_close(&g_state.cfg);
    }
    g_state.test_mode = false;
    return ESP_OK;
}

esp_err_t uart_bridge_get_config(uart_bridge_config_t *out) {
    if (out == NULL) { return ESP_ERR_INVALID_ARG; }
    if (!g_state.running) { return ESP_ERR_INVALID_STATE; }
    state_lock();
    *out = g_state.cfg;
    state_unlock();
    return ESP_OK;
}

int uart_bridge_client_count(void) {
    if (!g_state.running) { return 0; }
    state_lock();
    int n = (g_state.client_fd >= 0) ? 1 : 0;
    state_unlock();
    return n;
}

esp_err_t uart_bridge_run_session_fd(int client_fd) {
    if (!g_state.running) { return ESP_ERR_INVALID_STATE; }
    state_lock();
    if (g_state.client_fd >= 0) {
        state_unlock();
        return ESP_ERR_INVALID_STATE;
    }
    g_state.client_fd = client_fd;
    g_state.session_stopping = false;
    state_unlock();

    if (spawn_session_tasks() != ESP_OK) {
        state_lock();
        g_state.client_fd = -1;
        state_unlock();
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

int uart_bridge_test_inject_rx(const uint8_t *buf, size_t len) {
    if (!g_state.test_mode || g_state.test_rx_fifo == NULL) { return -1; }
    return (int)test_rx_push(buf, len);
}

int uart_bridge_test_drain_tx(uint8_t *buf, size_t cap) {
    if (!g_state.test_mode || g_state.test_tx_fifo == NULL) { return -1; }
    return (int)test_tx_pop(buf, cap);
}

esp_err_t uart_bridge_test_wait_tx(size_t min_bytes, uint32_t timeout_ms) {
    if (!g_state.test_mode) { return ESP_ERR_INVALID_STATE; }
    TickType_t start = xTaskGetTickCount();
    TickType_t deadline = start + pdMS_TO_TICKS(timeout_ms);
    while (true) {
        xSemaphoreTake(g_state.test_lock, portMAX_DELAY);
        bool ok = (g_state.test_tx_used >= min_bytes);
        xSemaphoreGive(g_state.test_lock);
        if (ok) { return ESP_OK; }
        if ((TickType_t)(xTaskGetTickCount() - start) > pdMS_TO_TICKS(timeout_ms)) {
            return ESP_ERR_TIMEOUT;
        }
        // Wait on the avail semaphore but cap the wait at remaining timeout.
        TickType_t now = xTaskGetTickCount();
        TickType_t wait = (deadline > now) ? (deadline - now) : 0;
        xSemaphoreTake(g_state.test_tx_avail, wait);
    }
}
