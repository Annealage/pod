# Annealage Pod MicroPython package.
#
# Public surface (per docs/esp32-s3/spec.md §7 and the WS-E entry of
# plan/phase-2-parallel-implementation.md):
#   - annealage_pod.boot        boot orchestration
#   - annealage_pod.power       VTARGET / DUT-USB rails + INA228 telemetry
#   - annealage_pod.relays      opto-coupled relays 1..7
#   - annealage_pod.dut         four reset paths (swd / nrst / power / relay)
#   - annealage_pod.slave       I2C/SPI slave personality wrappers
#   - annealage_pod.carrier     carrier identification (EEPROM / strap)
#   - annealage_pod.supervisor  cleanup-hook lifecycle
#   - annealage_pod.compat      RP_INFRA-equivalent shim (Appendix B)
#   - annealage_pod.ops         OTA / WDT / log / time
#
# Submodules are imported on demand by callers so that Unix-port unit
# tests for one subsystem don't drag in machine-bound code from another.

from ._version import __version__

__all__ = ("__version__",)
