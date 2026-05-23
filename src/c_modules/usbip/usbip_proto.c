/* Annealage Pod: host-portable USB/IP protocol helpers.
 *
 * Pure byte-shuffling. No FreeRTOS, no lwIP, no IDF. Anything that
 * touches a socket or a task lives in usbip_server.c.
 *
 * Adapted from referencea/esp-usbip-bridge/main/usbip_server.c
 * (`fill_wire_device_desc`, `make_devid`, `send_ret_submit`,
 * `send_ret_unlink`). The byte order conversions and field
 * placements mirror the reference exactly; deviations are documented
 * in docs/design/usbip-server.md.
 */

#include "usbip_proto.h"

#include <errno.h>
#include <string.h>

/* Portable byte-swap: avoids dragging arpa/inet.h or lwIP into the
 * test build. The compiled-in calls collapse to a builtin on gcc/clang. */
static uint16_t bswap16(uint16_t x)
{
    return (uint16_t)((x << 8) | (x >> 8));
}

static uint32_t bswap32(uint32_t x)
{
    return ((x & 0x000000FFu) << 24)
         | ((x & 0x0000FF00u) << 8)
         | ((x & 0x00FF0000u) >> 8)
         | ((x & 0xFF000000u) >> 24);
}

#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
#define HTON16(x) (x)
#define HTON32(x) (x)
#define NTOH16(x) (x)
#define NTOH32(x) (x)
#else
#define HTON16(x) bswap16(x)
#define HTON32(x) bswap32(x)
#define NTOH16(x) bswap16(x)
#define NTOH32(x) bswap32(x)
#endif

void usbip_proto_pack_op_common(usbip_op_common_t *out,
                                uint16_t code, uint32_t status)
{
    out->version = HTON16(USBIP_VERSION);
    out->code    = HTON16(code);
    out->status  = HTON32(status);
}

bool usbip_proto_unpack_op_common(const usbip_op_common_t *in,
                                  uint16_t *version,
                                  uint16_t *code,
                                  uint32_t *status)
{
    if (in == NULL) {
        return false;
    }
    if (version) {
        *version = NTOH16(in->version);
    }
    if (code) {
        *code = NTOH16(in->code);
    }
    if (status) {
        *status = NTOH32(in->status);
    }
    return true;
}

void usbip_proto_pack_device_desc(const usbip_dev_record_t *src,
                                  usbip_device_desc_t *dst)
{
    memset(dst, 0, sizeof(*dst));
    memcpy(dst->path, src->path, sizeof(dst->path));
    memcpy(dst->busid, src->busid, sizeof(dst->busid));
    dst->busnum               = HTON32(src->busnum);
    dst->devnum               = HTON32(src->devnum);
    dst->speed                = HTON32(src->speed);
    dst->id_vendor            = HTON16(src->id_vendor);
    dst->id_product           = HTON16(src->id_product);
    dst->bcd_device           = HTON16(src->bcd_device);
    dst->device_class         = src->device_class;
    dst->device_subclass      = src->device_subclass;
    dst->device_protocol      = src->device_protocol;
    dst->configuration_value  = src->configuration_value;
    dst->num_configurations   = src->num_configurations;
    dst->num_interfaces       = src->num_interfaces;
}

bool usbip_proto_pack_interface_desc(const usbip_dev_record_t *src,
                                     uint8_t i,
                                     usbip_interface_desc_t *dst)
{
    if (i >= src->num_interfaces || i >= USBIP_MAX_INTERFACES) {
        return false;
    }
    dst->interface_class    = src->interfaces[i].interface_class;
    dst->interface_subclass = src->interfaces[i].interface_subclass;
    dst->interface_protocol = src->interfaces[i].interface_protocol;
    dst->padding            = 0;
    return true;
}

uint32_t usbip_proto_make_devid(const usbip_dev_record_t *src)
{
    return (src->busnum << 16) | (src->devnum & 0xFFFFu);
}

