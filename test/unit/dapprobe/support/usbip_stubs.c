/* mpy-pod WS-C unit-test stubs for the usbip server registration
 * surface. The host tests do not run a real TCP listener; they just
 * exercise the synthetic device's ops table and descriptor blobs.
 * usbip_server_register_virtual_device forwards to the host-portable
 * virtual_device.c (linked in directly) so the registry returns the
 * normal -EEXIST / 0 results. */

#include "../../../src/c_modules/usbip/usbip_server.h"
#include "../../../src/c_modules/usbip/virtual_device.h"

int usbip_server_register_virtual_device(virtual_device_t *dev) {
    return usbip_register_virtual_device(dev);
}
