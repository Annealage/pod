# Unit tests for annealage_pod.supervisor cleanup hooks.

import importlib

from annealage_pod import supervisor


def test_nothing_registered_on_import():
    # The pod is a bench multiple agents share in turn; a hook that runs
    # unconditionally on every REPL disconnect (_default_cleanup's lone-user
    # power-off) would power-cycle whichever agent is mid-session the moment
    # any other agent's call disconnects. Nothing auto-registers: a target
    # opts in explicitly with register_cleanup(_default_cleanup).
    importlib.reload(supervisor)
    assert supervisor.list_hooks() == []


def test_register_unregister_clear():
    supervisor.clear_hooks()
    log = []

    def hook_a():
        log.append("a")

    def hook_b():
        log.append("b")

    supervisor.register_cleanup(hook_a)
    supervisor.register_cleanup(hook_b)
    assert hook_a in supervisor.list_hooks()
    assert hook_b in supervisor.list_hooks()

    supervisor.unregister_cleanup(hook_a)
    assert hook_a not in supervisor.list_hooks()
    assert hook_b in supervisor.list_hooks()

    supervisor.clear_hooks()
    assert supervisor.list_hooks() == []


def test_run_cleanup_invokes_in_order():
    supervisor.clear_hooks()
    log = []
    supervisor.register_cleanup(lambda: log.append(1))
    supervisor.register_cleanup(lambda: log.append(2))
    supervisor.register_cleanup(lambda: log.append(3))
    supervisor.run_cleanup()
    assert log == [1, 2, 3]


def test_run_cleanup_swallows_exceptions():
    supervisor.clear_hooks()
    log = []

    def boom():
        raise RuntimeError("kaboom")

    supervisor.register_cleanup(boom)
    supervisor.register_cleanup(lambda: log.append("ran"))
    supervisor.run_cleanup()
    # Second hook still ran despite the first raising.
    assert log == ["ran"]


def test_default_cleanup_does_not_raise():
    """Default cleanup invokes power.off and relays.all_off; should never raise."""
    supervisor.clear_hooks()
    supervisor.register_cleanup(supervisor._default_cleanup)
    # Should print but not raise.
    supervisor.run_cleanup()