void usbip_proto_unpack_header(const usbip_header_t *raw,
                               usbip_decoded_header_t *out)
{
    out->command   = NTOH32(raw->base.command);
    out->seqnum    = NTOH32(raw->base.seqnum);
    out->devid     = NTOH32(raw->base.devid);
    out->direction = NTOH32(raw->base.direction);
    out->ep        = NTOH32(raw->base.ep);

    /* The CMD_SUBMIT and CMD_UNLINK payloads are mutually exclusive
     * but live in a union; copy both unconditionally and let the
     * caller pick the right field on dispatch. */
    out->transfer_flags          = NTOH32(raw->u.cmd_submit.transfer_flags);
    out->transfer_buffer_length  = (int32_t)NTOH32((uint32_t)raw->u.cmd_submit.transfer_buffer_length);
    out->start_frame             = (int32_t)NTOH32((uint32_t)raw->u.cmd_submit.start_frame);
    out->number_of_packets       = (int32_t)NTOH32((uint32_t)raw->u.cmd_submit.number_of_packets);
    out->interval                = (int32_t)NTOH32((uint32_t)raw->u.cmd_submit.interval);
    memcpy(out->setup, raw->u.cmd_submit.setup, sizeof(out->setup));

    out->unlink_seqnum = NTOH32(raw->u.cmd_unlink.unlink_seqnum);
}

void usbip_proto_pack_ret_submit(usbip_header_t *raw,
                                 uint32_t seqnum,
                                 uint32_t devid,
                                 uint32_t direction,
                                 uint32_t ep,
                                 int32_t  status,
                                 uint32_t actual_length)
{
    memset(raw, 0, sizeof(*raw));
    raw->base.command   = HTON32(USBIP_RET_SUBMIT);
    raw->base.seqnum    = HTON32(seqnum);
    raw->base.devid     = HTON32(devid);
    raw->base.direction = HTON32(direction);
    raw->base.ep        = HTON32(ep);

    raw->u.ret_submit.status              = (int32_t)HTON32((uint32_t)status);
    raw->u.ret_submit.actual_length       = HTON32(actual_length);
    raw->u.ret_submit.start_frame         = 0;
    raw->u.ret_submit.number_of_packets   = 0;
    raw->u.ret_submit.error_count         = 0;
    raw->u.ret_submit.padding             = 0;
}

void usbip_proto_pack_ret_unlink(usbip_header_t *raw,
                                 uint32_t seqnum,
                                 uint32_t devid,
                                 uint32_t direction,
                                 uint32_t ep,
                                 int32_t  status)
{
    memset(raw, 0, sizeof(*raw));
    raw->base.command   = HTON32(USBIP_RET_UNLINK);
    raw->base.seqnum    = HTON32(seqnum);
    raw->base.devid     = HTON32(devid);
    raw->base.direction = HTON32(direction);
    raw->base.ep        = HTON32(ep);
    raw->u.ret_unlink.status = (int32_t)HTON32((uint32_t)status);
}

int usbip_proto_validate_submit(const usbip_decoded_header_t *hdr,
                                int32_t max_transfer)
{
    if (hdr->transfer_buffer_length < 0) {
        return -EINVAL;
    }
    if (max_transfer > 0 && hdr->transfer_buffer_length > max_transfer) {
        return -EMSGSIZE;
    }
    if (hdr->direction != USBIP_DIR_OUT && hdr->direction != USBIP_DIR_IN) {
        return -EINVAL;
    }
    /* USB-2.0 endpoints are 4-bit; the wire field is 32-bit so a
     * malformed client can put arbitrary garbage here. Reject up
     * front rather than letting it cast-truncate into bus calls
     * (the (uint8_t)(hdr->ep | 0x80) folds in lane_dispatch would
     * silently alias different endpoints). */
    if (hdr->ep > 15u) {
        return -EINVAL;
    }
    /* Per the kernel docs, non-iso URBs carry number_of_packets =
     * 0xFFFFFFFF. Some clients send 0. Reject only positive counts,
     * which would actually be isochronous. */
    if ((uint32_t)hdr->number_of_packets != USBIP_NON_ISO_PACKETS
        && hdr->number_of_packets != 0) {
        return -EOPNOTSUPP;
    }
    return 0;
}
