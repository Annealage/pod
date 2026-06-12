# Annealage Pod RP2350: Pico 2 W base with the native USB controller in host
# mode (rhport 0) for the DUT. See mpconfigboard.h for the rationale.

set(PICO_BOARD "pico2_w")

# Networking (CYW43 + lwIP) and Bluetooth, matching RPI_PICO2_W. Bluetooth is
# kept on because the pico_btstack_hci_transport_cyw43 link is what pulls in
# pico_cyw43_driver (and thus pico/cyw43_driver.h) on the rp2 port; a CYW43
# board with BT off fails to build cyw43_bus_pio_spi.c. (A no-BT board would
# need the rp2 CMakeLists to link pico_cyw43_driver for CYW43 directly.)
set(MICROPY_PY_LWIP ON)
set(MICROPY_PY_NETWORK_CYW43 ON)
set(MICROPY_PY_BLUETOOTH ON)
set(MICROPY_BLUETOOTH_BTSTACK ON)
set(MICROPY_PY_BLUETOOTH_CYW43 ON)

# Native USB host for the DUT (no PIO USB; no DP pin defined).
set(MICROPY_HW_USB_HOST 1)

# Raw USB/IP forwarder build: disable the TinyUSB host class drivers so they do
# not claim the DUT's interfaces (the pod forwards raw URBs over USB/IP, it does
# not terminate the DUT), and enable the generic endpoint-transfer API so
# tuh_edpt_xfer delivers the user completion callback (without it the callback is
# silently dropped). These must reach every USB compilation unit
# (machine_usb_host.c, shared/tinyusb/mp_usbh.c, the usbhost module, tinyusb),
# so define them via MICROPY_DEF_BOARD.
list(APPEND MICROPY_DEF_BOARD CFG_TUH_CDC=0 CFG_TUH_MSC=0 CFG_TUH_HID=0)
list(APPEND MICROPY_DEF_BOARD CFG_TUH_API_EDPT_XFER=1)

# Frozen Python package.
set(MICROPY_FROZEN_MANIFEST ${MICROPY_BOARD_DIR}/manifest.py)
