# Annealage Pod: ops subpackage.
#
# WS-H surface (plan/phase-2-parallel-implementation.md):
#   ota:  esp_https_ota wrapper
#   wdt:  esp_task_wdt wrapper
#   log:  TCP socket dup of stdout
#   time: ntptime sync
#
# Submodules import lazily so an environment that lacks one (e.g.
# Unix port without esp_https_ota) does not break import of the
# others. Tests should `from annealage_pod.ops import wdt` etc. directly.

from . import ota, wdt, log, time

__all__ = ("ota", "wdt", "log", "time")
