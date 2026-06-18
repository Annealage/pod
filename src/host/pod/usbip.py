"""Host-side USB/IP attach for a pod-exported DUT.

The pod exports the DUT's native USB over USB/IP (the C lwIP-RAW server, default
TCP 3240). This drives the standard `usbip` client to list / attach / detach that
device on the host, so an agent reaches the DUT's own USB (e.g. its CDC REPL)
without a hand-run usbip dance - and `usbip list -r` is also where the live DUT
VID:PID comes from (the pod has no separate descriptor read).

Requirements and caveats:
  - host `usbip` tooling + the vhci_hcd kernel module;
  - attach/detach write to vhci sysfs and need root, so they run under `sudo -n`
    (configure passwordless sudo for usbip, or run `pod attach` in a shell);
  - ensure_server() activates the pod's native USB controller in host mode.
    NOTE: bringing USB host up after boot has been observed to disturb the pod's
    Wi-Fi link (the pod's only management channel) - call it deliberately.

Parsing (parse_usbip_list / tty matching) is pure so it is unit-tested offline.
"""

import glob
import os
import re
import subprocess
import time


USBIP_PORT = 3240

# A `usbip list -r` device line, e.g.
#   "        1-1: unknown vendor : unknown product (f055:9802)"
_LIST_RE = re.compile(
    r"^\s*([0-9][\w.\-]*):\s.*\(([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\)")


def _run(argv, sudo=False, timeout=20, runner=None):
    runner = runner or subprocess.run
    # attach/detach write vhci sysfs and normally need root, so they run under
    # `sudo -n`. Set POD_USBIP_SUDO=0 when the host is configured for sudo-less
    # usbip (a udev rule making the vhci sysfs group-writable, or running as
    # root) so the bare command is used instead. See src/host/README.md.
    if sudo and os.environ.get("POD_USBIP_SUDO", "1") != "0":
        argv = ["sudo", "-n"] + argv
    return runner(argv, capture_output=True, text=True, timeout=timeout)


def parse_usbip_list(text):
    """Parse `usbip list -r` stdout into [{busid, vid, pid}] (pure)."""
    devs = []
    for line in (text or "").splitlines():
        m = _LIST_RE.match(line)
        if m:
            devs.append({"busid": m.group(1),
                         "vid": m.group(2).lower(),
                         "pid": m.group(3).lower()})
    return devs


def list_remote(host, runner=None):
    """Devices the pod's usbip server exports (live VID:PID + busid)."""
    out = _run(["usbip", "list", "-r", host], runner=runner)
    if out.returncode != 0:
        raise RuntimeError("usbip list -r %s failed: %s"
                           % (host, (out.stderr or out.stdout).strip()))
    return parse_usbip_list(out.stdout)


def attach(host, busid, runner=None):
    """Attach an exported device (needs root via sudo -n)."""
    out = _run(["usbip", "attach", "-r", host, "-b", busid], sudo=True,
               runner=runner)
    if out.returncode != 0:
        err = (out.stderr or out.stdout).strip()
        raise RuntimeError(
            "usbip attach -r %s -b %s failed: %s (needs passwordless sudo for "
            "usbip, or run in a shell)" % (host, busid, err))
    return True


def detach(port, runner=None):
    """Detach a vhci port (the small integer from `usbip port`)."""
    out = _run(["usbip", "detach", "-p", str(port)], sudo=True, runner=runner)
    if out.returncode != 0:
        raise RuntimeError("usbip detach -p %s failed: %s"
                           % (port, (out.stderr or out.stdout).strip()))
    return True


def serial_devices():
    """Current CDC serial device paths (by-id symlinks + raw ttyACM)."""
    return set(glob.glob("/dev/serial/by-id/*")) | set(glob.glob("/dev/ttyACM*"))


def pick_new_tty(before, after):
    """Choose the device that appeared between two serial_devices() sets (pure).

    Prefers a stable /dev/serial/by-id path over a raw /dev/ttyACM node. Matching
    by appearance avoids the by-id naming problem (those names carry
    manufacturer/serial, not the VID:PID).
    """
    new = sorted(after - before)
    if not new:
        return None
    by_id = [p for p in new if "/by-id/" in p]
    return by_id[0] if by_id else new[0]


def wait_for_new_tty(before, timeout=8.0, _sleep=time.sleep, _list=serial_devices):
    """Poll until a new serial device appears (vs `before`); return its path or None."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hit = pick_new_tty(before, _list())
        if hit:
            return os.path.realpath(hit) if os.path.exists(hit) else hit
        _sleep(0.25)
    return None


def ensure_server(pod, settle=6.0, _sleep=time.sleep):
    """Bring the pod's USB host + usbip server up over the REPL, in the right order.

    The export table is seeded exactly once, inside usbip.start()'s rescan of
    mounted devices, so the DUT must already be enumerated when start() runs.
    TinyUSB enumeration is pumped by the pod's asyncio idle poll, which only runs
    BETWEEN execs - a blocking sleep inside a single exec would stall the
    single-core loop and prevent enumeration. So this activates the host, waits
    host-side for enumeration to settle, THEN starts usbip. The DUT must be
    physically present and powered before activation.

    RISK: machine.USBHost().active(True) drives the native USB controller into
    host mode. It coexists with the Wi-Fi REPL on the dual-stack firmware, but it
    is a deliberate action - the REPL is the pod's only management channel.

    NB: the seed is one-shot per boot - if usbip was already started earlier with
    an empty table (e.g. a prior wrong-order bring-up), start() here will not
    re-seed and a reboot is needed. The firmware re-seeds on every start() to
    avoid this; see usbhost_start().
    """
    pod.exec("import machine; machine.USBHost().active(True)")
    _sleep(settle)
    return pod.exec(
        "import usbip; usbip.start(); print('usbip-running', usbip.is_running())")
