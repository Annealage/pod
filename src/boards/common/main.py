# Frozen startup for the Annealage Pod RP2350: run the single-core asyncio
# management runtime (Wi-Fi REPL + supervisor) on core0. The native USB port is
# the DUT host, so there is no USB-CDC REPL; asyncio.arepl serves the REPL over
# UART and the Wi-Fi socket. asyncio.run() blocks here for the life of the pod;
# if it ever returns (stdin closed), control falls through to the normal UART
# REPL. Failures are caught so a bad config can still reach the REPL.
try:
    import asyncio
    import netboot

    asyncio.run(netboot.main())
except Exception as e:
    import sys

    sys.print_exception(e)
