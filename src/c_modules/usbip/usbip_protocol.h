/* Annealage Pod: USB/IP wire format.
 *
 * Written from the USB/IP protocol specification in the Linux kernel
 * documentation, Documentation/usb/usbip_protocol.rst
 * (https://docs.kernel.org/usb/usbip_protocol.html). Field names are the
 * spec's; type names follow the kernel's own structure names so the two
 * can be read side by side.
 *
 * Every multi-byte field is big-endian on the wire. These packed structs
 * describe the wire layout exactly, so a received buffer can be viewed
 * through them directly; usbip_proto.c does the byte-order conversion.
 * The offset assertions at the end pin each struct to the spec's tables.
 *
 * Host-portable (no lwIP or SDK dependency) so the unit-test
 * harness under test/unit/usbip/ can include it directly.
 *
 * SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Annealage-firmware-exception
 */

#ifndef MPY_POD_USBIP_PROTOCOL_H
#define MPY_POD_USBIP_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

/* ---- Connection-level constants -------------------------------------- */

#define USBIP_TCP_PORT 3240
/* Protocol version carried in every OP_* message: 1.1.1, BCD. */
#define USBIP_VERSION 0x0111

/* ---- OP_* messages (device list and import, before URB traffic) ------ */

#define USBIP_OP_REQ_DEVLIST 0x8005
#define USBIP_OP_REP_DEVLIST 0x0005
#define USBIP_OP_REQ_IMPORT 0x8003
#define USBIP_OP_REP_IMPORT 0x0003

/* Sizes of the fixed-length strings in a device description. */
#define USBIP_PATH_SIZE  256u
#define USBIP_BUSID_SIZE 32u

/* Header shared by every OP_* request and reply. status is 0 for OK;
 * requests send 0. */
typedef struct __attribute__((packed)) {
    uint16_t version;
    uint16_t code;
    uint32_t status;
} usbip_op_common_t;

/* One exported device, as it appears in OP_REP_DEVLIST (followed there by
 * bNumInterfaces usbip_usb_interface_t records) and in OP_REP_IMPORT
 * (with no interface records). */
typedef struct __attribute__((packed)) {
    char     path[USBIP_PATH_SIZE];    /* sysfs path, NUL-padded */
    char     busid[USBIP_BUSID_SIZE];  /* e.g. "2-1", NUL-padded */
    uint32_t busnum;
    uint32_t devnum;
    uint32_t speed;
    uint16_t idVendor;
    uint16_t idProduct;
    uint16_t bcdDevice;
    uint8_t  bDeviceClass;
    uint8_t  bDeviceSubClass;
    uint8_t  bDeviceProtocol;
    uint8_t  bConfigurationValue;
    uint8_t  bNumConfigurations;
    uint8_t  bNumInterfaces;
} usbip_usb_device_t;

/* One interface of an exported device, in OP_REP_DEVLIST only. */
typedef struct __attribute__((packed)) {
    uint8_t bInterfaceClass;
    uint8_t bInterfaceSubClass;
    uint8_t bInterfaceProtocol;
    uint8_t padding;                   /* shall be zero */
} usbip_usb_interface_t;

/* ---- URB traffic (after a successful import) -------------------------- */

#define USBIP_CMD_SUBMIT 0x00000001u
#define USBIP_CMD_UNLINK 0x00000002u
#define USBIP_RET_SUBMIT 0x00000003u
#define USBIP_RET_UNLINK 0x00000004u

/* header_basic.direction */
#define USBIP_DIR_OUT 0u
#define USBIP_DIR_IN  1u

/* number_of_packets value the spec requires for a non-isochronous URB.
 * Some clients send 0 instead; the decoder accepts both. */
#define USBIP_NUMBER_OF_PACKETS_NON_ISO 0xFFFFFFFFu

/* First 20 bytes of every URB message. */
typedef struct __attribute__((packed)) {
    uint32_t command;
    uint32_t seqnum;
    uint32_t devid;                    /* (busnum << 16) | devnum */
    uint32_t direction;
    uint32_t ep;
} usbip_header_basic_t;

