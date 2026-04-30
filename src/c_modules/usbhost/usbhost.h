/* Annealage Pod: USB host (TinyUSB) abstraction.
 *
 * Phase 2 WS-A scope: declarations only. WS-B replaces the stub
 * implementation with the real TinyUSB host integration. The shape
 * of this API tracks referencea/esp-usbip-bridge/main/usb_backend.h
 * so the multiplexer can use it without knowing whether the backend
 * is a stub or the real TinyUSB host stack.
 *
 * This header is host-portable on purpose; usbhost.c on-target is
 * the only file that needs IDF / TinyUSB headers.
 */

#ifndef MPY_POD_USBHOST_H
#define MPY_POD_USBHOST_H

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "../usbip/usbip_protocol.h"
#include "../usbip/virtual_device.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Status codes returned by the URB submit functions. Negative errno
 * with the standard meaning. The Phase 2 WS-B stub returns -ENOSYS
 * to signal "real USB host not yet wired up"; the multiplexer maps
 * that into a transparent USBIP_RET_SUBMIT error so the host sees
 * an honest "no such function" rather than a silent zero-length
 * completion. */
#define USBHOST_ERR_NOT_IMPLEMENTED (-ENOSYS)

/* Bring up the USB host stack. The Phase 2 stub returns 0 without
 * doing anything; WS-B replaces this with TinyUSB host init. */
int usbhost_start(void);

/* Populate the array with the real-USB devices currently enumerated.
 * Returns the count copied (0 in the Phase 2 stub). */
size_t usbhost_get_devices(usbip_dev_record_t *out, size_t max);

/* Look up a real-USB device by busid. Returns true on hit. */
bool usbhost_get_device_by_busid(const char busid[USBIP_BUSID_SIZE],
                                 usbip_dev_record_t *out);

/* Submit a control transfer (EP0). The setup packet's direction
 * determines whether the data stage is OUT (out_data/out_len) or IN
 * (in_data/in_capacity, *in_len). Returns 0 on success, negative
 * errno on failure. The cancel flag, if non-NULL, is polled by the
 * backend and aborts the transfer when set. */
int usbhost_control_transfer(const char busid[USBIP_BUSID_SIZE],
                             const usbip_setup_packet_t *setup,
                             const uint8_t *out_data, size_t out_len,
                             uint8_t *in_data, size_t in_capacity, size_t *in_len,
                             volatile bool *cancel);

/* Submit a bulk transfer on a non-zero endpoint. ep_addr's high bit
 * carries direction (0x8N = IN, 0x0N = OUT). */
int usbhost_bulk_transfer(const char busid[USBIP_BUSID_SIZE],
                          uint8_t ep_addr,
                          const uint8_t *out_data, size_t out_len,
                          uint8_t *in_data, size_t in_capacity, size_t *in_len,
                          volatile bool *cancel);

/* Submit-order hook. The backend invokes wait_fn(ctx) immediately
 * before calling the underlying USB host submit, and advance_fn(ctx)
 * immediately after the submit returns (regardless of success). The
 * caller uses this to enforce per-pipe submit ordering when multiple
 * worker tasks may race into a single pipe; without it, the IDF sees
 * submits in worker-race order and the device-side byte stream is
 * scrambled. Both function pointers may be NULL to skip the hook. */
typedef struct usbhost_submit_order {
    void (*wait_fn)(void *ctx);
    void (*advance_fn)(void *ctx);
    void  *ctx;
} usbhost_submit_order_t;

/* Bulk transfer with an explicit submit-order hook. */
int usbhost_bulk_transfer_ordered(const char busid[USBIP_BUSID_SIZE],
                                  uint8_t ep_addr,
                                  const uint8_t *out_data, size_t out_len,
                                  uint8_t *in_data, size_t in_capacity, size_t *in_len,
                                  volatile bool *cancel,
                                  const usbhost_submit_order_t *order);

/* Submit an interrupt transfer; same shape as bulk. */
int usbhost_interrupt_transfer(const char busid[USBIP_BUSID_SIZE],
                               uint8_t ep_addr,
                               const uint8_t *out_data, size_t out_len,
                               uint8_t *in_data, size_t in_capacity, size_t *in_len,
                               volatile bool *cancel);

/* Interrupt transfer with an explicit submit-order hook. */
int usbhost_interrupt_transfer_ordered(const char busid[USBIP_BUSID_SIZE],
                                       uint8_t ep_addr,
                                       const uint8_t *out_data, size_t out_len,
                                       uint8_t *in_data, size_t in_capacity, size_t *in_len,
                                       volatile bool *cancel,
                                       const usbhost_submit_order_t *order);

/* Returns true if (busid, ep_num, direction) refers to an interrupt
 * endpoint on the real-device side. The multiplexer uses this to
 * choose between bulk and interrupt transfer paths. */
bool usbhost_is_interrupt_endpoint(const char busid[USBIP_BUSID_SIZE],
                                   uint8_t ep_num, uint8_t direction);

/* Toggle per-URB observability logging on the usbhost backend. When
 * enabled every transfer submit and completion emits one ESP_LOGI
 * line under the "usbhost" tag. Default off. */
void usbhost_set_verbose(bool enable);

/* True if per-URB verbose logging is currently enabled. */
bool usbhost_is_verbose(void);

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_USBHOST_H */
