// Annealage Pod: USB/IP server skeleton.
//
// Phase 1 exports a single entry point `usbip_server_start()` that logs
// a line and returns. Phase 2 brings up the real TCP listener on
// port 3240 and the USB/IP protocol multiplexer.

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

void usbip_server_start(void);

#ifdef __cplusplus
}
#endif
