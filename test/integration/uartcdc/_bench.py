"""Shared helpers for uartcdc integration tests."""

import glob
import os
import re
import socket as _socket
import subprocess
import sys
import time

ANNEALAGE_POD_HOSTNAME = os.environ.get("ANNEALAGE_POD_HOST", "annealage-pod-dabao.local")
_ANNEALAGE_POD_IP_FALLBACK = os.environ.get("USBIPD_IP", "192.168.0.166")
UARTCDC_VID_PID = "c251:f00b"
VHCI_DETACH_PATH = "/sys/devices/platform/vhci_hcd.0/detach"


def _resolve_host():
    """Return ANNEALAGE_POD_HOSTNAME if it resolves via mDNS/DNS, else _ANNEALAGE_POD_IP_FALLBACK."""
    try:
        _socket.getaddrinfo(ANNEALAGE_POD_HOSTNAME, None, _socket.AF_INET)
        return ANNEALAGE_POD_HOSTNAME
    except _socket.gaierror:
        return _ANNEALAGE_POD_IP_FALLBACK


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def annealage_pod_tty():
    tty = os.environ.get("ANNEALAGE_POD_TTY")
    if tty:
        return tty
    r = _run(["mpy-dev", "tty", "esp32-s3"])
    if r.returncode != 0:
        raise RuntimeError(f"mpy-dev tty esp32-s3 failed: {r.stderr.strip()}")
    return r.stdout.strip()


def find_uartcdc_busid(ip=None):
    """Parse `usbip list -r <host>` and return the busid for c251:f00b."""
    ip = ip or _resolve_host()
    r = _run(["usbip", "list", "-r", ip])
    if r.returncode != 0:
        raise RuntimeError(f"usbip list failed: {r.stderr.strip()}")
    for line in r.stdout.splitlines():
        if UARTCDC_VID_PID in line:
            m = re.match(r"\s+(\d+-\d+):", line)
            if m:
                return m.group(1)
    raise RuntimeError(
        f"uartcdc {UARTCDC_VID_PID} not found in usbip list output from {ip}"
    )


def find_attached_uartcdc_port():
    """Return the vhci port number (int) of the currently attached uartcdc, or None."""
    r = _run(["usbip", "port"])
    lines = r.stdout.splitlines()
    for i, line in enumerate(lines):
        if UARTCDC_VID_PID in line:
            for j in range(i - 1, -1, -1):
                m = re.match(r"Port (\d+):", lines[j].strip())
                if m:
                    return int(m.group(1))
    return None


def usbip_attach(busid, ip=None):
    ip = ip or _resolve_host()
    r = _run(["sudo", "usbip", "attach", "-r", ip, "-b", busid])
    if r.returncode != 0:
        raise RuntimeError(
            f"usbip attach -r {ip} -b {busid} failed: {r.stderr.strip()}"
        )


def usbip_detach_port(port_num):
    _run(["sudo", "bash", "-c", f"echo {port_num} > {VHCI_DETACH_PATH}"])
    time.sleep(0.5)


def wait_for_cdc_uart(timeout_s=10):
    """Wait for /dev/serial/by-id/usb-mpy-pod_mpy-pod_CDC_UART_* to appear."""
    pattern = "/dev/serial/by-id/usb-mpy-pod_mpy-pod_CDC_UART_*"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
        time.sleep(0.1)
    raise RuntimeError(f"CDC_UART device did not appear within {timeout_s}s")


def dut_reset_via_annealage_pod():
    """Pulse GPIO47 LOW then release to IN (hi-Z, pull-up takes HIGH) to reset the DUT."""
    tty = annealage_pod_tty()
    r = _run(
        [
            "mpremote", "connect", tty, "resume", "exec",
            "from machine import Pin; import time; "
            "p=Pin(47,Pin.OUT,value=0); time.sleep_ms(50); p.init(Pin.IN)",
        ],
        timeout=10,
    )
    if r.returncode != 0:
        raise RuntimeError(f"DUT reset via annealage_pod failed: {r.stderr.strip()}")


def mpremote(*args, tty=None, timeout=30):
    base = ["mpremote"]
    if tty:
        base += ["connect", tty]
    base += ["resume"] + list(args)
    return _run(base, timeout=timeout)


class AttachedUartCDC:
    """Context manager: detach any existing uartcdc attachment, attach fresh,
    yield CDC port path, detach on exit."""

    def __init__(self, ip=None):
        self.ip = ip or _resolve_host()
        self._port_num = None
        self.cdc_path = None

    def __enter__(self):
        existing = find_attached_uartcdc_port()
        if existing is not None:
            usbip_detach_port(existing)
            time.sleep(1)

        busid = find_uartcdc_busid(self.ip)
        usbip_attach(busid, self.ip)
        self.cdc_path = wait_for_cdc_uart()
        self._port_num = find_attached_uartcdc_port()
        return self

    def __exit__(self, *_):
        if self._port_num is not None:
            usbip_detach_port(self._port_num)
        return False


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def passed(msg=""):
    print(f"PASS{': ' + msg if msg else ''}")
    sys.exit(0)
