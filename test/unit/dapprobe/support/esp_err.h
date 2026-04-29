/* mpy-pod WS-C unit-test shim for ESP-IDF's esp_err.h.
 *
 * The host build is not linked against IDF, but WS-D's headers
 * (swd.h, swo.h) include "esp_err.h" for the esp_err_t typedef.
 * Provide just enough to satisfy the include.
 */

#ifndef MPY_POD_TEST_ESP_ERR_H
#define MPY_POD_TEST_ESP_ERR_H

#include <stdint.h>

typedef int32_t esp_err_t;

#define ESP_OK           0
#define ESP_FAIL         -1
#define ESP_ERR_NO_MEM   0x101
#define ESP_ERR_INVALID_ARG     0x102
#define ESP_ERR_INVALID_STATE   0x103
#define ESP_ERR_NOT_SUPPORTED   0x106

#endif
