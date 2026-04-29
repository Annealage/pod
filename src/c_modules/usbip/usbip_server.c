/* Annealage Pod: USB/IP multiplexer server.
 *
 * Adapted from referencea/esp-usbip-bridge/main/usbip_server.c. The
 * accept-loop / per-connection-worker / per-URB watchdog shape is
 * preserved; deviations:
 *
 *   - Two backends instead of one: real-USB (WS-B usbhost stub) and
 *     virtual (WS-C dapprobe via virtual_device_t ops).
 *   - Multiple-host serialisation: a small attachment table tracks
 *     which busid each connection holds and rejects re-import.
 *   - Protocol byte-shuffling lives in usbip_proto.{c,h} so the
 *     unit tests can link it directly without FreeRTOS.
 *   - Configurable listener port: defaults to USBIP_TCP_PORT but
 *     usbip.start(port=...) can override (used by the host-side
 *     test harness which connects over loopback).
 *   - APP_CPU pinning of both the accept-loop task and the
 *     per-connection workers, per architecture.md §3.
 *
 * Vendoring note: the original esp-usbip-bridge has no SPDX header
 * and no LICENSE file. See docs/design/usbip-server.md for the
 * licensing analysis. The code paths reused from the reference are
 * the protocol-level mechanics (read_exact, write_all, send_op_*,
 * the dispatch shape inside handle_submit, the accept-loop). The
 * synthesis is rewritten to fit the multiplexer requirements.
 */

#include "usbip_server.h"
#include "usbip_proto.h"
#include "../usbhost/usbhost.h"

#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

/* On-target build pulls in IDF + FreeRTOS + lwIP. The unit tests
 * compile usbip_proto.c and virtual_device.c only, so this TU is
 * never compiled on the host. The MPY_POD_HOST_TEST_BUILD guard is
 * defined by the unit test CMakeLists when a host build wants
 * usbip_server.c excluded; this lets a future test add server-level
 * tests under a posix-sockets stub if needed without disturbing the
 * default Phase 2 build. */
#ifndef MPY_POD_HOST_TEST_BUILD

#include <inttypes.h>
#include <unistd.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "freertos/idf_additions.h"
#include "esp_log.h"

#include "lwip/sockets.h"
#include "lwip/inet.h"
#include "netinet/tcp.h"
#include "lwip/sys.h"

static const char *TAG = "usbip";

/* Tunables. Match referencea defaults; can later be lifted into
 * sdkconfig if needed. */
#define USBIP_SERVER_TASK_STACK    8192
#define USBIP_SERVER_TASK_PRIORITY 5
#define USBIP_CLIENT_TASK_STACK    8192
#define USBIP_CLIENT_TASK_PRIORITY 5
#define USBIP_WATCHDOG_STACK       3072

/* APP_CPU per architecture.md §3. */
#define USBIP_TASK_CORE 1

/* Default per-URB transfer cap: 16 KiB. CMSIS-DAP packets are <= 64
 * bytes; the cap is sized for DUT bulk transfers. The kernel
 * fragments larger transfers automatically. */
#define USBIP_MAX_TRANSFER_DEFAULT (16 * 1024)

/* Maximum number of simultaneous client connections. Phase 2: two
 * (one DUT attach + one probe attach is the design intent). The
 * limit affects the fixed-size attachment table. */
#define USBIP_MAX_CLIENTS 4

typedef struct {
    bool                in_use;
    char                busid[USBIP_BUSID_SIZE];
    int                 fd;
    SemaphoreHandle_t   release_signal; /* given when the worker exits */
} client_slot_t;

typedef struct {
    TaskHandle_t          task;
    int                   listen_fd;
    bool                  running;
    SemaphoreHandle_t     attach_lock;
    client_slot_t         slots[USBIP_MAX_CLIENTS];
    int32_t               max_transfer;
} usbip_server_state_t;

static usbip_server_state_t s_state = {
    .listen_fd     = -1,
    .max_transfer  = USBIP_MAX_TRANSFER_DEFAULT,
};

/* Per-URB observability flag. Toggled at runtime by
 * `annealage_pod.usbip.set_verbose(True)` -> usbip_server_set_verbose.
 * Default off; INFO-level URB-trace logs only fire on the hot path
 * while this is true. */
static volatile bool s_urb_verbose = false;

