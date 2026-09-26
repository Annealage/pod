/* Annealage Pod: hub_device firmware notification beacon.
 *
 * Vendor-class virtual USB device (not a real USB hub) exposed via the
 * USB/IP server. A host-side daemon imports the device and polls EP1 IN
 * to learn when the DUT connects or disconnects so it can re-attach the
 * DUT busid.
 *
 * Registered via usbip_register_virtual_device; the resulting busid
 * depends on registration order (typically 2-2 when registered after
 * dapprobe at 2-1, but the actual value lives in s_device.desc.busid
 * after a successful register and is not guaranteed by this API).
 *
 * EP1 IN payload: 2 bytes. payload[0] = 1 (connected) or 0 (disconnected);
 * payload[1] = 0. On poll with no pending state change, EP1 IN blocks
 * for up to ~1 s waiting for an event; if none arrives it returns a ZLP
 * and the host re-polls.
 *
 * SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Annealage-firmware-exception
 */

#ifndef MPY_POD_HUB_DEVICE_H
#define MPY_POD_HUB_DEVICE_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Register the hub_device virtual device with the USB/IP server.
 * Idempotent: re-calling after a successful register returns 0. */
int hub_device_register(void);

/* True once hub_device_register() has successfully registered the
 * virtual device. */
bool hub_device_is_registered(void);

/* Latch a DUT connect (true) or disconnect (false) event for the next
 * EP1 IN poll. Safe to call from any FreeRTOS task. */
void hub_device_notify(bool dut_connected);

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_HUB_DEVICE_H */
