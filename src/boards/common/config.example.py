# Template for the pod's network config. Copy to config.py on the pod
# filesystem and fill in real values. config.py is NOT frozen into firmware and
# is NOT committed (it holds credentials); netboot.py imports it at startup.
WIFI_SSID = "your-wifi-ssid"
WIFI_PASSWORD = "your-wifi-passphrase"
REPL_PORT = 8266
