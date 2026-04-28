// Annealage Pod: Task watchdog C shim public API.
//
// Wraps esp_task_wdt for use from the MP main task. C-side
// long-running tasks subscribe via esp_task_wdt_add() / _add_user()
// directly; this shim only owns the MP-main-task subscription.
//
// See spec.md §6.2 and docs/design/ops.md for the subscription
// model.

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Subscribe the calling task (typically the MP main task) to the
// task watchdog with a `timeout_ms` deadline. Reconfigures the TWDT
// if the requested timeout differs from the current setting; the
// TWDT itself is initialised by IDF at boot via
// CONFIG_ESP_TASK_WDT_INIT (see boards/.../sdkconfig.board).
//
// Idempotent: subscribing a task already subscribed is a no-op
// (returns ESP_OK).
//
// Returns ESP_OK on success; otherwise the IDF esp_err_t.
esp_err_t ops_wdt_subscribe(uint32_t timeout_ms);

// Reset the watchdog on behalf of the calling task. Must be called
// at least once per timeout window from the MP asyncio tick.
// Returns ESP_OK on success.
esp_err_t ops_wdt_kick(void);

// Unsubscribe the calling task from the watchdog. Returns ESP_OK on
// success; ESP_ERR_NOT_FOUND if the task was never subscribed.
esp_err_t ops_wdt_unsubscribe(void);

// Query subscription state of the calling task. Returns true when
// subscribed.
bool ops_wdt_is_subscribed(void);

#ifdef __cplusplus
}
#endif
