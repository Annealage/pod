# ESP32-S3 carrier code, quarantined from the live RP2350 surface.
#
# Everything in this subpackage belongs to the superseded ESP32-S3 pod design
# (see ../../../../docs/esp32-s3/): the carrier's four-path DUT reset, the
# opto-relay bank, the VTARGET/DUT-USB rails and INA228 telemetry, the carrier
# EEPROM identification, the C `slaveio` I2C/SPI personalities, the boot
# orchestration those services hang off, and the ESP32-S3 carrier pin map they
# all index.
#
# It is kept, not deleted, because plan/overview.md section 5 makes the package
# surface, the INA228 driver, the slave register-table model and the reset
# abstractions shared between the two variants; deleting them would discard the
# reference implementation the RP2350 side is meant to converge on.
#
# It lives behind its own name so that "shared" cannot quietly become "loaded by
# accident". These modules index `_pinmap`, the ESP32-S3 CARRIER pin map, whose
# GPIO numbers are meaningless on an RP2350 and in places actively dangerous
# there - the carrier's relay GPIOs 1-7 land on the RP2350 pod's DUT UART
# (GP4/GP5) and free pins, and the carrier's NRST of 14 is the RP2350's SWDIO.
# They also need carrier hardware (relays, rails, EEPROM) that a bare pod does
# not have, and C modules (`dapprobe`, `slaveio`) that the RP2350 firmware does
# not build.
#
# So: do not import this subpackage from RP2350 code paths. The live RP2350
# surface is annealage_pod.debug (SWD/DAP/flash/GDB/nRST/logic analyser),
# .peripherals, .spi_target and .uart_bridge, all of which index _rp2_pinmap.
# The RP2350 DUT reset lives at annealage_pod.debug.nrst, deliberately NOT in
# .esp32.dut, because it needs one wire and no carrier.
