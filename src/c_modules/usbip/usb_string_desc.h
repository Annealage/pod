/* Shared USB string descriptor encoder for mpy-pod virtual devices. */

#ifndef MPY_POD_USB_STRING_DESC_H
#define MPY_POD_USB_STRING_DESC_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define USB_STRING_DESC_TYPE 0x03u

static inline size_t encode_string_desc(uint8_t *buf, size_t cap, const char *s)
{
    size_t n = strlen(s);
    if (n > 30u) { n = 30u; }
    size_t total = 2u + n * 2u;
    if (total > cap) { return 0u; }
    buf[0] = (uint8_t)total;
    buf[1] = USB_STRING_DESC_TYPE;
    for (size_t i = 0u; i < n; i++) {
        buf[2u + i * 2u]      = (uint8_t)s[i];
        buf[2u + i * 2u + 1u] = 0x00u;
    }
    return total;
}

#endif /* MPY_POD_USB_STRING_DESC_H */
