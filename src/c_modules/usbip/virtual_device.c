/* Annealage Pod: virtual-device registry implementation.
 *
 * Adapted from referencea/esp-usbip-bridge/main/virtual_device.c.
 * Logging and IDF dependencies stripped so this translation unit is
 * compilable both on-target and against a host stub.
 */

#include "virtual_device.h"

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

static virtual_device_t *s_devices[USBIP_VIRTUAL_DEVICE_MAX];
static size_t s_device_count;

static bool busid_eq(const char *a, const char *b)
{
    /* Compare the full 32-byte field; busids are NUL-padded. */
    return memcmp(a, b, USBIP_BUSID_SIZE) == 0;
}

int usbip_register_virtual_device(virtual_device_t *dev)
{
    if (dev == NULL || dev->ops == NULL) {
        return -EINVAL;
    }
    if (s_device_count >= USBIP_VIRTUAL_DEVICE_MAX) {
        return -ENOMEM;
    }

    /* The reference assigns devnum sequentially starting at 1. We
     * preserve that policy: dapprobe lands on busid "2-1", a future
     * second virtual device would land on "2-2". */
    uint32_t devnum = (uint32_t)(s_device_count + 1u);
    dev->desc.present = true;
    dev->desc.busnum = USBIP_VIRTUAL_DEVICE_BUSNUM;
    dev->desc.devnum = devnum;

    memset(dev->desc.busid, 0, sizeof(dev->desc.busid));
    snprintf(dev->desc.busid, sizeof(dev->desc.busid),
             "%u-%" PRIu32, USBIP_VIRTUAL_DEVICE_BUSNUM, devnum);

    /* Mirror the kernel sysfs convention so `usbip list -r` logs a
     * recognisable path. The free-form value is informational. */
    if (dev->desc.path[0] == '\0') {
        snprintf(dev->desc.path, sizeof(dev->desc.path),
                 "/sys/devices/platform/annealage_pod/usb%u/%u-%" PRIu32,
                 USBIP_VIRTUAL_DEVICE_BUSNUM,
                 USBIP_VIRTUAL_DEVICE_BUSNUM, devnum);
    }

    /* Reject re-registration of the same busid. */
    for (size_t i = 0; i < s_device_count; i++) {
        if (busid_eq(s_devices[i]->desc.busid, dev->desc.busid)) {
            return -EEXIST;
        }
    }

    s_devices[s_device_count++] = dev;
    return 0;
}

virtual_device_t *usbip_find_virtual_device(const char busid[USBIP_BUSID_SIZE])
{
    if (busid == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < s_device_count; i++) {
        if (busid_eq(s_devices[i]->desc.busid, busid)) {
            return s_devices[i];
        }
    }
    return NULL;
}

size_t usbip_get_virtual_devices(usbip_dev_record_t *out_records, size_t max_records)
{
    if (out_records == NULL || max_records == 0) {
        return 0;
    }
    size_t copied = 0;
    for (size_t i = 0; i < s_device_count && copied < max_records; i++) {
        out_records[copied++] = s_devices[i]->desc;
    }
    return copied;
}

size_t usbip_virtual_device_count(void)
{
    return s_device_count;
}

void usbip_virtual_device_reset_for_test(void)
{
    for (size_t i = 0; i < USBIP_VIRTUAL_DEVICE_MAX; i++) {
        s_devices[i] = NULL;
    }
    s_device_count = 0;
}
