/* Annealage Pod: host-side records for devices the USB/IP server exports.
 *
 * usbip_dev_record_t holds an exported device's descriptor fields in host
 * byte order; usbip_proto.c packs it into the wire format for
 * OP_REP_DEVLIST and OP_REP_IMPORT.
 *
 * SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Annealage-firmware-exception
 */

#ifndef MPY_POD_USBIP_DEVICE_H
#define MPY_POD_USBIP_DEVICE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "usbip_protocol.h"

/* USB SETUP packet (USB 2.0 chapter 9), as carried in CMD_SUBMIT's
 * setup field. */
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

#endif /* MPY_POD_USBIP_DEVICE_H */
