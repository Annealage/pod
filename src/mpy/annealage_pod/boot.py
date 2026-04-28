# Annealage Pod boot module.
#
# Phase 1 entry point. Just prints a banner so the smoke flash can
# observe a successful import + call. Phase 2 will replace this with
# Wi-Fi bring-up, mDNS announcement, REPL os.dupterm() onto a TCP
# listener, and supervisor cleanup hook registration.


def up():
    """Phase 1 smoke entry point. Returns None."""
    print("annealage_pod skeleton up")


# Allow `import annealage_pod.boot` to print the banner immediately, so
# auto-boot wiring in later phases lands cleanly without forcing every
# caller to remember up().
if __name__ == "__main__":
    up()
