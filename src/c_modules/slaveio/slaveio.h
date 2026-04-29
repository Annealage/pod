// Annealage Pod: I2C-slave / SPI-slave personality public API.
//
// One personality may be active at a time (mutual exclusion enforced
// here, see spec §4.8). The C surface is what the MicroPython binding
// in modslaveio.c calls; the MP wrapper in src/mpy/annealage_pod/slave.py is
// a thin shim above that.
//
// All entry points return ESP_OK on success or a negative ESP_ERR
// value on failure. The ESP-IDF v5.5 i2c_slave V2 driver and the
// spi_slave driver run the actual hardware path; this module:
//
//   - owns the read_table and write_table (allocated on start, freed
//     on stop);
//   - feeds the register-table state machine in slaveio_regtable.{c,h}
//     from the IDF driver event callbacks;
//   - dispatches notify-callbacks on a non-ISR FreeRTOS task so MP
//     callbacks run with the GIL holdable;
//   - statically configures the direction-control GPIOs per Appendix
//     A §A.5.1 (DIR_DUT_I2C_SDA on GPIO21).
//
// Default pin assignments (used when slaveio_*_start() is called from
// MicroPython without explicit pins) come from Appendix A:
//
//   I2C-slave:
//     SDA: GPIO17 (DUT_I2C_SDA, DIR-controlled translator)
//     SCL: GPIO18 (DUT_I2C_SCL, fixed-input translator)
//     DIR: GPIO21 (DIR_DUT_I2C_SDA)
//     Peripheral: I2C1 (I2C0 is the local INA228 bus)
//
//   SPI-slave:
//     MOSI:  GPIO17 (shared with I2C SDA, fixed-input)
//     SCK:   GPIO18 (shared with I2C SCL, fixed-input)
//     MISO:  GPIO38 (DUT_SPI_MISO, fixed-output)
//     CS:    GPIO39 (DUT_SPI_CS, fixed-input)
//     DIR:   GPIO21 (set fixed for SPI; I2C-mode bidir disarmed)
//     Peripheral: GPSPI3 (SPI2 reserved for SWD per WS-D)

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Status snapshot returned to MP via slaveio.{i2c,spi}_status().
typedef struct {
    bool active;
    uint64_t transfers_total;
    uint32_t overflow_count;
    uint32_t last_offset;
    uint32_t last_length;
    bool last_was_write;
    uint32_t reg_ptr;
    uint32_t notify_queue_dropped;
} slaveio_status_t;

typedef enum {
    SLAVEIO_PERSONALITY_NONE = 0,
    SLAVEIO_PERSONALITY_I2C  = 1,
    SLAVEIO_PERSONALITY_SPI  = 2,
} slaveio_personality_t;

// One-time module init. Idempotent; safe to call from MP module init.
// Must be called before any of the personality entry points.
esp_err_t slaveio_init(void);

// Which personality (if any) is active right now.
slaveio_personality_t slaveio_active_personality(void);

// Start the I2C-slave personality.
//
// addr            : 7-bit slave address.
// sda_pin/scl_pin : GPIOs; pass -1 to use Appendix A defaults
//                   (GPIO17/GPIO18).
// dir_pin         : DIR pin for the SDA bidir translator; -1 for the
//                   default (GPIO21).
// read_buf_size   : size of the read-table; allocated by this module.
// write_buf_size  : size of the write-table; allocated by this module.
//
// Returns ESP_ERR_INVALID_STATE if the SPI personality is currently
// active.
esp_err_t slaveio_i2c_start(uint16_t addr,
                            int sda_pin, int scl_pin, int dir_pin,
                            size_t read_buf_size, size_t write_buf_size);

// Stop the I2C-slave personality. Safe to call when stopped.
esp_err_t slaveio_i2c_stop(void);

// Get pointers and sizes of the read and write tables. Returns
// ESP_ERR_INVALID_STATE if the I2C personality is not active. The
// pointers are valid until slaveio_i2c_stop().
esp_err_t slaveio_i2c_get_read_buf(uint8_t **out_buf, size_t *out_size);
esp_err_t slaveio_i2c_get_write_buf(uint8_t **out_buf, size_t *out_size);

// Register a notify callback. The id is opaque to this module; the
// MP binding allocates it. Returns ESP_OK or ESP_ERR_NO_MEM.
esp_err_t slaveio_i2c_register_notify(uint32_t start_offset,
                                       uint32_t end_offset,
                                       uint32_t callback_id);

// Remove a notify callback by id.
esp_err_t slaveio_i2c_unregister_notify(uint32_t callback_id);

// Pop one pending notify-id from the dispatch queue. Non-blocking.
// Returns ESP_OK and writes *out_id, or ESP_ERR_NOT_FOUND if empty.
// Used by the MP binding's polling helper or the dispatch task.
esp_err_t slaveio_i2c_pop_notify(uint32_t *out_id);

// Status snapshot.
esp_err_t slaveio_i2c_status(slaveio_status_t *out);

// SPI mirrors. mode is 0..3 (CPOL/CPHA pair); freq_max_hz is the cap
// the slave is willing to accept (informational on slave; the master's
// clock drives the bus).
esp_err_t slaveio_spi_start(uint8_t mode, uint32_t freq_max_hz,
                            int miso_pin, int mosi_pin, int sck_pin,
                            int cs_pin, int dir_pin,
                            size_t read_buf_size, size_t write_buf_size);

esp_err_t slaveio_spi_stop(void);

esp_err_t slaveio_spi_get_read_buf(uint8_t **out_buf, size_t *out_size);
esp_err_t slaveio_spi_get_write_buf(uint8_t **out_buf, size_t *out_size);

esp_err_t slaveio_spi_register_notify(uint32_t start_offset,
                                       uint32_t end_offset,
                                       uint32_t callback_id);
esp_err_t slaveio_spi_unregister_notify(uint32_t callback_id);
esp_err_t slaveio_spi_pop_notify(uint32_t *out_id);
esp_err_t slaveio_spi_status(slaveio_status_t *out);

// Skeleton compatibility: keep the Phase 1 entry point so any caller
// linking against the old ABI does not break the build. No-op once
// slaveio_init() has run.
void slaveio_start(void);

// Wire the MP binding's dispatch thunk. The C-side dispatch task calls
// fn(callback_id) on a non-ISR FreeRTOS task whenever a notify fires.
// Called once from MP module init.
typedef void (*slaveio_dispatch_fn_t)(uint32_t callback_id);
void slaveio_set_dispatch_fn(slaveio_dispatch_fn_t fn);

#ifdef __cplusplus
}
#endif
