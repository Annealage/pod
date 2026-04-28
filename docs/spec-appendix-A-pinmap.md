# Appendix A: Pin Map (DUT carrier and ESP32-S3 GPIO assignment)

Status: draft, derived from the Octoprobe Annealage Pod v0.7.1 schematic
(`referencea/annealage_pod/kicad/annealage_pod_v0.7/`, schematic PDF
`production_v0.7/schematics_annealage_pod_v0.7.pdf`, 14 pages, KiCad 9.0.7,
date stamp 2026-02-18).

This appendix populates the placeholder in `spec.md` §A and answers the
follow-up item in §8.5 (pin budget reality check on S3-WROOM-1-N16R8).

## A.1 Reality check on the existing 'DUT carrier' interface

The existing v0.7 hardware does NOT expose a single 2x20 0.1" pitch
DUT carrier connector. The schematic instead splits the DUT-facing
signals across four separate 2.54 mm headers:

| Designator | Type           | Footprint                                    | Role                                    | Sheet (pg) |
|------------|----------------|----------------------------------------------|-----------------------------------------|-----------:|
| J1501      | Conn_02x12     | PinSocket_2x12_P2.54mm_Vertical_squarepad    | RP_PROBE breakout, level-shifted to DUT |        14  |
| J201       | Conn_02x05     | PinSocket_2x05_P2.54mm_Vertical_squarepad    | RP_INFRA GPIO breakout                  |         2  |
| J501       | Conn_02x14     | PinSocket_2x14_P2.54mm_Vertical_squarepad    | Opto-relay output pairs                 |         5  |
| J1303      | Conn_01x40 sym, PinSocket_2x20 footprint | (DNP twin J1302) prototype/'Octobus' area, no annealage_pod-driven nets in schematic | 13 |

J1303 is the only physical 2x20 pad pattern on the v0.7 PCB, but in
the schematic it has no labels, no hierarchical pins, and no driven
nets - it is exposed prototyping area, not a DUT carrier interface.
J1302 is `(dnp yes)` in the schematic (do-not-populate) and shares
PCB pads with J1303.

