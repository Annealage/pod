/* Annealage Pod: host-portable USB/IP protocol helpers.
 *
 * Splits the byte-format work out of usbip_server.c so the unit
 * tests under test/unit/usbip/ can exercise it without FreeRTOS,
 * lwIP, or the IDF in scope.
 *
 * Convention: the in-memory `usbip_dev_record_t` and
 * `usbip_setup_packet_t` are host byte order; this module produces
 * and consumes the corresponding kernel-on-the-wire packed structs.
 */

#ifndef MPY_POD_USBIP_PROTO_H
#define MPY_POD_USBIP_PROTO_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "usbip_protocol.h"
#include "virtual_device.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Serialise an `op_common` reply into the 8-byte network-order
 * struct. */
void usbip_proto_pack_op_common(usbip_op_common_t *out,
                                uint16_t code, uint32_t status);

/* Parse an `op_common` request from a raw 8-byte buffer. Returns
 * false on a length-zero or NULL input. The returned fields are in
 * host byte order. */
bool usbip_proto_unpack_op_common(const usbip_op_common_t *in,
                                  uint16_t *version,
                                  uint16_t *code,
                                  uint32_t *status);

/* Pack a backend descriptor record into the 0x138-byte device
 * descriptor struct, ready to write to the wire. */
void usbip_proto_pack_device_desc(const usbip_dev_record_t *src,
                                  usbip_usb_device_t *dst);

/* Pack a single interface descriptor (the 4-byte trailing record
 * appended in OP_REP_DEVLIST). Caller selects which interface
 * triple by index. Returns false if i >= src->num_interfaces. */
bool usbip_proto_pack_interface_desc(const usbip_dev_record_t *src,
                                     uint8_t i,
                                     usbip_usb_interface_t *dst);

/* Compute the kernel-style devid: (busnum << 16) | (devnum & 0xFFFF). */
uint32_t usbip_proto_make_devid(const usbip_dev_record_t *src);

/* Parse a 48-byte usbip_header_t from network byte order into a
 * caller-provided "decoded" struct. The decoded struct holds host-
 * byte-order fields only; the caller is expected to dispatch on
 * `command`. */
typedef struct {
    uint32_t command;
    uint32_t seqnum;
    uint32_t devid;
    uint32_t direction;
    uint32_t ep;
    /* CMD_SUBMIT fields (only valid when command == USBIP_CMD_SUBMIT). */
    uint32_t transfer_flags;
    int32_t  transfer_buffer_length;
    int32_t  start_frame;
    int32_t  number_of_packets;
    int32_t  interval;
    uint8_t  setup[8];
    /* CMD_UNLINK fields (only valid when command == USBIP_CMD_UNLINK). */
    uint32_t unlink_seqnum;
} usbip_decoded_header_t;

void usbip_proto_unpack_header(const usbip_header_t *raw,
                               usbip_decoded_header_t *out);

/* Pack a RET_SUBMIT reply header (no payload follows in this struct;
 * the caller writes payload bytes after). actual_length is host
 * byte order; the function byte-swaps. */
void usbip_proto_pack_ret_submit(usbip_header_t *raw,
                                 uint32_t seqnum,
                                 uint32_t devid,
                                 uint32_t direction,
                                 uint32_t ep,
                                 int32_t  status,
                                 uint32_t actual_length);

/* Pack a RET_UNLINK reply. */
void usbip_proto_pack_ret_unlink(usbip_header_t *raw,
                                 uint32_t seqnum,
                                 uint32_t devid,
                                 uint32_t direction,
                                 uint32_t ep,
                                 int32_t  status);

/* Validate that a CMD_SUBMIT header is acceptable independent of the
 * backend. Returns:
 *   0           - header is well-formed; caller proceeds.
 *  -EINVAL      - transfer_buffer_length < 0 or direction not 0/1.
 *  -EMSGSIZE    - transfer_buffer_length exceeds max_transfer.
 *  -EOPNOTSUPP  - isochronous (positive number_of_packets that is
 *                 not the kernel's 0xFFFFFFFF tag).
 * Negative returns are intended to be sent verbatim as the
 * RET_SUBMIT.status (after caller drains any inbound OUT data). */
int usbip_proto_validate_submit(const usbip_decoded_header_t *hdr,
                                int32_t max_transfer);

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_USBIP_PROTO_H */
