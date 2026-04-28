// Annealage Pod: TCP log socket C shim implementation.
//
// Spec §6.1: stdout dup'd to UART0 always; to TCP log socket when a
// client is connected. This shim owns the TCP-fan-out half. The
// UART0 path is preserved by the original vprintf hook saved at
// start() time and chain-called from our hook.
//
// Concurrency model
// -----------------
//   - accept_task: blocks in accept() on the listening socket. On
//     a new client, replaces the previous client (if any) so the
//     newest log viewer wins.
//   - log_vprintf: called by ESP-IDF on every ESP_LOGx invocation,
//     and by the fan-out hook in any caller that pipes stdout
//     through it. Writes to UART0 via the saved chain hook AND to
//     the active client socket (if any). Drops bytes if the client
//     send buffer is full; does not block log emission.
//
// The vprintf path runs in the caller's context (potentially an
// ISR-deferred task or a high-priority IDF task), so the send()
// call uses MSG_DONTWAIT to avoid blocking.

#include "ops_log.h"

#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "esp_err.h"
#include "esp_log.h"

#ifndef OPS_LOG_HOST_TEST_BUILD

// Host-test builds (test/unit/ops/) compile a stand-alone copy of
// this file with no IDF dependency. Production builds get the full
// stack.

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include "esp_log_write.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "ops_log";

#define OPS_LOG_ACCEPT_BACKLOG       2
#define OPS_LOG_ACCEPT_STACK         3072
#define OPS_LOG_ACCEPT_PRIO          5
#define OPS_LOG_VPRINTF_LINE_BUF     256

typedef struct {
    bool running;
    bool test_mode;
    uint16_t port;
    int task_core;

    SemaphoreHandle_t lock;
    int listen_fd;
    int client_fd;

    TaskHandle_t accept_task;
    SemaphoreHandle_t accept_done;

    vprintf_like_t prev_vprintf;
} ops_log_state_t;

static ops_log_state_t s = {
    .running = false,
    .test_mode = false,
    .listen_fd = -1,
    .client_fd = -1,
};

#define OPS_LOG_TEST_FIFO_BYTES 4096
static uint8_t s_test_fifo[OPS_LOG_TEST_FIFO_BYTES];
static size_t s_test_head, s_test_tail, s_test_used;

static void test_fifo_push(const uint8_t *buf, size_t len) {
    for (size_t i = 0; i < len; i++) {
        if (s_test_used == sizeof(s_test_fifo)) { return; }
        s_test_fifo[s_test_head] = buf[i];
        s_test_head = (s_test_head + 1) % sizeof(s_test_fifo);
        s_test_used++;
    }
}

static size_t test_fifo_pop(uint8_t *out, size_t cap) {
    size_t n = 0;
    while (n < cap && s_test_used > 0) {
        out[n++] = s_test_fifo[s_test_tail];
        s_test_tail = (s_test_tail + 1) % sizeof(s_test_fifo);
        s_test_used--;
    }
    return n;
}

static int ops_log_vprintf(const char *fmt, va_list args) {
    char buf[OPS_LOG_VPRINTF_LINE_BUF];
    va_list args_copy;
    va_copy(args_copy, args);
    int n = vsnprintf(buf, sizeof(buf), fmt, args_copy);
    va_end(args_copy);

    int chain_ret = 0;
    if (s.prev_vprintf) {
        // Re-render via the original (UART0) vprintf for the local
        // console path. This is safe because vprintf-likes accept
        // any va_list, and we did not mutate `args`.
        chain_ret = s.prev_vprintf(fmt, args);
    } else {
        chain_ret = vprintf(fmt, args);
    }

    if (n > 0) {
        size_t to_send = (n < (int)sizeof(buf)) ? (size_t)n : sizeof(buf) - 1;
        if (s.test_mode) {
            test_fifo_push((const uint8_t *)buf, to_send);
        } else {
            int fd = s.client_fd;
            if (fd >= 0) {
                int sent = send(fd, buf, to_send, MSG_DONTWAIT);
                if (sent < 0 && (errno == EPIPE || errno == ECONNRESET || errno == EBADF)) {
                    // Client disappeared. Mark slot empty so the
                    // accept task can replace it. We do not close
                    // here from the vprintf context.
                    s.client_fd = -1;
                }
            }
        }
    }

    return chain_ret;
}

static void close_client_locked(void) {
    if (s.client_fd >= 0) {
        close(s.client_fd);
        s.client_fd = -1;
    }
}

