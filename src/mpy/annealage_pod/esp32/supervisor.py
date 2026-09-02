# Annealage Pod: supervisor and cleanup-hook lifecycle.
#
# Per spec.md §5.4 the annealage_pod lifecycle is hybrid: services start at
# boot and run forever; on REPL TCP disconnect a cleanup hook fires.
# Default behaviour: DUT power off, level translators tristated, and
# in-flight queues drained. Test scripts can replace the hook.
#
# The supervisor maintains a list of cleanup callables. The default
# hook is registered at import time. Boot orchestration (boot.py)
# wires the REPL disconnect handler to call run_cleanup().


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


# Register the default hook at import time. User code can swap it
# out via clear_hooks() + register_cleanup(custom).
register_cleanup(_default_cleanup)