void usbip_server_set_verbose(bool enable)
{
    s_urb_verbose = enable;
    ESP_LOGI(TAG, "URB verbose logging %s", enable ? "enabled" : "disabled");
}

bool usbip_server_is_verbose(void)
{
    return s_urb_verbose;
}

/* ---------- attachment table ---------- */

/* Reserve a slot for a busid currently being imported. Returns true
 * if the slot is now ours; false if another connection already holds
 * the same busid. */
static bool attachment_acquire(int fd, const char busid[USBIP_BUSID_SIZE],
                               size_t *slot_idx)
{
    if (s_state.attach_lock == NULL) {
        return false;
    }
    xSemaphoreTake(s_state.attach_lock, portMAX_DELAY);

    for (size_t i = 0; i < USBIP_MAX_CLIENTS; i++) {
        if (s_state.slots[i].in_use &&
            memcmp(s_state.slots[i].busid, busid, USBIP_BUSID_SIZE) == 0) {
            xSemaphoreGive(s_state.attach_lock);
            return false;
        }
    }
    for (size_t i = 0; i < USBIP_MAX_CLIENTS; i++) {
        if (!s_state.slots[i].in_use) {
            s_state.slots[i].in_use = true;
            s_state.slots[i].fd = fd;
            memcpy(s_state.slots[i].busid, busid, USBIP_BUSID_SIZE);
            *slot_idx = i;
            xSemaphoreGive(s_state.attach_lock);
            return true;
        }
    }
    xSemaphoreGive(s_state.attach_lock);
    return false;
}

static void attachment_release(size_t slot_idx)
{
    if (s_state.attach_lock == NULL || slot_idx >= USBIP_MAX_CLIENTS) {
        return;
    }
    xSemaphoreTake(s_state.attach_lock, portMAX_DELAY);
    s_state.slots[slot_idx].in_use = false;
    s_state.slots[slot_idx].fd = -1;
    memset(s_state.slots[slot_idx].busid, 0, USBIP_BUSID_SIZE);
    xSemaphoreGive(s_state.attach_lock);
}

size_t usbip_server_attached_busids(char (*out)[USBIP_BUSID_SIZE], size_t max)
{
    if (out == NULL || max == 0 || s_state.attach_lock == NULL) {
        return 0;
    }
    size_t copied = 0;
    xSemaphoreTake(s_state.attach_lock, portMAX_DELAY);
    for (size_t i = 0; i < USBIP_MAX_CLIENTS && copied < max; i++) {
        if (s_state.slots[i].in_use) {
            memcpy(out[copied], s_state.slots[i].busid, USBIP_BUSID_SIZE);
            copied++;
        }
    }
    xSemaphoreGive(s_state.attach_lock);
    return copied;
}

/* ---------- byte-stream helpers ---------- */

static bool read_exact(int fd, void *buf, size_t len)
{
    uint8_t *ptr = (uint8_t *)buf;
    size_t remaining = len;
    while (remaining > 0) {
        const ssize_t n = recv(fd, ptr, remaining, 0);
        if (n <= 0) {
            return false;
        }
        ptr += (size_t)n;
        remaining -= (size_t)n;
    }
    return true;
}

static bool write_all(int fd, const void *buf, size_t len)
{
    const uint8_t *ptr = (const uint8_t *)buf;
    size_t remaining = len;
    while (remaining > 0) {
        const ssize_t n = send(fd, ptr, remaining, 0);
        if (n <= 0) {
            return false;
        }
        ptr += (size_t)n;
        remaining -= (size_t)n;
    }
    return true;
}

static bool discard_exact(int fd, size_t len)
{
    uint8_t scratch[128];
    size_t remaining = len;
    while (remaining > 0) {
        size_t chunk = remaining < sizeof(scratch) ? remaining : sizeof(scratch);
        if (!read_exact(fd, scratch, chunk)) {
            return false;
        }
        remaining -= chunk;
    }
    return true;
}

/* ---------- protocol-level send wrappers ---------- */

static bool send_op_common(int fd, uint16_t code, uint32_t status)
{
    usbip_op_common_t reply;
    usbip_proto_pack_op_common(&reply, code, status);
    return write_all(fd, &reply, sizeof(reply));
}

