// Annealage Pod RP2350B board variant.
//
// Waveshare RP2350B-Plus-W base (RP2350B + 16 MB flash + optional QSPI PSRAM +
// Raspberry Pi RM2 / CYW43439 radio) with the native USB controller in HOST
// mode (rhport 0) for the DUT. No MICROPY_HW_USB_HOST_DP_PIN is defined, so the
// host is native, not PIO USB.
//
// Same pod role as ANNEALAGE_POD_RP2350; only the board hardware differs. The
// DUT-facing pin assignments in annealage_pod._rp2_pinmap (SWD GP14/GP15, nRST
// GP13, I2C1 GP10/GP11, UART1 GP4/GP5, SPI GP16-GP19) are unchanged and all sit
// on this board's 40-pin header, so a DUT harness moves between the two pods
// without rewiring.

#define MICROPY_HW_BOARD_NAME                   "Annealage Pod RP2350B"
// MICROPY_HW_FLASH_STORAGE_BYTES is deliberately not defined here: it is set in
// mpconfigboard.cmake so that it also reaches the linker script.

// Networking via the RM2 (CYW43439). Pin assignments come from the board header.
// The runtime hostname is replaced by netboot.pod_name() with a chip-ID-derived
// per-unit name before the interface comes up; this default only applies if that
// never runs.
#define MICROPY_PY_NETWORK 1
#define MICROPY_PY_NETWORK_HOSTNAME_DEFAULT     "annealage-pod"

// CYW43 driver configuration (matches ANNEALAGE_POD_RP2350).
#define CYW43_USE_SPI (1)
#define CYW43_LWIP (1)
#define CYW43_GPIO (1)
#define CYW43_SPI_PIO (1)

// QSPI PSRAM on XIP CS1 = GPIO47, sharing the flash bus. The footprint ships
// unpopulated, so this is a probe, not an assertion: psram_init() reads the
// device ID and returns 0 when no chip is fitted, and rp2 main.c then falls back
// to the SRAM-only GC heap. With a chip fitted the heap is split (SRAM plus the
// PSRAM window at 0x11000000) rather than replaced, so short-lived allocations
// still land in fast SRAM.
#define MICROPY_HW_ENABLE_PSRAM (1)
#define MICROPY_HW_PSRAM_CS_PIN (WAVESHARE_RP2350B_PLUS_W_PSRAM_CS_PIN)

// Native USB host for the DUT.
#define MICROPY_HW_USB_HOST (1)

// Backup REPL on UART0 (GP0 = TX, GP1 = RX, 115200), always-on alongside the
// Wi-Fi socket REPL and the USB-CDC REPL. It is a separate stdio path, not the
// single os.dupterm slot (which Wi-Fi uses), and it survives the native USB
// switching to host mode (when there is no USB-CDC). Intended out-of-band route:
// the pico-probe's USB-UART bridge, crossed pod GP0/GP1 <-> probe GP5/GP4 (the
// debugprobe bridge is uart1 on GP4 = TX / GP5 = RX), so one probe gives both
// SWD programming and a backup REPL. GP0/GP1 are therefore reserved for the REPL
// and must not be used as DUT/LA pins. See docs/pod/dev-notes.md.
#define MICROPY_HW_ENABLE_UART_REPL (1)

#define MICROPY_HW_PIN_EXT_COUNT    CYW43_WL_GPIO_COUNT

int mp_hal_is_pin_reserved(int n);
#define MICROPY_HW_PIN_RESERVED(i) mp_hal_is_pin_reserved(i)
