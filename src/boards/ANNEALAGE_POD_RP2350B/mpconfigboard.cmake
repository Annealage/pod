# Annealage Pod RP2350B: Waveshare RP2350B-Plus-W base with the native USB
# controller in host mode (rhport 0) for the DUT. Same pod role as
# ANNEALAGE_POD_RP2350 (Pico 2 W); the deltas are all board hardware:
# RP2350B (48 GPIOs) instead of RP2350A, 16 MB flash instead of 4 MB, an
# optional QSPI PSRAM on XIP CS1, and the CYW43 radio on GPIO36-39 instead of
# GPIO23-29. See mpconfigboard.h and waveshare_rp2350b_plus_w.h.

# The QFN-80 part exposes 48 GPIOs. This selects the rp2350b pin alternate-
# function table, and (via PICO_RP2350A=0 in the board header) enables
# PICO_PIO_USE_GPIO_BASE so a PIO can reach the radio pins above GPIO31.
set(PICO_NUM_GPIOS 48)

# Waveshare boards have no upstream pico-sdk support, so the board header is
# carried in this directory and found by adding it to the header search path.
list(APPEND PICO_BOARD_HEADER_DIRS ${MICROPY_BOARD_DIR})
set(PICO_BOARD "waveshare_rp2350b_plus_w")

# 16 MB W25Q128 on QSPI CS0.
set(PICO_FLASH_SIZE_BYTES 16777216)

# Filesystem size: all of flash bar a 1.5 MB firmware reserve, matching the
# reserve used by ANNEALAGE_POD_RP2350. This has to be a CMake variable, not
# just a mpconfigboard.h define: the port turns it into the
# __micropy_flash_storage_bytes__ link symbol that sizes the FLASH and FLASH_FS
# regions in memmap_mp_rp2350.ld, and without it both region lengths underflow
# and the linker stops checking that the firmware and the filesystem do not
# overlap. The port also emits the matching C define, so the header must not.
if(NOT DEFINED MICROPY_HW_FLASH_STORAGE_BYTES)
    set(MICROPY_HW_FLASH_STORAGE_BYTES 15204352)  # 16 MB - 1536 * 1024
endif()

# Networking (CYW43 + lwIP) and Bluetooth, matching ANNEALAGE_POD_RP2350.
# Bluetooth is kept on because the pico_btstack_hci_transport_cyw43 link is what
# pulls in pico_cyw43_driver (and thus pico/cyw43_driver.h) on the rp2 port; a
# CYW43 board with BT off fails to build cyw43_bus_pio_spi.c.
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

# Two concurrent lwIP-RAW TCP listeners (usbip:3240 + the Wi-Fi socket REPL:8266)
# run under NO_SYS=1 and share lwIP's global TCP PCB pools. Raise MEMP_NUM_TCP_PCB
# so usbip's accepted connections (up to USBIP_MAX_CONNS=4), the REPL's client, the
# REPL accept backlog, and TIME_WAIT churn never starve the REPL - otherwise new
# REPL connects are RST'd while forwarding (the pod's only management channel on a
# deployed unit). Set via MICROPY_DEF_BOARD so the define reaches the lwIP sources
# (memp.c sizes the pools), which compile into the firmware target through the
# micropy_lib_lwip INTERFACE library. MEMP_NUM_TCP_PCB_LISTEN is pinned at its
# default to document intent. MEM_SIZE is deliberately untouched: this is a
# PCB-count problem, not a pbuf/segment one.
list(APPEND MICROPY_DEF_BOARD MEMP_NUM_TCP_PCB=10 MEMP_NUM_TCP_PCB_LISTEN=8)

# Frozen Python package.
set(MICROPY_FROZEN_MANIFEST ${MICROPY_BOARD_DIR}/manifest.py)
