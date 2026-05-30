include("$(PORT_DIR)/boards/manifest.py")

# Networking helpers (sockets, requests, ntptime) for the Wi-Fi management
# transport. The annealage_pod package is frozen here once it exists.
require("bundle-networking")

# Freeze the network boot bootstrap (Wi-Fi + os.dupterm socket REPL) so the pod
# comes up reachable over Wi-Fi with no USB-CDC REPL. config.py (credentials)
# stays on the filesystem, not frozen.
freeze("$(BOARD_DIR)", ("netboot.py", "main.py"))
