# Frozen manifest for the ESP32_S3_ANNEALAGE_POD board variant.
#
# Picks up the standard MicroPython esp32 port modules (asyncio, network
# helpers, etc.) and additionally freezes the annealage_pod Python package
# from src/mpy/.

include("$(PORT_DIR)/boards/manifest.py")

# Freeze the annealage_pod package living at src/mpy/annealage_pod/. The board
# variant directory is src/boards/ESP32_S3_ANNEALAGE_POD/, so the package
# root is two levels up plus mpy/.
#
# `package` walks the directory tree and freezes every .py file under
# annealage_pod/, which currently includes:
#   annealage_pod/__init__.py
#   annealage_pod/_pinmap.py
#   annealage_pod/_ina228.py
#   annealage_pod/_version.py
#   annealage_pod/boot.py
#   annealage_pod/carrier.py
#   annealage_pod/compat.py
#   annealage_pod/dut.py
#   annealage_pod/power.py
#   annealage_pod/relays.py
#   annealage_pod/slave.py
#   annealage_pod/supervisor.py
#   annealage_pod/ops/__init__.py
#   annealage_pod/ops/log.py
#   annealage_pod/ops/ota.py
#   annealage_pod/ops/time.py
#   annealage_pod/ops/wdt.py
#
# credentials.example.json is shipped as a source-tree template and is
# NOT frozen; users provide their real /credentials.json on the
# device's vfs partition.
package(
    "annealage_pod",
    base_path="$(BOARD_DIR)/../../mpy",
)

# Top-level frozen main.py runs annealage_pod.boot.up() on every power-up.
freeze("$(BOARD_DIR)/../../mpy", "main.py")
