# Frozen startup for the Annealage Pod RP2350: bring up the Wi-Fi REPL so the
# pod is reachable over the network (the native USB port is the DUT host, so
# there is no USB-CDC REPL). Failures are caught so a bad config does not stop
# the board reaching the REPL via other means during development.
try:
    import netboot
    netboot.start()
except Exception as e:
    import sys
    sys.print_exception(e)
