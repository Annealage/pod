// MicroPython board variant: ESP32_S3_ANNEALAGE_POD
//
// ESP32-S3-WROOM-1-N16R8 module on the Annealage Pod PCB.
// USB-OTG is reserved for host mode (DUT pass-through), so the native
// USB-CDC/USB-Serial-JTAG console paths are disabled and the REPL runs
// on UART0 via the onboard CH340N USB-UART bridge (GPIO43/44).
//
// See docs/spec.md and docs/spec-appendix-A-pinmap.md for the full pin
// map; this header only defines the bindings MicroPython itself looks
// at directly. C user modules consume Appendix A's GPIO assignments
// from src/c_modules/pinmap.h (added in Phase 2).

#define MICROPY_HW_BOARD_NAME               "Annealage Pod (ESP32-S3-WROOM-1-N16R8)"
#define MICROPY_HW_MCU_NAME                 "ESP32S3"

// REPL: UART0 only. USB-OTG (GPIO19/20) and USB-Serial/JTAG share pins
// and are both disabled because USB-OTG is dedicated to DUT host mode.
#define MICROPY_HW_ENABLE_UART_REPL         (1)
#define MICROPY_HW_ENABLE_USBDEV            (0)
#define MICROPY_HW_USB_CDC                  (0)
#define MICROPY_HW_ESP_USB_SERIAL_JTAG      (0)

// Local I2C bus to onboard INA228 monitors and carrier-ID EEPROM.
// Appendix A §A.5: SDA on GPIO8, SCL on GPIO9.
#define MICROPY_HW_I2C0_SDA                 (8)
#define MICROPY_HW_I2C0_SCL                 (9)

// mDNS/network hostname default.
#define MICROPY_PY_NETWORK_HOSTNAME_DEFAULT "annealage-pod"
