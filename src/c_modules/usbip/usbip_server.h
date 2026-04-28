/* Annealage Pod: USB/IP server public interface.
 *
 * The server multiplexes two virtual devices on a single TCP/3240
 * listener:
 *   - busid 1-N: the real DUT, forwarded through the WS-B usbhost
 *     stub (Phase 2: stub returning -ENOSYS; Phase 3: TinyUSB host).
 *   - busid 2-1: the synthetic CMSIS-DAP-v2 probe registered by the
 *     WS-C dapprobe module.
 *
 * Concurrency: one accept-loop task pinned to APP_CPU; one
 * per-connection worker spawned from each accept(). Multiple-host
 * serialisation: a busid that is currently imported by one
 * connection rejects further IMPORTs from other connections with
 * USBIP_REPLY status=1 until the holding connection closes.
 */

#ifndef MPY_POD_USBIP_SERVER_H
#define MPY_POD_USBIP_SERVER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "virtual_device.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Start the server on the given TCP port. Binds, listens, spawns
 * the accept-loop task. Idempotent: a second call with the server
 * already running is a no-op and returns 0.
 *
 * Returns 0 on success, negative errno otherwise. */
int usbip_server_start(uint16_t port);

/* Stop the server. Closes the listener, signals the accept task to
 * exit, and tears down active client tasks (which causes their TCP
 * connections to drop). Idempotent. */
int usbip_server_stop(void);

/* Returns true if the server task is running and the listener is
 * bound. */
bool usbip_server_is_running(void);

/* Public registration entry for synthetic devices. Forwards to
 * usbip_register_virtual_device(); kept here so callers do not have
 * to include virtual_device.h directly. */
int usbip_server_register_virtual_device(virtual_device_t *dev);

/* Return the maximum URB transfer length the server accepts in a
 * single CMD_SUBMIT. Larger transfers receive -EMSGSIZE. */
int32_t usbip_server_max_transfer(void);

/* Snapshot the list of currently-attached busids (each NUL-padded
 * to 32 bytes). Returns the count copied. Used by the MP API
 * `usbip.attached_devices()`. */
size_t usbip_server_attached_busids(char (*out)[USBIP_BUSID_SIZE], size_t max);

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_USBIP_SERVER_H */