static bool send_device_with_interfaces(int fd, const usbip_dev_record_t *device)
{
    usbip_device_desc_t wire;
    usbip_proto_pack_device_desc(device, &wire);
    if (!write_all(fd, &wire, sizeof(wire))) {
        return false;
    }
    for (uint8_t i = 0; i < device->num_interfaces; i++) {
        usbip_interface_desc_t iface;
        if (!usbip_proto_pack_interface_desc(device, i, &iface)) {
            break;
        }
        if (!write_all(fd, &iface, sizeof(iface))) {
            return false;
        }
    }
    return true;
}

static bool send_ret_submit(int fd,
                            uint32_t seqnum, uint32_t devid,
                            uint32_t direction, uint32_t ep,
                            int32_t status,
                            const uint8_t *payload, uint32_t payload_len)
{
    usbip_header_t reply;
    usbip_proto_pack_ret_submit(&reply, seqnum, devid, direction, ep,
                                status, payload_len);

    if (payload_len > 0 && payload != NULL) {
        /* Coalesce header + payload into a single send so the kernel
         * can read both without a second TCP segment delay. */
        uint8_t *buf = malloc(sizeof(reply) + payload_len);
        if (buf == NULL) {
            return false;
        }
        memcpy(buf, &reply, sizeof(reply));
        memcpy(buf + sizeof(reply), payload, payload_len);
        bool ok = write_all(fd, buf, sizeof(reply) + payload_len);
        free(buf);
        return ok;
    }
    return write_all(fd, &reply, sizeof(reply));
}

static bool send_ret_unlink(int fd,
                            uint32_t seqnum, uint32_t devid,
                            uint32_t direction, uint32_t ep,
                            int32_t status)
{
    usbip_header_t reply;
    usbip_proto_pack_ret_unlink(&reply, seqnum, devid, direction, ep, status);
    return write_all(fd, &reply, sizeof(reply));
}

/* ---------- DEVLIST / IMPORT ---------- */

static size_t collect_all_devices(usbip_dev_record_t *out, size_t max)
{
    /* Real-USB devices first (busid "1-N"), virtuals second
     * (busid "2-N"). Ordering is informational; the host iterates
     * regardless. */
    size_t copied = usbhost_get_devices(out, max);
    if (copied < max) {
        copied += usbip_get_virtual_devices(out + copied, max - copied);
    }
    return copied;
}

static bool find_device_by_busid(const char busid[USBIP_BUSID_SIZE],
                                 usbip_dev_record_t *out)
{
    /* Try virtuals first: the synthetic CMSIS-DAP busid space (2-N)
     * never collides with the real-USB busid space (1-N), but doing
     * the cheap in-process lookup first saves a TinyUSB API call on
     * the common case of a probe attach. */
    virtual_device_t *vdev = usbip_find_virtual_device(busid);
    if (vdev != NULL) {
        *out = vdev->desc;
        return true;
    }
    return usbhost_get_device_by_busid(busid, out);
}

static bool handle_devlist_request(int fd)
{
    usbip_dev_record_t devices[USBIP_MAX_CLIENTS + USBIP_VIRTUAL_DEVICE_MAX];
    const size_t count = collect_all_devices(devices,
        sizeof(devices) / sizeof(devices[0]));

    ESP_LOGI(TAG, "DEVLIST: reporting %zu device(s)", count);

    if (!send_op_common(fd, USBIP_OP_REP_DEVLIST, 0)) {
        return false;
    }
    uint32_t count_be = htonl((uint32_t)count);
    if (!write_all(fd, &count_be, sizeof(count_be))) {
        return false;
    }
    for (size_t i = 0; i < count; i++) {
        if (!send_device_with_interfaces(fd, &devices[i])) {
            return false;
        }
    }
    return true;
}

/* ---------- URB stream ---------- */

typedef struct {
    int           fd;
    volatile bool cancel;
} urb_stream_ctx_t;

static void socket_watchdog_task(void *arg)
{
    urb_stream_ctx_t *ctx = (urb_stream_ctx_t *)arg;
    uint8_t probe;
    while (!ctx->cancel) {
        fd_set readfds;
        FD_ZERO(&readfds);
        FD_SET(ctx->fd, &readfds);
        struct timeval tv = { .tv_sec = 0, .tv_usec = 50000 };
        int ret = select(ctx->fd + 1, &readfds, NULL, NULL, &tv);
        if (ret > 0) {
            ret = recv(ctx->fd, &probe, 1, MSG_PEEK | MSG_DONTWAIT);
            if (ret <= 0) {
                ctx->cancel = true;
                break;
            }
            break;
        }
    }
    vTaskDelete(NULL);
}

