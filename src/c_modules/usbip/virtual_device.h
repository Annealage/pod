/* Annealage Pod: virtual-device abstraction for the USB/IP server.
 *
 * Shape adapted from referencea/esp-usbip-bridge/main/virtual_device.h.
 * The reference defines a `virtual_device_t` that exposes
 * `control_transfer` and `data_transfer` ops; this header preserves
 * that surface so the WS-C dapprobe module can register itself with
 * the multiplexer in Phase 2 without further coupling.
 *
 * Differences from the upstream reference:
 *  - Uses `usbip_setup_packet_t` (defined here) instead of IDF's
 *    `usb_setup_packet_t` from `usb/usb_types_ch9.h`. The struct is
 *    layout-compatible with the IDF type but is host-portable, so the
 *    unit tests can include this header directly.
 *  - The registration entry returns the assigned busid via the
 *    pre-populated descriptor on success, no out-parameter games.
 *  - The synthetic bus number is fixed at 2 to match the design in
 *    research/usbip-multiplexing-design.md §1.1.3.
 */

#ifndef MPY_POD_USBIP_VIRTUAL_DEVICE_H
#define MPY_POD_USBIP_VIRTUAL_DEVICE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "usbip_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

#define USBIP_VIRTUAL_DEVICE_MAX 4u
#define USBIP_VIRTUAL_DEVICE_BUSNUM 2u

/* USB SETUP packet (Chapter 9). Layout matches the IDF
 * `usb_setup_packet_t`; defining it here keeps the protocol layer
 * portable for unit tests that do not link against IDF. */
typedef struct __attribute__((packed)) {
    uint8_t  bmRequestType;
    uint8_t  bRequest;
    uint16_t wValue;
    uint16_t wIndex;
    uint16_t wLength;
} usbip_setup_packet_t;

#define USBIP_REQUEST_DIR_IN 0x80u

/* Backend descriptor record. Mirrors the kernel-on-the-wire fields
 * the server emits in OP_REP_DEVLIST and OP_REP_IMPORT. The interface
 * triple list is used only for OP_REP_DEVLIST. */
typedef struct {
    uint8_t interface_class;
    uint8_t interface_subclass;
    uint8_t interface_protocol;
} usbip_iface_triple_t;

typedef struct {
    bool     present;
    char     path[USBIP_PATH_SIZE];
    char     busid[USBIP_BUSID_SIZE];
    uint32_t busnum;
    uint32_t devnum;
    uint32_t speed;
    uint16_t id_vendor;
    uint16_t id_product;
    uint16_t bcd_device;
    uint8_t  device_class;
    uint8_t  device_subclass;
    uint8_t  device_protocol;
    uint8_t  configuration_value;
    uint8_t  num_configurations;
    uint8_t  num_interfaces;
    usbip_iface_triple_t interfaces[USBIP_MAX_INTERFACES];
} usbip_dev_record_t;

typedef struct virtual_device virtual_device_t;

typedef struct {
    /* Handle a control transfer (EP0). out_data/out_len carry the data
     * stage for OUT control; in_data/in_capacity is the buffer for IN
     * control with *in_len set to bytes written. Return 0 on success,
     * negative errno on error (-EPIPE for STALL on unknown request). */
    int (*control_transfer)(virtual_device_t *dev,
                            const usbip_setup_packet_t *setup,
                            const uint8_t *out_data, size_t out_len,
                            uint8_t *in_data, size_t in_capacity, size_t *in_len);

    /* Handle a bulk or interrupt transfer on a non-zero endpoint.
     * ep_addr has direction bit set (0x8N for IN, 0x0N for OUT).
     * Return 0 on success, negative errno on error. */
    int (*data_transfer)(virtual_device_t *dev,
                         uint8_t ep_addr,
                         const uint8_t *out_data, size_t out_len,
                         uint8_t *in_data, size_t in_capacity, size_t *in_len);

    /* Optional: called when a host imports the device. May return
     * non-zero to refuse the import (e.g. if another host already
     * holds the device). NULL means "always allow". */
    int (*on_attach)(virtual_device_t *dev);

    /* Optional: called when the connection that imported the device
     * tears down. Symmetric with on_attach. NULL means "no-op". */
    void (*on_detach)(virtual_device_t *dev);
} virtual_device_ops_t;

struct virtual_device {
    const virtual_device_ops_t *ops;
    usbip_dev_record_t          desc;
    void                       *ctx;
};

/* Register a virtual device. The caller fills in `ops`, `ctx` and
 * the descriptor fields for vid/pid/class/etc; the registry assigns
 * `busnum=2`, the next available `devnum`, and writes the busid and
 * path strings. Re-registering with a busid already present returns
 * an error.
 *
 * Returns 0 on success, -ENOMEM if the slot table is full,
 * -EINVAL on bad arguments. */
int usbip_register_virtual_device(virtual_device_t *dev);

/* Look up a registered virtual device by 32-byte busid (NUL-padded).
 * Returns NULL if not found. */
virtual_device_t *usbip_find_virtual_device(const char busid[USBIP_BUSID_SIZE]);

/* Copy descriptor records for all registered virtual devices into the
 * provided array. Returns the number copied (capped at max_records). */
size_t usbip_get_virtual_devices(usbip_dev_record_t *out_records, size_t max_records);

/* Number of currently registered virtual devices. */
size_t usbip_virtual_device_count(void);

/* Test-only entry. Wipes the registry; not exported elsewhere. */
void usbip_virtual_device_reset_for_test(void);

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_USBIP_VIRTUAL_DEVICE_H */
