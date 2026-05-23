/* mpy-pod uartcdc unit-test stubs for usbip server registration.
 * Forwards to the host-portable virtual_device.c registry. */

#include "../../../src/c_modules/usbip/usbip_server.h"
#include "../../../src/c_modules/usbip/virtual_device.h"

int usbip_server_register_virtual_device(virtual_device_t *dev)
{
    return usbip_register_virtual_device(dev);
}
