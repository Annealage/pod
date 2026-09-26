/* Annealage Pod: USB host (TinyUSB) interface used by the USB/IP server.
 *
 * Implemented by usbhost_rp2.c. The header is host-portable; only the
 * implementation needs TinyUSB and the Pico SDK.
 */

#ifndef MPY_POD_USBHOST_H
#define MPY_POD_USBHOST_H

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "../usbip/usbip_protocol.h"
#include "../usbip/usbip_device.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Bring up the TinyUSB host stack. */
int usbhost_start(void);

/* Populate the array with the USB devices currently enumerated.
 * Returns the count copied. */
size_t usbhost_get_devices(usbip_dev_record_t *out, size_t max);

/* Look up a real-USB device by busid. Returns true on hit. */
bool usbhost_get_device_by_busid(const char busid[USBIP_BUSID_SIZE],
                                 usbip_dev_record_t *out);

/* Copy the cached raw device descriptor (18 bytes, USB-spec layout)
 * for the device at `busid` into `out` (capacity `cap`). Sets `*out_len`
 * to the bytes written (min(cap, 18)). Returns true if the cache had a
 * descriptor; false if busid unknown or no cache entry yet.
 *
 * Used by the USB/IP server to serve EP0 GET_DESCRIPTOR(DEVICE) replies
 * locally instead of round-tripping to the device. Devices whose
 * firmware STALLs repeat GET_DESCRIPTOR after TinyUSB's own boot-time
 * enumeration (e.g. the dabao DUT) need this to enumerate cleanly
 * over USB/IP. */
bool usbhost_get_cached_device_desc(const char busid[USBIP_BUSID_SIZE],
                                    uint8_t *out, size_t cap, size_t *out_len);

/* Copy the cached raw configuration descriptor (variable length, USB-
 * spec layout) for the device at `busid` into `out`. Sets `*out_len`
 * to the bytes written (min(cap, cached cfg_len)). Returns true if the
 * cache had a descriptor. Companion to usbhost_get_cached_device_desc. */
bool usbhost_get_cached_config_desc(const char busid[USBIP_BUSID_SIZE],
                                    uint8_t *out, size_t cap, size_t *out_len);

/* Submit a control transfer (EP0). The setup packet's direction
 * determines whether the data stage is OUT (out_data/out_len) or IN
 * (in_data/in_capacity, *in_len). Returns 0 on success, negative
 * errno on failure. The cancel flag, if non-NULL, is polled by the
 * backend and aborts the transfer when set. */
int usbhost_control_transfer(const char busid[USBIP_BUSID_SIZE],
                             const usbip_setup_packet_t *setup,
                             const uint8_t *out_data, size_t out_len,
                             uint8_t *in_data, size_t in_capacity, size_t *in_len,
                             volatile bool *cancel);

/* Submit a bulk transfer on a non-zero endpoint. ep_addr's high bit
 * carries direction (0x8N = IN, 0x0N = OUT). */
int usbhost_bulk_transfer(const char busid[USBIP_BUSID_SIZE],
                          uint8_t ep_addr,
                          const uint8_t *out_data, size_t out_len,
                          uint8_t *in_data, size_t in_capacity, size_t *in_len,
                          volatile bool *cancel);

/* Submit an interrupt transfer; same shape as bulk. */
int usbhost_interrupt_transfer(const char busid[USBIP_BUSID_SIZE],
                               uint8_t ep_addr,
                               const uint8_t *out_data, size_t out_len,
                               uint8_t *in_data, size_t in_capacity, size_t *in_len,
                               volatile bool *cancel);


/* Returns true if (busid, ep_num, direction) refers to an interrupt
 * endpoint on the real-device side. The multiplexer uses this to
 * choose between bulk and interrupt transfer paths. */
bool usbhost_is_interrupt_endpoint(const char busid[USBIP_BUSID_SIZE],
                                   uint8_t ep_num, uint8_t direction);

/* Non-blocking async submit. Calls cb(ctx, status, in_len) from the
 * tuh_task context when the transfer completes; ownership of the buffers
 * stays with the host stack until then. Returns 0 if the submit was
 * accepted, negative errno on immediate failure (the callback is then
 * NOT called). ep_addr's high bit carries direction (0x8N = IN, 0x0N =
 * OUT). The UNLINK handler cancels via usbhost_cancel_ep. */
int usbhost_submit_async(const char busid[USBIP_BUSID_SIZE],
                         uint8_t ep_addr, bool is_control,
                         const usbip_setup_packet_t *setup,
                         const uint8_t *out_data, size_t out_len,
                         uint8_t *in_data, size_t in_capacity,
                         void (*cb)(void *ctx, int status, size_t in_len),
                         void *ctx);

