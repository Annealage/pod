# Annealage Pod: supervisor and cleanup-hook lifecycle.
#
# Per spec.md §5.4 the annealage_pod lifecycle is hybrid: services start at
# boot and run forever; boot orchestration can wire a REPL-disconnect handler
# to call run_cleanup(). Whether that fires, and what it does, is a per-target
# choice: the pod is a bench multiple agents share in turn, and a hook that
# powers off DUT rails on every disconnect (a lone-user assumption) would
# power-cycle whichever agent is mid-session the moment any other agent's
# call disconnects. _default_cleanup captures that lone-user behaviour
# (DUT power off, relays open) but is not registered by default; a target
# that wants it opts in with register_cleanup(_default_cleanup).
#
# The supervisor maintains a list of cleanup callables, empty until something
# registers one.


from . import power, relays


_hooks = []


def register_cleanup(callback):
    """Register a callable to be invoked on cleanup. Returns the callback."""
    if callback not in _hooks:
        _hooks.append(callback)
    return callback


def unregister_cleanup(callback):
    """Remove a previously-registered cleanup callable. Silent if absent."""
    try:
        _hooks.remove(callback)
    except ValueError:
        pass


def list_hooks():
    """Return the current ordered list of cleanup hooks."""
    return list(_hooks)


def clear_hooks():
    """Drop every registered cleanup hook. Used by tests."""
    _hooks[:] = []


def run_cleanup():
    """Invoke every registered cleanup hook in registration order.

    Hook exceptions are caught, printed, and the run continues so a
    misbehaving hook cannot strand later hooks.
    """
    for hook in list(_hooks):
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 - run-cleanup must not raise
            print("annealage_pod.supervisor: cleanup hook {!r} raised {!r}".format(hook, exc))


def _default_cleanup():
    """Default cleanup: power off both DUT rails and open every relay."""
    try:
        power.dut_usb.off()
    except Exception as exc:  # noqa: BLE001
        print("annealage_pod.supervisor: dut_usb.off() raised {!r}".format(exc))
    try:
        power.vtarget.off()
    except Exception as exc:  # noqa: BLE001
        print("annealage_pod.supervisor: vtarget.off() raised {!r}".format(exc))
    try:
        relays.relays.all_off()
    except Exception as exc:  # noqa: BLE001
        print("annealage_pod.supervisor: relays.all_off() raised {!r}".format(exc))


# Not registered by default (see the module header): a target opts in with
# register_cleanup(_default_cleanup) if a lone-user power-off-on-disconnect
# is the behaviour it wants.
