# Annealage Pod MicroPython package.
#
# Surface on the RP2350 pod. All of it indexes
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
#
# Submodules are imported on demand by callers so that Unix-port unit
# tests for one subsystem don't drag in machine-bound code from another.

from ._version import __version__

__all__ = ("__version__",)
