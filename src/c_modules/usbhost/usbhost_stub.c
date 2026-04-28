/* Annealage Pod: Phase 2 stub for the USB host backend.
 *
 * WS-A (this workstream) integrates against this stub so the
 * multiplexer can be built, linked and unit-tested without WS-B's
 * TinyUSB host implementation. WS-B replaces this file (or
 * #defines a guard so the stub link symbols stay out of a real
 * build) once the host stack is wired in.
 *
 * Every function returns -ENOSYS or empty results so the URB
 * dispatch path produces a USBIP_RET_SUBMIT with a transparent
 * error code. The multiplexer never silently swallows a stubbed
 * call.
 */

#include "usbhost.h"

#include <errno.h>
#include <string.h>

int usbhost_start(void)
{
    /* WS-B replaces this with TinyUSB host init. The stub returns
     * 0 so the rest of the firmware can boot; URBs are still
     * rejected at submit-time. */
    return 0;
}

size_t usbhost_get_devices(usbip_dev_record_t *out, size_t max)
{
    (void)out;
    (void)max;
    return 0;
}

bool usbhost_get_device_by_busid(const char busid[USBIP_BUSID_SIZE],
                                 usbip_dev_record_t *out)
{
    (void)busid;
    (void)out;
    return false;
}

int usbhost_control_transfer(const char busid[USBIP_BUSID_SIZE],
                             const usbip_setup_packet_t *setup,
                             const uint8_t *out_data, size_t out_len,
                             uint8_t *in_data, size_t in_capacity, size_t *in_len,
                             volatile bool *cancel)
{
    (void)busid;
    (void)setup;
    (void)out_data;
    (void)out_len;
    (void)in_data;
    (void)in_capacity;
    (void)cancel;
    if (in_len) {
        *in_len = 0;
    }
    return -ENOSYS;
}

int usbhost_bulk_transfer(const char busid[USBIP_BUSID_SIZE],
                          uint8_t ep_addr,
                          const uint8_t *out_data, size_t out_len,
                          uint8_t *in_data, size_t in_capacity, size_t *in_len,
                          volatile bool *cancel)
{
    (void)busid;
    (void)ep_addr;
    (void)out_data;
    (void)out_len;
    (void)in_data;
    (void)in_capacity;
    (void)cancel;
    if (in_len) {
        *in_len = 0;
    }
    return -ENOSYS;
}

int usbhost_interrupt_transfer(const char busid[USBIP_BUSID_SIZE],
                               uint8_t ep_addr,
                               const uint8_t *out_data, size_t out_len,
                               uint8_t *in_data, size_t in_capacity, size_t *in_len,
                               volatile bool *cancel)
{
    (void)busid;
    (void)ep_addr;
    (void)out_data;
    (void)out_len;
    (void)in_data;
    (void)in_capacity;
    (void)cancel;
    if (in_len) {
        *in_len = 0;
    }
    return -ENOSYS;
}

bool usbhost_is_interrupt_endpoint(const char busid[USBIP_BUSID_SIZE],
                                   uint8_t ep_num, uint8_t direction)
{
    (void)busid;
    (void)ep_num;
    (void)direction;
    return false;
}