The §3.1 spec language ("re-uses the Octoprobe Annealage Pod 2x20 0.1"
pitch DUT socket connector pinout") describes a design *intent* for
the new ESP32-S3 PCB: consolidate everything that v0.7 currently
distributes across J1501, J201, and J501 plus the missing
DUT-USB / VTARGET / I2C-ID / DUT-UART signals into a single 40-pin
0.1" pitch socket. The remainder of this appendix proposes that
unified 40-pin layout and binds it to ESP32-S3-WROOM-1-N16R8 GPIOs.

## A.2 Existing v0.7 signals that must travel to a future single 40-pin DUT carrier

Aggregated across the four existing headers:

- 14 RP_PROBE GPIOs reaching the DUT through TXB0108 translators
  (named GPD0, GPD1, GPD2, GPD3, GPD4, GPD5, GPD6, GPD7, GPD8, GPD9,
  GPD10, GPD11, GPD12, GPD13, of which GPD2-GPD5 are SWCLK / SWDIO /
  UART_TX / UART_RX in the picoprobe firmware mapping per
  `referencea/annealage_pod/docs/rp2_probe.rst`)
- VDUT (level-shifter B-side reference, sets DUT logic level)
- 7 opto-relay output pairs (RELAY_OUT_1A/B through RELAY_OUT_7A/B,
  i.e. dry-contact pairs for boot-button presses and arbitrary
  digital signal injection; per page 6-12 each is a G3VM/GAQY221S
  MOSFET-relay)
- DUT USB VBUS (5 V) and DUT USB D+ / D- (carried on a separate USB
  Type-A receptacle J202 on v0.7, page 1; on the new PCB the spec
  routes this through the ESP32-S3 USB-OTG controller acting as host)
- VTARGET (3V3 in v0.7 from regulator U202 / current-limited via
  TPS2595 U207, see page 16; rev1 fixed at 3v3, programmable
  deferred to rev2)
- nRST line to DUT (not present as a dedicated net in v0.7; the
  v0.7 design relies on SWD AIRCR.SYSRESETREQ or a relay press for
  reset)
- GND
- I2C-EEPROM / strap-resistor carrier-ID lines (not present in v0.7;
  added in spec §3.6)

Existing v0.7 has no I2C-slave / SPI-slave to the DUT and no
INA228 power monitors; those are spec additions for the new PCB.

## A.3 Proposed 40-pin DUT carrier signal table

This is the proposed layout for the new ESP32-S3 PCB. Pin numbering
follows the standard 2x20 odd/even convention: pin 1 top-left, pin
2 top-right, pin 39 bottom-left, pin 40 bottom-right. Direction is
specified at the carrier-side translator output (i.e. as seen by the
DUT plugged into the carrier). 'Translator' column says which kind
of 74LVC1T45 strapping is used; 'fixed-out' = DIR tied high on the
S3 side, 'fixed-in' = DIR tied low, 'DIR-ctrl' = DIR driven by an
S3 GPIO (runtime direction switching).

| Pin | Net name        | Signal type           | Direction at carrier | Translator       | Notes                                                            |
|----:|-----------------|-----------------------|----------------------|------------------|------------------------------------------------------------------|
|   1 | VTARGET         | 3v3 power rail        | out (sourced)        | none (analog)    | Switched by TPS2595, INA228 monitored                            |
|   2 | GND             | ground                | -                    | none             |                                                                  |
|   3 | DUT_USB_VBUS    | 5 V power rail        | out (sourced)        | none (analog)    | Switched by TPS2595, INA228 monitored, ADC-sensed                |
|   4 | GND             | ground                | -                    | none             |                                                                  |
|   5 | DUT_USB_DP      | USB D+ (full speed)   | bidir                | none (USB PHY)   | Direct from S3 GPIO20, no translator (USB-OTG host)              |
|   6 | DUT_USB_DM      | USB D- (full speed)   | bidir                | none (USB PHY)   | Direct from S3 GPIO19, no translator (USB-OTG host)              |
|   7 | SWCLK           | SWD clock             | out                  | fixed-out        | SPI2 SCLK; up to 25 MHz steady, 40 MHz with short wires          |
|   8 | SWDIO           | SWD bidir data        | bidir                | DIR-ctrl         | SPI2 D pin; DIR toggled per SWD frame phase                      |
|   9 | SWO             | SWO trace input       | in                   | fixed-in         | UART1 RX via UHCI/GDMA, NRZ, several Mbps                        |
|  10 | nRST            | DUT reset             | out, open-drain      | fixed-out OD     | OD via translator; tristated on idle                             |
|  11 | DUT_UART_TX     | UART from S3 to DUT   | out                  | fixed-out        | S3 UART2 TX                                                      |
|  12 | DUT_UART_RX     | UART from DUT to S3   | in                   | fixed-in         | S3 UART2 RX                                                      |
|  13 | DUT_I2C_SDA     | slave I2C data, or SPI MOSI (mux) | bidir / in | DIR-ctrl       | I2C-slave SDA needs DIR; SPI-slave shares pin as MOSI input      |
|  14 | DUT_I2C_SCL     | slave I2C clock, or SPI SCK (mux) | in        | fixed-in         | Slave clock is always input on S3                                |
|  15 | DUT_SPI_MISO    | SPI MISO (mux only)   | out                  | fixed-out        | Active only when SPI-slave personality selected                  |
|  16 | DUT_SPI_CS      | SPI CS (mux only)     | in                   | fixed-in         | Active only when SPI-slave personality selected                  |
|  17 | RELAY_1A        | opto-relay 1 contact A| dry contact          | none (opto)      | Driven by S3 GPIO via opto-coupler in 7.1 sheet style            |
|  18 | RELAY_1B        | opto-relay 1 contact B| dry contact          | none (opto)      |                                                                  |
|  19 | RELAY_2A        | opto-relay 2 contact A| dry contact          | none (opto)      |                                                                  |
|  20 | RELAY_2B        | opto-relay 2 contact B| dry contact          | none (opto)      |                                                                  |
|  21 | RELAY_3A        | opto-relay 3 contact A| dry contact          | none (opto)      |                                                                  |
|  22 | RELAY_3B        | opto-relay 3 contact B| dry contact          | none (opto)      |                                                                  |
|  23 | RELAY_4A        | opto-relay 4 contact A| dry contact          | none (opto)      |                                                                  |
|  24 | RELAY_4B        | opto-relay 4 contact B| dry contact          | none (opto)      |                                                                  |
|  25 | RELAY_5A        | opto-relay 5 contact A| dry contact          | none (opto)      |                                                                  |
|  26 | RELAY_5B        | opto-relay 5 contact B| dry contact          | none (opto)      |                                                                  |
|  27 | RELAY_6A        | opto-relay 6 contact A| dry contact          | none (opto)      |                                                                  |
|  28 | RELAY_6B        | opto-relay 6 contact B| dry contact          | none (opto)      |                                                                  |
|  29 | RELAY_7A        | opto-relay 7 contact A| dry contact          | none (opto)      |                                                                  |
|  30 | RELAY_7B        | opto-relay 7 contact B| dry contact          | none (opto)      |                                                                  |
|  31 | GPD0            | GP DUT IO 0           | bidir                | DIR-strap (TBD)  | Spare GP digital (kept for v0.7 carrier compat); fixed-direction selected on the new carrier per usage |
|  32 | GPD1            | GP DUT IO 1           | bidir                | DIR-strap (TBD)  | Spare GP digital                                                 |
|  33 | GPD6            | GP DUT IO 6           | bidir                | DIR-strap (TBD)  | Spare GP digital                                                 |
|  34 | GPD7            | GP DUT IO 7           | bidir                | DIR-strap (TBD)  | Spare GP digital                                                 |
|  35 | CARRIER_ID_SDA  | I2C EEPROM data       | bidir                | none (3v3 local) | On the S3 local I2C bus (not through translators)                |
|  36 | CARRIER_ID_SCL  | I2C EEPROM clock      | out                  | none (3v3 local) | On the S3 local I2C bus                                          |
|  37 | VTARGET_SENSE   | INA228 V- shunt sense | analog               | none             | Tap from VTARGET INA228 instrumentation amp side                 |
|  38 | GND             | ground                | -                    | none             |                                                                  |
|  39 | VBUS_SENSE      | DUT-USB VBUS sense    | analog               | divider only     | Resistor-divided to S3 ADC GPIO                                  |
|  40 | GND             | ground                | -                    | none             |                                                                  |

Pin count by class:
- Power / sense rails: 4 (VTARGET, DUT_USB_VBUS, VTARGET_SENSE, VBUS_SENSE)
- GND: 4
- USB host: 2 (D+, D-)
- SWD + SWO + nRST: 4
- DUT UART: 2
- I2C-slave / SPI-slave (muxed): 4
- Opto-relays: 14 (7 pairs)
- General-purpose DUT IO carried over from v0.7 GPD set: 4
- Carrier-ID I2C: 2

Total: 40.

Notes:

- The v0.7 GPD set has 14 entries (GPD0 through GPD13). Of those,
  GPD2, GPD3, GPD4, GPD5 are aliased to SWCLK / SWDIO / UART_TX /
  UART_RX in picoprobe / yapicoprobe firmwares; in the new design
  those four roles get dedicated carrier pins (7, 8, 11, 12 above).
  GPD8 through GPD13 have no documented function in the v0.7
  firmwares I parsed, only physical traces. The proposal keeps four
  spare GPDs (GPD0, GPD1, GPD6, GPD7) on the new carrier; the rest
  are dropped to free pin budget. Decision flagged in §A.7.
- Pin 9 (SWO) is shown as a dedicated trace input and not aliased
  to UART_RX; SWO needs UART1 RX with UHCI/GDMA per spec §4.7,
  separate peripheral instance from the DUT_UART forwarder.
- The translator on pin 8 (SWDIO) is the same 74LVC1T45 family as
  pins 7, 9, 10, etc., picked specifically for its DIR-controlled
  bidir mode; spec §3.2 picks this part to escape the TXB0108
  auto-direction SWD clock cap.
- Pin 13 (DUT_I2C_SDA) and pin 14 (DUT_I2C_SCL) double as MOSI / SCK
  in SPI-slave mode; pins 15 (MISO) and 16 (CS) are SPI-only. This
  is the muxing called out in spec §4.8 ("I2C-slave and SPI-slave
  personalities are mutually exclusive on a given test").

## A.4 Existing RP_INFRA + RP_PROBE GPIO assignment (reference)

Extracted from the v0.7 schematic. Use this as the regression
target when validating the new ESP32-S3 firmware against existing
testbed_micropython code.

### A.4.1 RP_INFRA (U201, RP2040) - schematic page 2

| RP2040 GPIO | RP2040 pin | v0.7 net      | Function                                |
|------------:|----------:|---------------|-----------------------------------------|
| GPIO0       |         2 | RELAIS_1      | Opto-relay 1 drive                      |
| GPIO1       |         3 | RELAIS_2      | Opto-relay 2 drive                      |
| GPIO2       |         4 | RELAIS_3      | Opto-relay 3 drive                      |
| GPIO3       |         5 | RELAIS_4      | Opto-relay 4 drive                      |
| GPIO4       |         6 | RELAIS_5      | Opto-relay 5 drive                      |
| GPIO5       |         7 | RELAIS_6      | Opto-relay 6 drive                      |
| GPIO6       |         8 | RELAIS_7      | Opto-relay 7 drive                      |
| GPIO7       |         9 | (unconnected) | Reserved                                |
| GPIO8-15    |        11-18 | J201 pins 1-8 | RP_INFRA general-purpose breakout    |
| GPIO16-22   |        21-29 (selection) | LED_ERR, J201 spare, etc. | LED_ERR (D203, red); LED_ACTIVE (D201, blue); spare |
| GPIO_PROBE_BOOT | (var)  | RP2_PROBE_BOOT | Pulls PROBE QSPI_SS at boot (page 14) |
| GPIO_PROBE_RUN  | (var)  | RP2_PROBE_RUN  | Drives PROBE RUN line (page 14)       |
| GPIO_DUT_PWR_EN | (var)  | DUT_PWR_EN     | Enables U205 TPS2595 5V DUT switch (page 16) |
| GPIO_INFRA_PWR_EN | (var) | RP2_INFRA_PWR_EN | (returned from USB hub port 1, page 1; not driven by INFRA itself) |
| USB_DP/USB_DM   |        46-47 | USB_INFRA_D+/- | Upstream USB to USB hub port 1        |

The exact J201 silkscreen has documented errors (see v0.7 README
"J201 Conn_02x05 Silkscreen with wrong GPIO numbers"). Treat the
schematic as the authoritative source.

### A.4.2 RP_PROBE (U1501, RP2040) - schematic page 14 (pdf id 15/14)

| RP2040 GPIO | RP2040 pin | Goes through                | DUT-side net | Notes                                                      |
|------------:|----------:|-----------------------------|--------------|------------------------------------------------------------|
| GPIO0       |         2 | (direct, no LS at v0.7)     | -            | Free                                                       |
| GPIO1       |         3 | -                            | -            | Free                                                       |
| GPIO2       |         4 | TXB0108 U1504 A1            | GPD2         | picoprobe SWCLK / yapicoprobe SWCLK                        |
| GPIO3       |         5 | TXB0108 U1504 A2            | GPD3         | picoprobe SWDIO / yapicoprobe SWDIO                        |
| GPIO4       |         6 | TXB0108 U1504 A3            | GPD4         | picoprobe UART TX / yapicoprobe UART TX                    |
| GPIO5       |         7 | TXB0108 U1504 A4            | GPD5         | picoprobe UART RX / yapicoprobe UART RX                    |
| GPIO6       |         8 | TXB0108 U1503 A1            | GPD6         | yapicoprobe SWD reset (others free)                        |
| GPIO7-9     |       9-12| TXB0108 U1503 A2-A4         | GPD7-GPD9    | logic analyzer / digital channels                          |
| GPIO10-13   |      14-17| TXB0108 U1503 A5-A8         | GPD10-GPD13  | logic analyzer / digital channels                          |
| GPIO14, 15  |      19,20| TXB0108 U1503/U1504 spare lines or J1501 direct | direct GPIO breakout | not level-shifted in v0.7   |
| GPIO16-22   |      27-29| J1501 direct (no LS)        | -            | exposed on probe breakout for user wiring                  |
| GPIO24-28   |      36-40| J1501 direct (no LS)        | -            | (GPIO26-28 are also ADC-capable)                           |
| GPIO29 (ADC3)|         41| -                            | -            | not exposed on J1501 (per probe sheet pin layout)          |
| USB_DP/DM   |       46-47| -                            | -            | Upstream USB to USB hub port 4 (USB_PROBE_D+/D-)           |
| RUN         |        26 | RP2_PROBE_RUN (driven by INFRA) | -         | RP_INFRA controls power-on of RP_PROBE                     |
| QSPI_SS     |        56 | RP2_PROBE_BOOT via diode (B5819W L) | -    | RP_INFRA pulls low at reset to force BOOT mode             |

The two TXB0108 chips are stuffed on one side or the other depending
on whether VDUT is above or below 3v3 (per `big_picture.rst`:
"VCCA should not exceed VCCB. This requires to switch the sides
of the level shifter depending if the VCC of the DUT is above or
below 3V3."). The new 74LVC1T45-based design eliminates this
solder-time decision because each LVC1T45 has independent VCCA / VCCB
supplies and a directional pin.

## A.5 Proposed ESP32-S3-WROOM-1-N16R8 GPIO assignment

Module pad availability check first. ESP32-S3-WROOM-1-N16R8 brings
out the following GPIOs on the module pads (per Espressif datasheet
Table 3 'Pin Definitions', cross-checked against the N16R8 octal
PSRAM/octal flash variant):

External GPIOs available: 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
13, 14, 15, 16, 17, 18, 19, 20, 21, 38, 39, 40, 41, 42, 43, 44, 45,
46, 47, 48. Total 33.

Internally consumed (NOT brought to module pads on N16R8):
- GPIO22-25: not exposed on WROOM-1 (these GPIOs do not exist on
  S3-WROOM-1; ESP32-S3 GPIO numbering jumps 21->26)
- GPIO26-32: SPI0 octal PSRAM (CS, CLK, D0-D7, DQS)
- GPIO33-37: octal flash pins (D4-D7 plus DQS); on N16R8 these are
  consumed internally by the flash and are not pinned externally

Reservations on the 33 external GPIOs:
- GPIO19 (USB D-), GPIO20 (USB D+): reserved for USB-OTG host
- GPIO43 (U0TXD), GPIO44 (U0RXD): reserved for CH340N console UART0
- GPIO0: strapping (boot mode select), also doubles as the chip
  BOOT button; avoid driving low at boot
- GPIO3: strapping (JTAG source select), drive at boot acceptable
  but must not be strong-pulled low if JTAG-on-pads is desired
- GPIO45: strapping (VDD_SPI voltage select), avoid low at boot
- GPIO46: strapping (boot ROM download print enable), avoid high at
  boot

After reservations, 33 - 4 (USB+UART) = 29 freely assignable, of
which 4 are strapping pins better used for static-direction or
low-criticality outputs.

### A.5.1 Proposed assignment

| S3 GPIO | Direction | Carrier pin / function                        | IO_MUX peripheral binding (preferred) | Notes                                                                          |
|--------:|-----------|-----------------------------------------------|---------------------------------------|--------------------------------------------------------------------------------|
| GPIO0   | strap     | BOOT button (onboard)                         | -                                     | leave alone, do not route to carrier                                           |
| GPIO1   | out       | RELAY_1 drive (carrier 17/18 contact pair)    | GPIO matrix                           | low-side opto drive; latency-insensitive                                       |
| GPIO2   | out       | RELAY_2 drive                                 | GPIO matrix                           |                                                                                |
| GPIO3   | strap/out | RELAY_3 drive                                 | GPIO matrix                           | strapping pin; default-low at boot is acceptable for relay drive               |
| GPIO4   | out       | RELAY_4 drive                                 | GPIO matrix                           |                                                                                |
| GPIO5   | out       | RELAY_5 drive                                 | GPIO matrix                           |                                                                                |
| GPIO6   | out       | RELAY_6 drive                                 | GPIO matrix                           |                                                                                |
| GPIO7   | out       | RELAY_7 drive                                 | GPIO matrix                           |                                                                                |
| GPIO8   | bidir     | LOCAL_I2C_SDA (INA228 + carrier-ID EEPROM)    | I2C0 SDA via IO_MUX                   | local 3v3 bus; pull-up on annealage_pod PCB; not through translators                |
| GPIO9   | out       | LOCAL_I2C_SCL                                 | I2C0 SCL via IO_MUX                   |                                                                                |
| GPIO10  | out       | SWCLK (carrier pin 7)                         | FSPI CLK / SPI2 CLK via IO_MUX        | direct IO_MUX path for max clock; spec §4.6 25-40 MHz                          |
| GPIO11  | bidir     | SWDIO data (carrier pin 8)                    | FSPI MOSI/D via IO_MUX (half-duplex)  | SPI2 D pin in 3-wire half-duplex; bidir through translator                     |
| GPIO12  | out       | SWDIO_DIR (translator DIR pin)                | GPIO matrix                           | toggled per SWD frame phase by the C `dapprobe` module                         |
| GPIO13  | in        | SWO (carrier pin 9)                           | UART1 RX via IO_MUX                   | UHCI/GDMA NRZ capture per spec §4.7                                            |
| GPIO14  | out OD    | nRST (carrier pin 10)                         | GPIO matrix                           | open-drain through fixed-direction translator                                  |
| GPIO15  | out       | DUT_UART_TX (carrier pin 11)                  | UART2 TX                              | fixed-direction translator                                                     |
| GPIO16  | in        | DUT_UART_RX (carrier pin 12)                  | UART2 RX                              | fixed-direction translator                                                     |
| GPIO17  | bidir     | DUT_I2C_SDA / SPI MOSI (carrier pin 13, mux)  | I2C1 SDA / GPSPI3 MOSI via IO_MUX     | DIR-controlled translator on SDA; in SPI mode acts as input MOSI               |
| GPIO18  | in        | DUT_I2C_SCL / SPI SCK (carrier pin 14, mux)   | I2C1 SCL / GPSPI3 SCK via IO_MUX      | always input on the S3 (slave), through fixed-in translator                    |
| GPIO19  | bidir     | DUT_USB_DP (carrier pin 5)                    | USB-OTG D+                            | fixed module pin                                                               |
| GPIO20  | bidir     | DUT_USB_DM (carrier pin 6)                    | USB-OTG D-                            | fixed module pin                                                               |
| GPIO21  | out       | DIR_DUT_I2C_SDA (translator DIR for pin 13)   | GPIO matrix                           | personality-arbitrated control                                                 |
| GPIO38  | out       | DUT_SPI_MISO (carrier pin 15, SPI-slave only) | GPSPI3 MISO via IO_MUX                | only driven when SPI-slave personality active; tristated otherwise             |
| GPIO39  | in        | DUT_SPI_CS (carrier pin 16, SPI-slave only)   | GPSPI3 CS via GPIO matrix             | only sampled when SPI-slave personality active                                 |
| GPIO40  | out       | VTARGET_EN                                    | GPIO matrix                           | drives TPS2595 EN/UVLO on VTARGET rail                                         |
| GPIO41  | out       | DUT_USB_VBUS_EN                               | GPIO matrix                           | drives TPS2595 EN/UVLO on DUT-USB rail                                         |
| GPIO42  | in (ADC)  | VBUS_SENSE                                    | ADC2_CH6                              | resistor-divided VBUS to ADC for early-DUT-up detection (spec §3.3)            |
| GPIO43  | out       | UART0 TX (CH340N)                             | UART0 TX (default)                    | reserved console; not on carrier                                               |
| GPIO44  | in        | UART0 RX (CH340N)                             | UART0 RX (default)                    | reserved console                                                               |
| GPIO45  | strap/out | LED_STATUS_1                                  | GPIO matrix                           | strapping pin; LED is a high-side default-off load, safe at boot               |
| GPIO46  | strap/out | LED_STATUS_2                                  | GPIO matrix                           | strapping pin; LED low-active, safe at boot if held low only after boot        |
| GPIO47  | bidir     | GPD0 (carrier pin 31, spare GP DUT IO)        | GPIO matrix                           | through DIR-strapped translator (per-test polarity selected on the new carrier)|
| GPIO48  | bidir     | GPD1 (carrier pin 32, spare GP DUT IO)        | GPIO matrix                           |                                                                                |

Total external S3 GPIOs used: 33. The carrier in §A.3 also has GPD6
and GPD7 (carrier pins 33, 34); see §A.6 for how those are handled.

## A.6 Pin budget verification

S3-WROOM-1-N16R8 external GPIOs: 33.
S3 GPIOs claimed in §A.5.1: 33.
Free: 0.

Carrier signals that need an S3 driver but are NOT yet pinned in
§A.5.1:

- GPD6 (carrier pin 33): no S3 GPIO assigned
- GPD7 (carrier pin 34): no S3 GPIO assigned
- VTARGET_SENSE / INA228 shunt analog: routed off the carrier at
  the INA228 chip itself, not driven from S3 directly; INA228 output
  reaches S3 via I2C on GPIO8/9. No S3 GPIO needed.
- CARRIER_ID_SDA / CARRIER_ID_SCL (carrier pins 35, 36): share the
  local I2C bus with INA228 on S3 GPIO8/9; no extra S3 GPIO needed.
  Carrier pins 35/36 connect physically to GPIO8/9 through the local
  I2C bus traces, NOT through translators.
- 7 RELAY drives: 7 pins claimed (GPIO1-7).

Shortfall: 2 GPIOs for GPD6 / GPD7.

### A.6.1 Resolution: drop GPD6 / GPD7 from rev1 carrier OR mux

Two options, neither free:

**Option A (preferred for rev1)**: drop GPD6 and GPD7 from the
carrier pin layout. Reuse pins 33 and 34 for an extra GND pair
and a second VTARGET pin (current capacity headroom). Net pin
budget closes at 31 driven S3 GPIOs + 2 USB + 2 UART0 = 33,
exactly matching the module.

**Option B**: keep GPD6 / GPD7 on the carrier and mux them with the
SPI-slave-only pins (carrier 15 MISO and 16 CS). When the SPI-slave
personality is inactive, MISO and CS are repurposed as GPD6 and GPD7
through a 74CBT3257-style 2:1 mux controlled by a personality-select
GPIO. Cost: 1 extra mux IC, 1 extra control signal. The control
signal can be DIR_DUT_I2C_SDA (GPIO21) reused as PERSONALITY_SEL
because the I2C-slave and SPI-slave personalities are already
mutually exclusive (spec §4.8).

I recommend Option A for rev1: the GPD6 / GPD7 v0.7 channels have
no documented firmware role in `referencea/annealage_pod/docs/rp2_probe.rst`
beyond logic-analyzer-channel placement, and the rev1 spec does not
list logic-analyzer functionality. Defer Option B mux to rev2 if
GPD6 / GPD7 are needed.

### A.6.2 Strapping pin audit

Strapping pins used in §A.5.1: GPIO0 (BOOT button, internal pull-up,
no carrier load), GPIO3 (RELAY_3 drive), GPIO45 (LED_STATUS_1),
GPIO46 (LED_STATUS_2).

Risk review:
- GPIO0: untouched at boot, no carrier connection; safe.
- GPIO3: drives an opto-relay LED through a 390R resistor. At boot
  the relay is off (low). GPIO3 strapping selects the JTAG source;
  JTAG-on-pads is irrelevant for production firmware that uses the
  USB-Serial/JTAG only when USB-OTG is in device mode (which the
  spec explicitly disables). Safe.
- GPIO45: LED with 2k2 series resistor to GND. At boot GPIO45 floats
  with internal pull-down, LED off. GPIO45 strapping selects VDD_SPI
  voltage; both states are SPI-flash-related and irrelevant on
  N16R8 (flash uses internal config). Safe.
- GPIO46: LED with 2k2 to GND. At boot GPIO46 floats with internal
  pull-down. Strapping enables ROM-download UART message printing,
  which is desirable during factory bringup, so GPIO46 stays low at
  boot. Safe.

All four strapping pins land on loads that do not fight the
default-state requirement at boot. No external pull-up / pull-down
modifications needed beyond the LED series resistors.

## A.7 Open questions / muxing decisions

1. **GPD6 / GPD7 fate**: Option A (drop) vs Option B (mux with
   SPI-slave) per §A.6.1. Recommendation: Option A for rev1.
2. **Translator family selection**: spec §3.2 says "74LVC1T45-style".
   The exact part for the bidirectional channels with separate DIR
   should be confirmed against current price/availability (74LVC1T45,
   SN74LVC2T45, NLSV2T244 candidates). One direction-strapped
   variant for fixed-direction lines (74LVC1G125 buffer) reduces
   cost on those channels.
3. **Translator power gating**: VTARGET-side translator B-rail
   should track VTARGET (3v3 fixed in rev1). In rev2 with
   programmable VTARGET, this rail also moves. Add a single point
   of B-rail collection on the schematic.
4. **VBUS_SENSE divider ratio**: with 5 V VBUS and a target 3v3-safe
   ADC pin, a 2:1 (e.g., 10k / 10k) divider is sufficient. ADC2 is
   used on GPIO42; ADC2 conflicts with Wi-Fi when Wi-Fi is active
   (ESP32-S3 erratum). Alternative: route VBUS_SENSE to an ADC1
   pin instead. Propose moving VBUS_SENSE to GPIO1 (ADC1_CH0) and
   migrating RELAY_1 elsewhere, OR routing VBUS_SENSE through the
   INA228 already monitoring DUT-USB rail (which gives both V and I
   over I2C; this may be the cleanest answer and lets GPIO42 be
   freed for a future signal). Decision deferred to rev1 PCB review.
5. **Strapping-pin LED choice**: confirm LED forward currents against
   strapping-pin drive limits. ESP32-S3 GPIOs deliver up to 40 mA;
   LED at 2 mA through 2k2 to GND is well inside spec.
6. **Carrier-ID I2C address conflict**: INA228 default 7-bit
   addresses are 0x40/0x41/0x44/0x45 (per ADDR pin strap). The
   carrier ID EEPROM (typical 24Cxx series) defaults to 0x50 - 0x57.
   No conflict expected. Confirm at hardware bringup.
7. **Personality-select mux IC**: if Option B is picked later, a
   single 74CBT3257 (quad 2:1 bus mux) covers SDA/SCL/MISO/CS in one
   package. Decision deferred.
8. **DIR control granularity for GPDx**: §A.3 marks GPD0/GPD1/GPD6/GPD7
   as 'DIR-strap (TBD)'. The new carrier could either solder-strap
   each GPD's DIR per-carrier (matching the v0.7 'flip the chip'
   pattern with TXB0108) or expose a runtime DIR pin for each. The
   four runtime DIR pins are not budgeted in §A.5.1 and would push
   the design into Option B territory. Recommendation: solder-strap
   per-carrier in rev1, since the v0.7 ergonomic precedent is
   solder-time direction selection.

## A.8 Citations to schematic page numbers

Schematic file: `referencea/annealage_pod/kicad/annealage_pod_v0.7/production_v0.7/schematics_annealage_pod_v0.7.pdf`
(KiCad 9.0.7, 14 pages, dated 2026-02-18, rev 0.7.1).

| Subject                                          | Page |
|--------------------------------------------------|-----:|
| Top-level sheet, USB hub overview                |    1 |
| RP_INFRA (U201 RP2040), J201 breakout, LEDs      |    2 |
| USB-C input, USB hub U302 (USB2514B)             |    3 |
| (Page 4 reserved by KiCad sheet ordering)        |    - |
| Relay breakout J501 mapping to RELAIS_1..7       |    5 |
| Opto-relay 1 detail (G3VM/GAQY221S, R601, R602)  |    6 |
| Opto-relay 2-7 detail (identical sub-sheets)     | 7-12 |
| Prototype area / 'Octobus' J1302 (DNP) + J1303   |   13 |
| RP_PROBE (U1501 RP2040), TXB0108 U1503/U1504, J1501 | 14 (PDF id 15/14) |
| Power: TPS2595 5V VBUS limiter U204, AMS1117 3v3 U202, RP_INFRA power U207, DUT 5V switch U205 | 16 (PDF id 16/14) |

KiCad schematic source files referenced:
- `pcb_annealage_pod.kicad_sch` (top-level)
- `pcb_annealage_pod_cpu.kicad_sch` (RP_INFRA)
- `pcb_annealage_pod_probe.kicad_sch` (RP_PROBE)
- `pcb_annealage_pod_relay_breakout.kicad_sch` (J501 map)
- `pcb_annealage_pod_opto_relay.kicad_sch` (per-relay subsheet, 7 instances)
- `pcb_annealage_pod_prototype_area.kicad_sch` (J1302 / J1303 octobus)
- `pcb_annealage_pod_regulator3V3.kicad_sch` (power)
- `pcb_annealage_pod_usbhubchip.kicad_sch` (USB hub)
- `production_v0.7/bom.csv` (component value confirmation)

Supporting documentation in this repository's reference area:
- `referencea/annealage_pod/docs/rp2_probe.rst`: RP_PROBE GPIO_PROBE_x to
  firmware-feature mapping for debugprobe / yapicoprobe / sigrok /
  ula / logicanalyzer firmwares
- `referencea/annealage_pod/docs/big_picture.rst`: TXB0108 VCCA/VCCB
  asymmetry rationale (justifies the 74LVC1T45 swap in spec §3.2)
- `referencea/annealage_pod/kicad/annealage_pod_v0.7/README.md`: v0.4 to v0.7
  history, notes the J201 silkscreen GPIO-number error
