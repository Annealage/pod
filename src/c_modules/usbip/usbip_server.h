/* Annealage Pod: USB/IP server public interface.
 *
 * One TCP/3240 listener exporting the DUT attached to the pod's USB host
 * port. A busid imported by one connection refuses further IMPORTs from
 * other connections (status=1) until the holding connection closes.
 */

#ifndef MPY_POD_USBIP_SERVER_H
#define MPY_POD_USBIP_SERVER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "usbip_device.h"

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

/* Return the maximum URB transfer length the server accepts in a
 * single CMD_SUBMIT. Larger transfers receive -EMSGSIZE. */
int32_t usbip_server_max_transfer(void);

/* Snapshot the list of currently-attached busids (each NUL-padded
 * to 32 bytes). Returns the count copied. Used by the MP API
 * `usbip.attached_devices()`. */
size_t usbip_server_attached_busids(char (*out)[USBIP_BUSID_SIZE], size_t max);

/* Toggle per-URB observability logging on the usbip server. When
 * enabled every CMD_SUBMIT receive, dispatch decision, backend
 * completion, and RET_SUBMIT send emits one ESP_LOGI line under
 * the "usbip" tag. Default off so production builds stay quiet. */
void usbip_server_set_verbose(bool enable);

/* True if per-URB verbose logging is currently enabled. */
bool usbip_server_is_verbose(void);

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_USBIP_SERVER_H */
