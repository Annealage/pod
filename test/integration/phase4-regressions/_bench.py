"""Shared helpers for the phase-4 regression bench.

Each test_*.py imports from here. Keeps the attach/detach/find-tty
plumbing out of the test scripts themselves so the test bodies stay
focused on the regression they're pinning.
"""

import glob
import os
import socket as _socket
import subprocess
import sys
import time

DEFAULT_USBIPD_IP = "192.168.0.182"
DEFAULT_BUSID = "1-1"
VHCI_DETACH_PATH = "/sys/devices/platform/vhci_hcd.0/detach"


def env_ip():
    """Return the annealage_pod host to connect to.

    Resolution order:
    1. ANNEALAGE_POD_HOST env var - tried first (supports mDNS names).
    2. USBIPD_IP env var - used if ANNEALAGE_POD_HOST does not resolve.
    3. DEFAULT_USBIPD_IP hardcoded fallback.
    """
    hostname = os.environ.get("ANNEALAGE_POD_HOST", "")
    fallback = os.environ.get("USBIPD_IP", DEFAULT_USBIPD_IP)
    if hostname:
        try:
            _socket.getaddrinfo(hostname, None, _socket.AF_INET)
            return hostname
        except _socket.gaierror:
            pass
    return fallback


def env_busid():
    return os.environ.get("USBIPD_BUSID", DEFAULT_BUSID)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def usbip_list(ip=None):
    """Return raw stdout of `usbip list -r <ip>`. Caller parses."""
    r = _run(["usbip", "list", "-r", ip or env_ip()])
    if r.returncode != 0:
        raise RuntimeError(f"usbip list failed: {r.stderr.strip()}")
    return r.stdout


def usbip_attach(ip=None, busid=None):
    ip = ip or env_ip()
    busid = busid or env_busid()
    r = _run(["sudo", "usbip", "attach", "-r", ip, "-b", busid])
    if r.returncode != 0:
        raise RuntimeError(
            f"usbip attach -r {ip} -b {busid} failed: {r.stderr.strip()}"
        )


def usbip_detach_all():
    """Detach all vhci_hcd ports via sysfs. usbip detach -p N is
    fragile because it depends on a state file that can go stale.
    The sysfs write is the canonical way to drop a single port."""
    if not os.path.exists(VHCI_DETACH_PATH):
        return
    # Detach port 0 (the only one we attach in these tests).
    r = _run(["sudo", "bash", "-c", f"echo 0 > {VHCI_DETACH_PATH}"])
    if r.returncode != 0:
        # Already detached or never attached is fine.
        pass


def wait_for_ttyACM(timeout_s=10):
    """Wait for a /dev/ttyACM* node to appear after usbip attach.
    Returns the path of the first match."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        nodes = sorted(glob.glob("/dev/ttyACM*"))
        if nodes:
            return nodes[-1]
        time.sleep(0.1)
    raise RuntimeError(f"no /dev/ttyACM* appeared within {timeout_s}s")


def mpremote(*args, tty=None, timeout=30):
    """Run mpremote against tty (or whatever it picks up), return
    CompletedProcess. resume is used to avoid soft-resetting the
    asyncio aiorepl on the DUT side."""
    base = ["mpremote"]
    if tty:
        base += ["connect", tty]
    base += ["resume"] + list(args)
    return _run(base, timeout=timeout)


class AttachedDUT:
    """Context manager: attach over USB/IP, wait for /dev/ttyACM*,
    detach on exit. Raises on attach failure; detach errors are
    swallowed (best-effort cleanup)."""

    def __init__(self, ip=None, busid=None, wait_s=10):
        self.ip = ip
        self.busid = busid
        self.wait_s = wait_s
        self.tty = None

    def __enter__(self):
        usbip_attach(self.ip, self.busid)
        self.tty = wait_for_ttyACM(self.wait_s)
        return self

    def __exit__(self, exc_type, exc, tb):
        usbip_detach_all()
        return False  # don't suppress


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def passed(msg=""):
    print(f"PASS{': ' + msg if msg else ''}")
    sys.exit(0)