static void accept_task(void *arg) {
    (void)arg;
    while (s.running) {
        struct sockaddr_in cli_addr;
        socklen_t addr_len = sizeof(cli_addr);
        int cli = accept(s.listen_fd, (struct sockaddr *)&cli_addr, &addr_len);
        if (!s.running) {
            if (cli >= 0) { close(cli); }
            break;
        }
        if (cli < 0) {
            if (errno == EBADF || errno == EINVAL) { break; }
            continue;
        }

        xSemaphoreTake(s.lock, portMAX_DELAY);
        // Replace any existing client; newest viewer wins.
        close_client_locked();
        s.client_fd = cli;
        xSemaphoreGive(s.lock);

        ESP_LOGI(TAG, "log client attached");
    }
    xSemaphoreTake(s.lock, portMAX_DELAY);
    close_client_locked();
    xSemaphoreGive(s.lock);
    xSemaphoreGive(s.accept_done);
    vTaskDelete(NULL);
}

static esp_err_t bring_up_listener(uint16_t port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) { return ESP_FAIL; }

    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(port),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        return ESP_FAIL;
    }
    if (listen(fd, OPS_LOG_ACCEPT_BACKLOG) < 0) {
        close(fd);
        return ESP_FAIL;
    }
    s.listen_fd = fd;
    return ESP_OK;
}

esp_err_t ops_log_start(uint16_t port, int task_core) {
    if (s.lock == NULL) {
        s.lock = xSemaphoreCreateMutex();
        if (s.lock == NULL) { return ESP_ERR_NO_MEM; }
    }
    if (s.accept_done == NULL) {
        s.accept_done = xSemaphoreCreateBinary();
        if (s.accept_done == NULL) { return ESP_ERR_NO_MEM; }
    }

    if (s.running) {
        if (s.port == port) {
            return ESP_OK;
        }
        return ESP_ERR_INVALID_STATE;
    }

    s.port = port;
    s.task_core = task_core;
    s.test_mode = (port == 0);

    if (!s.test_mode) {
        esp_err_t err = bring_up_listener(port);
        if (err != ESP_OK) { return err; }
    }

    s.prev_vprintf = esp_log_set_vprintf(ops_log_vprintf);
    s.running = true;

    if (!s.test_mode) {
        BaseType_t ok;
        if (task_core < 0) {
            ok = xTaskCreate(accept_task, "ops_log",
                             OPS_LOG_ACCEPT_STACK, NULL,
                             OPS_LOG_ACCEPT_PRIO, &s.accept_task);
        } else {
            ok = xTaskCreatePinnedToCore(accept_task, "ops_log",
                                         OPS_LOG_ACCEPT_STACK, NULL,
                                         OPS_LOG_ACCEPT_PRIO,
                                         &s.accept_task, task_core);
        }
        if (ok != pdPASS) {
            esp_log_set_vprintf(s.prev_vprintf);
            close(s.listen_fd);
            s.listen_fd = -1;
            s.running = false;
            return ESP_ERR_NO_MEM;
        }
    }

    return ESP_OK;
}

esp_err_t ops_log_stop(void) {
    if (!s.running) { return ESP_OK; }

    s.running = false;
    if (s.prev_vprintf) {
        esp_log_set_vprintf(s.prev_vprintf);
        s.prev_vprintf = NULL;
    }
    xSemaphoreTake(s.lock, portMAX_DELAY);
    if (s.listen_fd >= 0) {
        shutdown(s.listen_fd, SHUT_RDWR);
        close(s.listen_fd);
        s.listen_fd = -1;
    }
    close_client_locked();
    xSemaphoreGive(s.lock);

    if (!s.test_mode && s.accept_task != NULL) {
        // Wait for the accept task to exit.
        if (xSemaphoreTake(s.accept_done, pdMS_TO_TICKS(2000)) == pdTRUE) {
            s.accept_task = NULL;
        }
    }

    s.test_mode = false;
    s.port = 0;

    // Drain the test FIFO.
    s_test_head = s_test_tail = s_test_used = 0;
    return ESP_OK;
}

int ops_log_client_count(void) {
    return (s.client_fd >= 0) ? 1 : 0;
}

void ops_log_test_fanout(const char *buf, size_t len) {
    if (!s.test_mode) { return; }
    test_fifo_push((const uint8_t *)buf, len);
}

size_t ops_log_test_drain(uint8_t *out, size_t cap) {
    if (!s.test_mode) { return 0; }
    return test_fifo_pop(out, cap);
}

#endif // OPS_LOG_HOST_TEST_BUILD
