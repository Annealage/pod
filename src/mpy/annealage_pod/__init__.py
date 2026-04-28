# Annealage Pod MicroPython package.
#
# Phase 1: just exposes a version string. Real subpackages (boot,
# power, dut, slave, compat) are stubs that arrive in Phase 2.

from ._version import __version__

__all__ = ("__version__",)
