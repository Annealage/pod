/* Annealage Pod: USB host (TinyUSB) abstraction.
 *
 * Phase 2 WS-A scope: declarations only. WS-B replaces the stub
 * implementation with the real TinyUSB host integration. The shape
 * of this API tracks referencea/esp-usbip-bridge/main/usb_backend.h
 * so the multiplexer can use it without knowing whether the backend
 * is a stub or the real TinyUSB host stack.
 *
 * This header is host-portable on purpose; usbhost.c on-target is
 * the only file that needs IDF / TinyUSB headers.
 */

#ifndef MPY_POD_USBHOST_H
#define MPY_POD_USBHOST_H

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "../usbip/usbip_protocol.h"
#include "../usbip/virtual_device.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Status codes returned by the URB submit functions. Negative errno
 * with the standard meaning. The Phase 2 WS-B stub returns -ENOSYS
 * to signal "real USB host not yet wired up"; the multiplexer maps
 * that into a transparent USBIP_RET_SUBMIT error so the host sees
 * an honest "no such function" rather than a silent zero-length
 * completion. */
#define USBHOST_ERR_NOT_IMPLEMENTED (-ENOSYS)

/* Bring up the USB host stack. The Phase 2 stub returns 0 without
 * doing anything; WS-B replaces this with TinyUSB host init. */
int usbhost_start(void);

/* Populate the array with the real-USB devices currently enumerated.
 * Returns the count copied (0 in the Phase 2 stub). */
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

/* usbhost_submit_order_t and the _ordered transfer variants are removed
 * in R20 step3. Per-EP lane tasks serialise submit order by construction
 * (FIFO queue per (ep,dir)). Use usbhost_bulk_transfer and
 * usbhost_interrupt_transfer directly. */

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

/* Non-blocking async submit. Calls cb(ctx, status, in_len) from the IDF
 * worker context (priority 9) when the transfer completes. The caller does
 * NOT block; ownership of the transfer is with the IDF until the callback
 * fires. Returns 0 if the IDF accepted the submit, negative errno on
 * immediate failure (IDF rejected submit; callback will NOT be called in
 * that case). ep_addr's high bit carries direction (0x8N = IN, 0x0N = OUT).
 * cancel is polled by the per-EP lane prior to submit; the UNLINK handler
 * drives cancellation via usbhost_cancel_ep instead of the cancel flag. */
int usbhost_submit_async(const char busid[USBIP_BUSID_SIZE],
                         uint8_t ep_addr, bool is_control,
                         const usbip_setup_packet_t *setup,
                         const uint8_t *out_data, size_t out_len,
                         uint8_t *in_data, size_t in_capacity,
                         void (*cb)(void *ctx, int status, size_t in_len),
                         void *ctx);

/* Synchronous halt+flush+clear of a specific endpoint. Called from the
 * UNLINK handler (under the per-EP submit mutex) to force-cancel any
 * in-flight URB on this EP. The IDF delivers the cancelled URB to the
 * async callback with status -ECONNRESET. */
void usbhost_cancel_ep(const char busid[USBIP_BUSID_SIZE], uint8_t ep_addr);

/* Toggle per-URB observability logging on the usbhost backend. When
 * enabled every transfer submit and completion emits one ESP_LOGI
 * line under the "usbhost" tag. Default off. */
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

/* Diagnostic: return the raw 32-bit value of the DWC2 HPRT (Host Port
 * Control and Status) register, or 0 if the host stack is not running.
 * Useful to disambiguate "no device on the bus" from "device on bus but
 * host state machine wedged":
 *
 *   bit  0  PRT_CONN_STS   1 = device connected (D+/D- pull-up detected)
 *   bit  1  PRT_CONN_DET   1 = port connect detected (W1C)
 *   bit  2  PRT_EN          1 = port enabled
 *   bit  3  PRT_EN_CHNG     1 = port enable changed (W1C)
 *   bit  8  PRT_RST         1 = port reset asserted
 *   bits 17:18 PRT_SPD      0=HS, 1=FS, 2=LS
 */
uint32_t usbhost_dwc2_hprt(void);

/* Diagnostic: sample HPRT at a high rate for a bounded duration and
 * record every value transition with a timestamp (microseconds from
 * trace start). Used to observe bus-state changes during a DUT
 * power-cycle or PROG+RESET when the kernel/TinyUSB-level mount/umount
 * events are not granular enough.
 *
 * Each captured transition is written into out[] as a pair of
 * (t_us, hprt). out_n receives the number of pairs written (clamped to
 * cap). Initial HPRT value is recorded as the first entry at t=0; from
 * there only transitions are appended.
 *
 * duration_ms is capped at HPRT_TRACE_MAX_DURATION_MS internally to
 * keep the loop bounded. period_us is the minimum spacing between
 * polls (a hint, not a tight bound).
 *
 * Returns 0 on success, negative errno on failure. */
typedef struct {
    uint32_t t_us;
    uint32_t hprt;
} usbhost_hprt_sample_t;

int usbhost_hprt_trace(uint32_t duration_ms, uint32_t period_us,
                       uint32_t force_every, usbhost_hprt_sample_t *out,
                       size_t cap, size_t *out_n);

/* Per-endpoint URB counters for a given busid. Indexed by
 * ep_mutex_index (low 4 bits = EP number, bit 4 = direction:
 * 0=OUT/control, 1=IN). 32 entries total.
 *
 * Used to triage "URB went out but completion never landed" vs "URB
 * was never submitted in the first place" failure modes - the
 * counters increment at the points where the host-stack code
 * actually has control, so a discrepancy between submitted and
 * completed-or-errored points at the boundary where transfers are
 * being lost.
 */
typedef struct {
    uint32_t submitted;   /* tuh_*_xfer called (URB handed to TinyUSB) */
    uint32_t completed;   /* xfer_complete_cb fired with XFER_RESULT_SUCCESS */
    uint32_t errored;     /* xfer_complete_cb fired with non-SUCCESS result */
    uint32_t cancelled;   /* usbhost_cancel_ep synthesised a completion */
} usbhost_ep_stats_t;

/* Copy the 32-entry per-EP stats array for the device at `busid` into
 * `out` (must be at least 32 entries). Returns true on hit, false if
 * busid not found. */
bool usbhost_get_ep_stats(const char busid[USBIP_BUSID_SIZE],
                          usbhost_ep_stats_t out[32]);

#ifdef __cplusplus
}
#endif

#endif /* MPY_POD_USBHOST_H */