typedef struct __attribute__((packed)) {
    uint32_t transfer_flags;
    int32_t  transfer_buffer_length;
    int32_t  start_frame;
    int32_t  number_of_packets;
    int32_t  interval;
    uint8_t  setup[8];
} usbip_header_cmd_submit_t;

typedef struct __attribute__((packed)) {
    int32_t  status;
    uint32_t actual_length;
    int32_t  start_frame;
    int32_t  number_of_packets;
    int32_t  error_count;
    uint8_t  padding[8];               /* shall be zero */
} usbip_header_ret_submit_t;

typedef struct __attribute__((packed)) {
    uint32_t unlink_seqnum;            /* seqnum of the SUBMIT to unlink */
    uint8_t  padding[24];              /* shall be zero */
} usbip_header_cmd_unlink_t;

typedef struct __attribute__((packed)) {
    int32_t status;
    uint8_t padding[24];               /* shall be zero */
} usbip_header_ret_unlink_t;

/* The fixed 48-byte URB header. A transfer buffer and, for isochronous
 * URBs, packet descriptors follow it on the wire. */
typedef struct __attribute__((packed)) {
    usbip_header_basic_t base;
    union {
        usbip_header_cmd_submit_t cmd_submit;
        usbip_header_ret_submit_t ret_submit;
        usbip_header_cmd_unlink_t cmd_unlink;
        usbip_header_ret_unlink_t ret_unlink;
    } u;
} usbip_header_t;

/* ---- Pod implementation limit, not part of the protocol --------------- */

/* Most interfaces the pod will describe for one exported device. */
#define USBIP_MAX_INTERFACES 8u

/* ---- Layout checks against the spec's offset tables ------------------- */

#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
#define USBIP_ASSERT_OFFSET(type, member, off) \
    _Static_assert(offsetof(type, member) == (off), #type "." #member " offset")

_Static_assert(sizeof(usbip_op_common_t) == 8, "op_common size");

/* Device description: the spec gives OP_REP_IMPORT offsets, where the
 * description starts at 0x08. */
USBIP_ASSERT_OFFSET(usbip_usb_device_t, busid, 0x108 - 0x08);
USBIP_ASSERT_OFFSET(usbip_usb_device_t, busnum, 0x128 - 0x08);
USBIP_ASSERT_OFFSET(usbip_usb_device_t, idVendor, 0x134 - 0x08);
USBIP_ASSERT_OFFSET(usbip_usb_device_t, bcdDevice, 0x138 - 0x08);
USBIP_ASSERT_OFFSET(usbip_usb_device_t, bNumInterfaces, 0x13F - 0x08);
_Static_assert(sizeof(usbip_usb_device_t) == 0x138, "usb_device size");
_Static_assert(sizeof(usbip_usb_interface_t) == 4, "usb_interface size");

USBIP_ASSERT_OFFSET(usbip_header_t, base.direction, 0x0C);
USBIP_ASSERT_OFFSET(usbip_header_t, base.ep, 0x10);
USBIP_ASSERT_OFFSET(usbip_header_t, u.cmd_submit.transfer_flags, 0x14);
USBIP_ASSERT_OFFSET(usbip_header_t, u.cmd_submit.number_of_packets, 0x20);
USBIP_ASSERT_OFFSET(usbip_header_t, u.cmd_submit.setup, 0x28);
USBIP_ASSERT_OFFSET(usbip_header_t, u.ret_submit.error_count, 0x24);
USBIP_ASSERT_OFFSET(usbip_header_t, u.ret_submit.padding, 0x28);
USBIP_ASSERT_OFFSET(usbip_header_t, u.cmd_unlink.padding, 0x18);
USBIP_ASSERT_OFFSET(usbip_header_t, u.ret_unlink.padding, 0x18);
_Static_assert(sizeof(usbip_header_t) == 0x30, "URB header size");

#undef USBIP_ASSERT_OFFSET
#endif

#endif /* MPY_POD_USBIP_PROTOCOL_H */
