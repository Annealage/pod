"""Host-side locks for resources reached without a pod.

A pod records who holds its USB/IP link and REPL. A probe or serial device
attached to this host has no such record, so each is guarded by an advisory
``flock`` keyed on the device, holding a small JSON holder record. Locks are
per host: they stop two agent processes on this machine colliding, and say who
holds the device when they do.
"""

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path


def _lock_dir() -> Path:
    env = os.environ.get("ANNEALAGE_POD_LOCK_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(xdg) if xdg else Path("/tmp/annealage-pod-%d" % os.getuid())
    return base / "annealage-pod-locks"


def _lock_path(key: str) -> Path:
    return _lock_dir() / (re.sub(r"[^A-Za-z0-9_.-]+", "_", key) + ".lock")


def read_holder(key: str) -> dict:
    """The holder record last written for a key ({} if none)."""
    try:
        return json.loads(_lock_path(key).read_text() or "{}")
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def hold(key: str, caller: str, force: bool = False):
    """Hold the lock for `key` for the duration of the block.

    Raises PodConflictError naming the holder when another process holds it.
    ``force=True`` proceeds without the lock instead of waiting for it.
    """
    from pod.client import PodConflictError

    path = _lock_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            if not force:
                raise PodConflictError(key, read_holder(key), caller)
        if locked:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"caller": caller, "pid": os.getpid(),
                                     "since": time.time()}).encode())
        yield
    finally:
        if locked:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