static bool dispatch_submit(int fd,
                            const usbip_decoded_header_t *hdr,
                            const char busid[USBIP_BUSID_SIZE],
                            uint32_t expected_devid,
                            bool is_virtual,
                            volatile bool *cancel)
{
    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbip_in: busid=%.32s ep=%" PRIu32 " dir=%s len=%" PRIu32
                      " seq=%" PRIu32,
                 busid, hdr->ep,
                 (hdr->direction == USBIP_DIR_IN) ? "IN" : "OUT",
                 hdr->transfer_buffer_length, hdr->seqnum);
    }

    /* Validation that does not require a backend. */
    int v = usbip_proto_validate_submit(hdr, s_state.max_transfer);
    if (v == -EINVAL && (hdr->direction != USBIP_DIR_OUT &&
                          hdr->direction != USBIP_DIR_IN)) {
        /* Bad direction: the URB stream is unrecoverable because we
         * cannot reliably consume the OUT data stage. Drop the
         * connection. */
        ESP_LOGW(TAG, "  -> bad direction %" PRIu32 ", dropping conn", hdr->direction);
        return false;
    }
    if (v == -EMSGSIZE) {
        if (hdr->direction == USBIP_DIR_OUT && hdr->transfer_buffer_length > 0) {
            if (!discard_exact(fd, (size_t)hdr->transfer_buffer_length)) {
                return false;
            }
        }
        return send_ret_submit(fd, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                               -EMSGSIZE, NULL, 0);
    }
    if (v == -EINVAL) {
        return send_ret_submit(fd, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                               -EINVAL, NULL, 0);
    }
    if (v == -EOPNOTSUPP) {
        return send_ret_submit(fd, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                               -EOPNOTSUPP, NULL, 0);
    }

    if (hdr->devid != expected_devid) {
        ESP_LOGW(TAG, "  -> ENODEV (devid 0x%08" PRIx32 " != expected 0x%08" PRIx32 ")",
                 hdr->devid, expected_devid);
        if (hdr->direction == USBIP_DIR_OUT && hdr->transfer_buffer_length > 0) {
            if (!discard_exact(fd, (size_t)hdr->transfer_buffer_length)) {
                return false;
            }
        }
        return send_ret_submit(fd, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                               -ENODEV, NULL, 0);
    }

    /* Read inbound OUT data into a dedicated buffer. */
    uint8_t *out_buf = NULL;
    size_t   out_len = 0;
    if (hdr->direction == USBIP_DIR_OUT && hdr->transfer_buffer_length > 0) {
        out_len = (size_t)hdr->transfer_buffer_length;
        out_buf = malloc(out_len);
        if (out_buf == NULL) {
            (void)discard_exact(fd, out_len);
            return send_ret_submit(fd, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                                   -ENOMEM, NULL, 0);
        }
        if (!read_exact(fd, out_buf, out_len)) {
            free(out_buf);
            return false;
        }
    }

    /* Allocate IN buffer. */
    uint8_t *in_buf = NULL;
    size_t   in_capacity = 0;
    if (hdr->direction == USBIP_DIR_IN && hdr->transfer_buffer_length > 0) {
        in_capacity = (size_t)hdr->transfer_buffer_length;
        in_buf = malloc(in_capacity);
        if (in_buf == NULL) {
            free(out_buf);
            return send_ret_submit(fd, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                                   -ENOMEM, NULL, 0);
        }
    }

    size_t in_len = 0;
    int    status;

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbip_dispatch: busid=%.32s ep=%" PRIu32 " target=%s",
                 busid, hdr->ep, is_virtual ? "virtual" : "host");
    }

    if (is_virtual) {
        virtual_device_t *vdev = usbip_find_virtual_device(busid);
        if (vdev == NULL) {
            free(out_buf);
            free(in_buf);
            return send_ret_submit(fd, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                                   -ENODEV, NULL, 0);
        }
        if (hdr->ep == 0) {
            usbip_setup_packet_t setup;
            memcpy(&setup, hdr->setup, sizeof(setup));
            /* SETUP-direction sanity. */
            const bool setup_in = (setup.bmRequestType & USBIP_REQUEST_DIR_IN) != 0;
            if ((hdr->direction == USBIP_DIR_IN) != setup_in) {
                free(out_buf);
                free(in_buf);
                return send_ret_submit(fd, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                                       -EINVAL, NULL, 0);
            }
            status = vdev->ops->control_transfer(vdev, &setup,
                                                 out_buf, out_len,
                                                 in_buf, in_capacity, &in_len);
        } else {
            uint8_t ep_addr = (uint8_t)(hdr->ep |
                              (hdr->direction == USBIP_DIR_IN ? 0x80u : 0x00u));
            status = vdev->ops->data_transfer(vdev, ep_addr,
                                              out_buf, out_len,
                                              in_buf, in_capacity, &in_len);
        }
    } else if (hdr->ep == 0) {
        usbip_setup_packet_t setup;
        memcpy(&setup, hdr->setup, sizeof(setup));
        const bool setup_in = (setup.bmRequestType & USBIP_REQUEST_DIR_IN) != 0;
        if ((hdr->direction == USBIP_DIR_IN) != setup_in) {
            free(out_buf);
            free(in_buf);
            return send_ret_submit(fd, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                                   -EINVAL, NULL, 0);
        }
        status = usbhost_control_transfer(busid, &setup,
                                          out_buf, out_len,
                                          in_buf, in_capacity, &in_len, cancel);
    } else {
        uint8_t ep_addr = (uint8_t)(hdr->ep |
                          (hdr->direction == USBIP_DIR_IN ? 0x80u : 0x00u));
        if (usbhost_is_interrupt_endpoint(busid, (uint8_t)hdr->ep,
                                          (uint8_t)hdr->direction)) {
            status = usbhost_interrupt_transfer(busid, ep_addr,
                                                out_buf, out_len,
                                                in_buf, in_capacity, &in_len, cancel);
        } else {
            status = usbhost_bulk_transfer(busid, ep_addr,
                                           out_buf, out_len,
                                           in_buf, in_capacity, &in_len, cancel);
        }
    }

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbip_complete: busid=%.32s ep=%" PRIu32
                      " status=%d actual=%zu",
                 busid, hdr->ep, status, in_len);
    }

    bool ok = send_ret_submit(fd, hdr->seqnum, hdr->devid, hdr->direction, hdr->ep,
                              status,
                              (status == 0 && hdr->direction == USBIP_DIR_IN) ? in_buf : NULL,
                              (status == 0 && hdr->direction == USBIP_DIR_IN)
                                ? (uint32_t)in_len : 0);

    if (s_urb_verbose) {
        ESP_LOGI(TAG, "usbip_out: busid=%.32s ep=%" PRIu32 " ok=%d",
                 busid, hdr->ep, ok ? 1 : 0);
    }

    free(out_buf);
    free(in_buf);
    return ok;
}