/* Force-cancel any in-flight URB on this endpoint, called from the
 * UNLINK handler. The cancelled URB completes through its async
 * callback with status -ECONNRESET. */
void usbhost_cancel_ep(const char busid[USBIP_BUSID_SIZE], uint8_t ep_addr);

/* Toggle per-URB logging on the usbhost backend: one line per transfer
 * submit and completion. Default off. */
void usbhost_set_verbose(bool enable);

/* True if per-URB verbose logging is currently enabled. */
bool usbhost_is_verbose(void);

/* In-place USB identity change recovery (flush + bus_reset are split
 * because of a DWC2 PRT_CONN_DET edge-trigger quirk; see below).
 *
 * Use case: a DUT changes USB device identity in place behind a stuck
 * D+ pull-up. The canonical example is the Baochip dabao going from
 * boot1's CDC profile to user-firmware's CDC profile without ever
 * tearing the pull-up down. Without a falling edge on D+, the host
 * never sees a disconnect, tuh_umount_cb never fires, and TinyUSB
 * serves the cached boot1 descriptors over USB/IP forever. The
 * recovery is to forcibly evict the cached record from the host
 * stack, and (in some cases) drive a fresh USB RESET event so the
 * chip-side EP0 sees a recognisable PORTSC.PR transition and re-arms.
 *
 * usbhost_flush: runs tuh_deinit(rhport) + mp_usbh_init_tuh().
 * tuh_deinit fires tuh_umount_cb for every attached device (clearing
 * _usbh_data.devices[] via clear_device) and resets the DWC2
 * controller; mp_usbh_init_tuh re-initialises with hot-plug detection
 * re-armed. force_bus_reset=False takes a lightweight cache-only path
 * (descriptor cache + slot table wipe only, no host stack restart).
 *
 * usbhost_bus_reset: standalone 10 ms SE0 drive via
 * tuh_rhport_reset_bus, no host stack state changes.
 *
 * Why two entry points: DWC2's PRT_CONN_DET only fires on a 0->1
 * transition. If the DUT holds D+ pull-up high throughout the
 * deinit-init window (which a well-behaved DCD does), the post-init
 * PHY may sense "already J state" with no transition to detect,
 * HCD_EVENT_DEVICE_ATTACH never queues, and enum_new_device's
 * implicit hcd_port_reset never executes. In that case the chip never
 * sees a USB RESET event after flush(). A subsequent bus_reset()
 * forces SE0 on the wire regardless of host stack state.
 *
 * For a chip whose DCD correctly handles tuh_init's natural
 * enumeration sequence (port_reset is part of enum_new_device for
 * a freshly-detected device), flush() alone is sufficient. Callers
 * who suspect they're in the edge-trigger-miss case should chain
 * flush() + bus_reset().
 *
 * Both return 0 on success, negative errno on failure. Safe to call
 * when no device is attached. */
int usbhost_flush(bool force_bus_reset);
int usbhost_bus_reset(void);

/* usbhost_reprobe: recover a device that reconnected on the root port WITHOUT
 * a connect-detect edge - the exact edge-trigger-miss case above (a DUT warm
 * reset / machine.reset() with no VBUS drop, or a D+ pull-up reconnect). Where
 * flush()/bus_reset() depend on the PHY sensing a 0->1 transition that a held-
 * high D+ never produces, reprobe synthesizes HCD_EVENT_DEVICE_ATTACH directly
 * (hcd_event_device_attach) and pumps tuh_task: TinyUSB force-removes the stale
 * device at the root-port bus address and runs enum_new_device, whose port
 * reset + descriptor/SET_ADDRESS sequence re-enumerates the device on the wire.
 * No-op (returns -ENODEV) when the host is not started or no device is present
 * on the port (hcd_port_connect_status). Returns 0 on success, negative errno
 * otherwise. reprobe re-seeds the forwarding slot table itself before returning,
 * so a separate usbip.start() rescan is not required. */
int usbhost_reprobe(void);

/* Bitmask of TinyUSB device addresses currently mounted (bit N = address N).
 * Diagnostic to tell "enumeration failed" (0) from "mounted but the forwarding
 * slot / export did not populate" (nonzero). */
uint32_t usbhost_mounted_mask(void);

/* Bitmask of device addresses whose descriptor cache is valid (bit N = addr N).
 * Diagnostic: enumerate_device needs a valid cache to build the export slot.
 * Compare with usbhost_mounted_mask() to tell a cache-missing fault from a
 * rescan-not-run fault. */
uint32_t usbhost_cache_valid_mask(void);

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_USBHOST_H */
