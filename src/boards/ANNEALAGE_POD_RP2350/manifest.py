include("$(PORT_DIR)/boards/manifest.py")

# Networking helpers (sockets, requests, ntptime) for the Wi-Fi management
# transport. The annealage_pod package is frozen here once it exists.
require("bundle-networking")

# Freeze the network boot bootstrap (Wi-Fi + os.dupterm socket REPL) so the pod
# comes up reachable over Wi-Fi with no USB-CDC REPL. config.py (credentials)
# stays on the filesystem, not frozen. These two files are board-agnostic and
# shared with the other pod boards from src/boards/common.
freeze("$(BOARD_DIR)/../common", ("netboot.py", "main.py"))

# USB/IP forwarder C modules: the on-pod raw-URB backend (usbhost) and the
# lwIP-RAW USB/IP server (usbip). Pulled in via the manifest c_module() directive
# (micropython PR #18229); each directory carries its own micropython.cmake, which
# selects the rp2 (*_rp2.c) sources. usbhost is listed first since usbip links
# against it.
c_module("$(BOARD_DIR)/../../c_modules/usbhost")
c_module("$(BOARD_DIR)/../../c_modules/usbip")