static bool handle_urb_stream(int fd,
                              const char busid[USBIP_BUSID_SIZE],
                              uint32_t expected_devid)
{
    urb_stream_ctx_t ctx = { .fd = fd, .cancel = false };
    const bool is_virtual = (usbip_find_virtual_device(busid) != NULL);

    while (true) {
        usbip_header_t raw;
        if (!read_exact(fd, &raw, sizeof(raw))) {
            return false;
        }
        ctx.cancel = false;

        usbip_decoded_header_t hdr;
        usbip_proto_unpack_header(&raw, &hdr);

        if (hdr.command == USBIP_CMD_SUBMIT) {
            TaskHandle_t watchdog = NULL;
            if (!is_virtual) {
                /* Real-device URBs may block on USB I/O; spawn a
                 * watchdog that aborts when the client disconnects.
                 * Synthetic URBs complete in microseconds and do
                 * not need this. */
                xTaskCreatePinnedToCore(socket_watchdog_task, "usbip_wd",
                                        USBIP_WATCHDOG_STACK, &ctx,
                                        USBIP_CLIENT_TASK_PRIORITY, &watchdog,
                                        USBIP_TASK_CORE);
            }

            bool ok = dispatch_submit(fd, &hdr, busid, expected_devid,
                                      is_virtual, &ctx.cancel);

            if (watchdog != NULL) {
                ctx.cancel = true;
                vTaskDelay(pdMS_TO_TICKS(10));
            }
            if (!ok) {
                return false;
            }
        } else if (hdr.command == USBIP_CMD_UNLINK) {
            /* Phase 2 policy: reply 0 (URB already complete). The
             * synthetic device path satisfies every URB locally with
             * bounded latency; the real-USB path will gain seqnum-
             * tracking cancellation in WS-B. */
            if (!send_ret_unlink(fd, hdr.seqnum, hdr.devid,
                                 hdr.direction, hdr.ep, 0)) {
                return false;
            }
        } else {
            ESP_LOGW(TAG, "Unsupported USB/IP command 0x%08" PRIx32, hdr.command);
            return false;
        }
    }
}

