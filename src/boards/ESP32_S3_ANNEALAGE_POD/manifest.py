# Frozen manifest for the ESP32_S3_ANNEALAGE_POD board variant.
#
# Picks up the standard MicroPython esp32 port modules (asyncio, network
# helpers, etc.) and additionally freezes the annealage_pod Python package
# from src/mpy/.

include("$(PORT_DIR)/boards/manifest.py")

# Freeze the annealage_pod package living at src/mpy/annealage_pod/. The board
# variant directory is src/boards/ESP32_S3_ANNEALAGE_POD/, so the package
# root is two levels up plus mpy/.
package(
    "annealage_pod",
    base_path="$(BOARD_DIR)/../../mpy",
)
