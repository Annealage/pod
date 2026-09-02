# Annealage Pod MicroPython package.
#
# Live surface on the RP2350 pod (the canonical target). All of it indexes
# _rp2_pinmap, the single source of truth for the pod's DUT-facing GPIO:
#   - annealage_pod.debug       SWD/DAP debug stack: swd_pio / swd_dap, the
#                               nRF52 + generic CMSIS-FLM flashers, the GDB RSP
#                               server, the PIO logic analyser, the nRST reset
#                               line (debug.nrst), and `ops`, the entry points
#                               the host `pod` tool drives over the REPL
#   - annealage_pod.peripherals curated machine.* helpers (hardware I2C target,
#                               GPIO, ADC) for functional tests
#   - annealage_pod.spi_target  PIO SPI-target personality (register table)
#   - annealage_pod.uart_bridge DUT UART forwarded over TCP
#   - annealage_pod.ops         OTA / WDT / log / time
#
# annealage_pod.esp32 is the superseded ESP32-S3 carrier code (boot / power /
# relays / dut / slave / carrier / compat / supervisor and the ESP32-S3 carrier
# pin map). It is retained for the code-sharing strategy in plan/overview.md
# section 5, and quarantined because its pin numbers and hardware assumptions do
# not hold on an RP2350. Do not import it from RP2350 code paths; see the note
# in annealage_pod/esp32/__init__.py.
#
# Submodules are imported on demand by callers so that Unix-port unit
# tests for one subsystem don't drag in machine-bound code from another.

from ._version import __version__

__all__ = ("__version__",)