static bool handle_import_request(int fd, size_t *slot_idx, bool *slot_held)
{
    char busid[USBIP_BUSID_SIZE];
    if (!read_exact(fd, busid, sizeof(busid))) {
        return false;
    }
    ESP_LOGI(TAG, "IMPORT: requested busid='%.32s'", busid);

    usbip_dev_record_t device;
    bool found = find_device_by_busid(busid, &device);

    if (!found) {
        (void)send_op_common(fd, USBIP_OP_REP_IMPORT, 1);
        return true;
    }

    /* Multiple-host serialisation: only one connection may import a
     * given busid at a time. */
    size_t my_slot = 0;
    if (!attachment_acquire(fd, busid, &my_slot)) {
        ESP_LOGW(TAG, "IMPORT: busid '%.32s' already attached, refusing", busid);
        (void)send_op_common(fd, USBIP_OP_REP_IMPORT, 1);
        return true;
    }
    *slot_idx = my_slot;
    *slot_held = true;

    if (!send_op_common(fd, USBIP_OP_REP_IMPORT, 0)) {
        return false;
    }

    /* The IMPORT reply carries the device descriptor only, no
     * interface descriptors. The kernel reads exactly
     * sizeof(struct usbip_usb_device); extra bytes get parsed as
     * URB PDU and the connection breaks. */
    usbip_device_desc_t wire;
    usbip_proto_pack_device_desc(&device, &wire);
    if (!write_all(fd, &wire, sizeof(wire))) {
        return false;
    }

    /* Optional virtual-device on_attach hook. */
    virtual_device_t *vdev = usbip_find_virtual_device(busid);
    if (vdev && vdev->ops->on_attach) {
        if (vdev->ops->on_attach(vdev) != 0) {
            return false;
        }
    }

    bool ok = handle_urb_stream(fd, busid, usbip_proto_make_devid(&device));

    if (vdev && vdev->ops->on_detach) {
        vdev->ops->on_detach(vdev);
    }
    return ok;
}

/* ---------- accept loop ---------- */

static void handle_client(int fd, size_t *slot_idx, bool *slot_held)
{
    usbip_op_common_t op_raw;
    if (!read_exact(fd, &op_raw, sizeof(op_raw))) {
        return;
    }
    uint16_t version, code;
    uint32_t status;
    usbip_proto_unpack_op_common(&op_raw, &version, &code, &status);

    if (version != USBIP_VERSION) {
        ESP_LOGW(TAG, "Unsupported USB/IP version 0x%04x", version);
        return;
    }
    if (code == USBIP_OP_REQ_DEVLIST) {
        (void)handle_devlist_request(fd);
        return;
    }
    if (code == USBIP_OP_REQ_IMPORT) {
        (void)handle_import_request(fd, slot_idx, slot_held);
        return;
    }
    ESP_LOGW(TAG, "Unsupported op 0x%04x", code);
}

typedef struct {
    int fd;
} client_task_arg_t;

static void client_task(void *arg)
{
    client_task_arg_t *ctx = (client_task_arg_t *)arg;
    int fd = ctx->fd;
    free(ctx);

    size_t slot_idx = 0;
    bool   slot_held = false;
    handle_client(fd, &slot_idx, &slot_held);
    if (slot_held) {
        attachment_release(slot_idx);
    }
    close(fd);
    ESP_LOGI(TAG, "Client disconnected");
    vTaskDelete(NULL);
}

