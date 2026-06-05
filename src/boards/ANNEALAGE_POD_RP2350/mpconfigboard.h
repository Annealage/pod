// Annealage Pod RP2350 board variant.
//
// Raspberry Pi Pico 2 W base (RP2350 + CYW43 Wi-Fi) with the native USB
// controller in HOST mode (rhport 0) for the DUT. Because the native USB port
// is the DUT host, there is no USB-CDC REPL; pod management is over Wi-Fi.
// No MICROPY_HW_USB_HOST_DP_PIN is defined, so the host is native, not PIO USB.

#define MICROPY_HW_BOARD_NAME                   "Annealage Pod RP2350"
#define MICROPY_HW_FLASH_STORAGE_BYTES          (PICO_FLASH_SIZE_BYTES - 1536 * 1024)

// Networking via CYW43 (Pico 2 W).
#define MICROPY_PY_NETWORK 1
#define MICROPY_PY_NETWORK_HOSTNAME_DEFAULT     "annealage-pod"

// CYW43 driver configuration (matches RPI_PICO2_W).
#define CYW43_USE_SPI (1)
#define CYW43_LWIP (1)
#define CYW43_GPIO (1)
#define CYW43_SPI_PIO (1)

// Native USB host for the DUT.
#define MICROPY_HW_USB_HOST (1)

// Backup REPL on UART0 (GP0 = TX, GP1 = RX, 115200), always-on alongside the
// Wi-Fi socket REPL and the USB-CDC REPL. It is a separate stdio path, not the
// single os.dupterm slot (which Wi-Fi uses), and it survives the native USB
// switching to host mode (when there is no USB-CDC). Intended out-of-band route:
// the pico-probe's USB-UART bridge, crossed pod GP0/GP1 <-> probe GP5/GP4 (the
// debugprobe bridge is uart1 on GP4 = TX / GP5 = RX), so one probe gives both
// SWD programming and a backup REPL. GP0/GP1 are therefore reserved for the REPL
// and must not be used as DUT/LA pins. See docs/rp2350/dev-notes.md.
#define MICROPY_HW_ENABLE_UART_REPL (1)

#define MICROPY_HW_PIN_EXT_COUNT    CYW43_WL_GPIO_COUNT

int mp_hal_is_pin_reserved(int n);
#define MICROPY_HW_PIN_RESERVED(i) mp_hal_is_pin_reserved(i)