static void usbip_server_task(void *arg)
{
    uint16_t port = (uint16_t)(uintptr_t)arg;

    int listen_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (listen_fd < 0) {
        ESP_LOGE(TAG, "socket() failed: errno=%d", errno);
        s_state.running = false;
        vTaskDelete(NULL);
        return;
    }
    int opt = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGE(TAG, "bind() failed on port %u: errno=%d", port, errno);
        close(listen_fd);
        s_state.running = false;
        vTaskDelete(NULL);
        return;
    }
    if (listen(listen_fd, 4) < 0) {
        ESP_LOGE(TAG, "listen() failed: errno=%d", errno);
        close(listen_fd);
        s_state.running = false;
        vTaskDelete(NULL);
        return;
    }

    s_state.listen_fd = listen_fd;
    s_state.running = true;
    ESP_LOGI(TAG, "USB/IP server listening on TCP %u", port);

    while (s_state.running) {
        struct sockaddr_in client_addr;
        socklen_t client_len = sizeof(client_addr);
        int client_fd = accept(listen_fd, (struct sockaddr *)&client_addr, &client_len);
        if (client_fd < 0) {
            if (!s_state.running) {
                break;
            }
            ESP_LOGW(TAG, "accept() failed: errno=%d", errno);
            continue;
        }

        int nodelay = 1;
        setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

        client_task_arg_t *cargs = malloc(sizeof(*cargs));
        if (cargs == NULL) {
            close(client_fd);
            continue;
        }
        cargs->fd = client_fd;
        if (xTaskCreatePinnedToCore(client_task, "usbip_client",
                                    USBIP_CLIENT_TASK_STACK, cargs,
                                    USBIP_CLIENT_TASK_PRIORITY, NULL,
                                    USBIP_TASK_CORE) != pdPASS) {
            free(cargs);
            close(client_fd);
        }
    }

    close(listen_fd);
    s_state.listen_fd = -1;
    s_state.running = false;
    s_state.task = NULL;
    ESP_LOGI(TAG, "USB/IP server stopped");
    vTaskDelete(NULL);
}

/* ---------- public API ---------- */

int usbip_server_start(uint16_t port)
{
    if (s_state.running) {
        return 0;
    }
    if (s_state.attach_lock == NULL) {
        s_state.attach_lock = xSemaphoreCreateMutex();
        if (s_state.attach_lock == NULL) {
            return -ENOMEM;
        }
        for (size_t i = 0; i < USBIP_MAX_CLIENTS; i++) {
            s_state.slots[i].in_use = false;
            s_state.slots[i].fd = -1;
            memset(s_state.slots[i].busid, 0, USBIP_BUSID_SIZE);
        }
    }

    /* Lazy-init the host backend. The Phase 2 stub is a no-op; WS-B
     * replaces it with the TinyUSB host stack. */
    (void)usbhost_start();

    if (port == 0) {
        port = USBIP_TCP_PORT;
    }

    BaseType_t rc = xTaskCreatePinnedToCore(
        usbip_server_task, "usbip_server",
        USBIP_SERVER_TASK_STACK,
        (void *)(uintptr_t)port,
        USBIP_SERVER_TASK_PRIORITY,
        &s_state.task,
        USBIP_TASK_CORE);
    if (rc != pdPASS) {
        return -ENOMEM;
    }
    return 0;
}

int usbip_server_stop(void)
{
    if (!s_state.running) {
        return 0;
    }
    /* Setting running=false then closing the listener forces accept()
     * out of its blocked state. The task self-terminates from there. */
    s_state.running = false;
    if (s_state.listen_fd >= 0) {
        shutdown(s_state.listen_fd, SHUT_RDWR);
        close(s_state.listen_fd);
        s_state.listen_fd = -1;
    }
    return 0;
}

bool usbip_server_is_running(void)
{
    return s_state.running;
}

int usbip_server_register_virtual_device(virtual_device_t *dev)
{
    return usbip_register_virtual_device(dev);
}

int32_t usbip_server_max_transfer(void)
{
    return s_state.max_transfer;
}

#else /* MPY_POD_HOST_TEST_BUILD */

/* Host-test shim. The real entry points are stubbed so a host
 * harness can link the protocol module without needing a TCP
 * listener. */
#include <stdio.h>

int usbip_server_start(uint16_t port)
{
    (void)port;
    return 0;
}

int usbip_server_stop(void)
{
    return 0;
}

bool usbip_server_is_running(void)
{
    return false;
}

int usbip_server_register_virtual_device(virtual_device_t *dev)
{
    return usbip_register_virtual_device(dev);
}

int32_t usbip_server_max_transfer(void)
{
    return 16 * 1024;
}

size_t usbip_server_attached_busids(char (*out)[USBIP_BUSID_SIZE], size_t max)
{
    (void)out;
    (void)max;
    return 0;
}

void usbip_server_set_verbose(bool enable) { (void)enable; }
bool usbip_server_is_verbose(void) { return false; }

#endif /* MPY_POD_HOST_TEST_BUILD */
