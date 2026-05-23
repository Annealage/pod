#!/usr/bin/env python3
"""POD connection helper: keep a annealage_pod USB/IP DUT attached across resets.

Monitors a annealage_pod USB/IP server and automatically re-attaches a DUT
by VID:PID whenever it disconnects (e.g. after DUT reset or bootloader entry).

Usage:
    python3 src/tools/pod-connect.py [--host HOST] [--vidpid VID:PID[,...]]
                                      [--poll-waiting MS] [--poll-attached MS]
                                      [--mode {auto,hub,poll}] [--hub-vidpid VID:PID]
                                      [--once]

Environment:
    ANNEALAGE_POD_HOST   mDNS hostname to try first (default: annealage_pod-dabao.local)
    USBIPD_IP       IP fallback if ANNEALAGE_POD_HOST does not resolve (default: 192.168.0.166)

VID:PID notes:
    Pass the VID:PID of the device as it appears on the USB/IP server.
    The DUT's bootloader often has a different VID:PID than the runtime
    firmware. For example, RP2040 BOOTSEL = 2e8a:0003, RP2 bootloader =
    2e8a:000f. To cover both states pass:
        --vidpid c251:f00b,2e8a:0003,2e8a:000f

Hub mode (preferred, requires python-libusb1):
    The annealage_pod exposes a notification device at VID:PID c251:f00c. When
    python-libusb1 is installed and the user has access to the device
    (install src/tools/99-annealage-pod-hub.rules under /etc/udev/rules.d/),
    pod-connect uses an interrupt endpoint on that device for instant
    DUT state notification, achieving sub-100ms reconnect latency.

    Install libusb1:        pip install --user libusb1
                            OR: apt install python3-libusb1
    Install udev rule:      sudo cp src/tools/99-annealage-pod-hub.rules /etc/udev/rules.d/
                            sudo udevadm control --reload && sudo udevadm trigger
    Force polling mode:     --mode poll

    Recovery: if hub mode fails mid-session, pod-connect falls back to the
    polling loop for the remainder of the session. Restart the script to
    retry hub mode after fixing the underlying issue (e.g. annealage_pod reboot).

    Note: Ctrl-C may take up to 2 seconds to take effect in hub mode while
    blocked on an interrupt-IN read. The script will still cleanly detach
    the DUT and release libusb resources before exiting.
"""

import argparse
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time

try:
    import usb1
    HAVE_LIBUSB = True
except ImportError:
    HAVE_LIBUSB = False

# State machine:
#   WAITING  - poll `usbip list -r HOST` every poll_waiting ms; when a matching
#              VID:PID appears, run `sudo usbip attach` and move to ATTACHED.
#   ATTACHED - poll `usbip port` every poll_attached ms; when the VID:PID is no
#              longer present the vhci has already torn down, so move to WAITING.

VHCI_DETACH_PATH = "/sys/devices/platform/vhci_hcd.0/detach"
_DEFAULT_HOST = os.environ.get("ANNEALAGE_POD_HOST", "annealage-pod-dabao.local")
_DEFAULT_IP_FALLBACK = os.environ.get("USBIPD_IP", "192.168.0.166")
_DEFAULT_VIDPID = "c251:f00b"
_DEFAULT_HUB_VIDPID = "c251:f00c"

# Hub mode tuning.
# Firmware blocks up to 1s in-device waiting for events before returning ZLP;
# libusb timeout must comfortably exceed that to avoid spurious timeouts on
# every idle cycle.
HUB_LIBUSB_TIMEOUT_MS = 2000
# Short non-blocking drain after open to flush any stale events from the
# firmware's 4-slot ring left over from a previous session.
HUB_DRAIN_TIMEOUT_MS = 100
# Reconcile event-driven state against ground truth every N consecutive ZLPs
# (~20s wall time at the 2s timeout) in case an event was missed.
HUB_RECONCILE_EVERY = 10
# Maximum consecutive transient libusb errors before giving up on hub mode and
# falling back to polling.
HUB_MAX_TRANSIENT = 10

# Flag set by signal handler; main loop checks this to perform clean shutdown.
_shutdown = False


def on_signal(signum, _frame):
    global _shutdown
    _shutdown = True


def resolve_host(hostname):
    """Return hostname if it resolves via DNS/mDNS, else the IP fallback."""
    try:
        socket.getaddrinfo(hostname, None, socket.AF_INET)
        return hostname
    except socket.gaierror:
        logging.warning("cannot resolve %s, falling back to %s", hostname, _DEFAULT_IP_FALLBACK)
        return _DEFAULT_IP_FALLBACK


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        logging.warning("command timed out: %s", cmd)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="timeout")


def _vp_matches(line, vidpids):
    """Return first VID:PID from vidpids that matches line with word boundaries, or None."""
    line_lc = line.lower()
    for vp in vidpids:
        if re.search(r"(?<![0-9a-f])" + re.escape(vp.lower()) + r"(?![0-9a-f])", line_lc):
            return vp
    return None


def list_remote(host):
    """Run `usbip list -r HOST` and return (stdout, error_string_or_None)."""
    r = _run(["usbip", "list", "-r", host])
    if r.returncode != 0:
        return None, r.stderr.strip() or "non-zero exit"
    return r.stdout, None


def find_busid(listing, vidpids):
    """Return the first busid in listing whose line contains any VID:PID in vidpids."""
    for line in listing.splitlines():
        vp = _vp_matches(line, vidpids)
        if vp:
            m = re.match(r"\s+(\d+-\d+):", line)
            if m:
                return m.group(1), vp
    return None, None


def attached_port(vidpids):
    """Return (port_num, matched_vidpid) of the first vhci port matching any VID:PID, or (None, None)."""
    r = _run(["usbip", "port"])
    lines = r.stdout.splitlines()
    for i, line in enumerate(lines):
        vp = _vp_matches(line, vidpids)
        if vp:
            for j in range(i - 1, -1, -1):
                m = re.match(r"Port (\d+):", lines[j].strip())
                if m:
                    return int(m.group(1)), vp
    return None, None


def do_attach(host, busid):
    """Run `sudo usbip attach -r HOST -b BUSID`. Return error string or None."""
    r = _run(["sudo", "usbip", "attach", "-r", host, "-b", busid])
    if r.returncode != 0:
        return r.stderr.strip() or "non-zero exit"
    return None


def do_detach(port_num):
    """Detach vhci port via sysfs. Returns True on success, False on failure."""
    r = _run(["sudo", "bash", "-c", f"echo {port_num} > {VHCI_DETACH_PATH}"])
    if r.returncode != 0:
        logging.warning("detach port %d failed (rc=%d): %s", port_num, r.returncode,
                        r.stderr.strip() or "no stderr")
        return False
    return True


def do_detach_all():
    """Best-effort detach of all vhci ports 0..7 (used to clean half-attached state)."""
    for port in range(8):
        _run(["sudo", "bash", "-c", f"echo {port} > {VHCI_DETACH_PATH}"])


def _attached_port_with_retry(vidpids, retries=10, delay=0.05):
    """Call attached_port with up to `retries` attempts separated by `delay` seconds."""
    for attempt in range(retries):
        port_num, matched_vp = attached_port(vidpids)
        if port_num is not None:
            return port_num, matched_vp
        if attempt < retries - 1:
            time.sleep(delay)
    return None, None


# ---------------------------------------------------------------------------
# Hub-mode helpers (event-driven path)
# ---------------------------------------------------------------------------


def attach_hub_via_usbip(host, hub_vidpid):
    """Locate and attach the annealage_pod hub_device on `host`.

    Returns (hub_busid, None) on success, (None, err) on failure. If the hub
    is already attached on vhci, returns ("(already-attached)", None) without
    invoking `usbip attach` again.
    """
    # Primary idempotency check: if the hub VID:PID is already visible on a
    # vhci port, do not re-run `usbip attach`. The busid is only informational
    # at the call site, so a placeholder is acceptable.
    port_num, _ = attached_port([hub_vidpid])
    if port_num is not None:
        logging.info("hub already attached on vhci port %d", port_num)
        return "(already-attached)", None

    listing, err = list_remote(host)
    if err:
        return None, f"usbip list failed: {err}"
    busid, _ = find_busid(listing, [hub_vidpid])
    if busid is None:
        return None, "hub device not found in usbip list"

    attach_err = do_attach(host, busid)
    if attach_err:
        # Fallback safety net for the race where another tool attached between
        # our attached_port() check and our do_attach() call. Not the primary
        # detection mechanism any more.
        low = attach_err.lower()
        if "already" in low or "imported" in low or "busy" in low:
            logging.info("hub busid %s already attached: %s", busid, attach_err)
            return busid, None
        return None, f"hub attach failed: {attach_err}"
    return busid, None


def _parse_hub_vidpid(s):
    """Parse a VVVV:PPPP string into (vid_int, pid_int). Raises ValueError on bad input."""
    parts = s.split(":")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 4:
        raise ValueError(f"hub-vidpid must be VVVV:PPPP (4 hex chars each), got {s!r}")
    try:
        return int(parts[0], 16), int(parts[1], 16)
    except ValueError:
        raise ValueError(f"hub-vidpid contains non-hex characters: {s!r}")


def open_hub_libusb_handle(ctx, hub_vid, hub_pid, retries=20, delay=0.1):
    """Open the hub via libusb after a usbip attach.

    vhci_hcd makes the device appear asynchronously, so retry briefly. Returns
    the libusb handle (caller owns it and must close) or None if the device
    cannot be opened. The interface is NOT claimed here; the caller must do so.
    """
    last_err = None
    for attempt in range(retries):
        try:
            handle = ctx.openByVendorIDAndProductID(hub_vid, hub_pid, skip_on_error=True)
        except usb1.USBError as exc:
            last_err = f"openByVendorIDAndProductID: {exc}"
            handle = None

        if handle is not None:
            # Vendor-class interface should not be claimed by a kernel driver,
            # but vhci_hcd has been observed to attach a generic driver in
            # rare cases. Detach defensively, ignore failures.
            try:
                if handle.kernelDriverActive(0):
                    handle.detachKernelDriver(0)
            except usb1.USBError:
                pass
            return handle

        if attempt < retries - 1:
            time.sleep(delay)

    if last_err:
        logging.debug("open_hub_libusb_handle: %s", last_err)
    return None


def drain_hub_events(handle):
    """Submit short-timeout reads until the firmware returns a ZLP or times out.

    Clears any stale events from the firmware's 4-slot ring left over from a
    previous session. Best-effort; non-fatal errors are swallowed.
    """
    for _ in range(8):
        try:
            data = handle.interruptRead(0x81, 2, timeout=HUB_DRAIN_TIMEOUT_MS)
        except usb1.USBError as exc:
            val = getattr(exc, "value", None)
            if val == usb1.ERROR_TIMEOUT:
                return
            logging.debug("drain swallowed error: %s", exc)
            return
        if not data:
            return


def read_hub_event(handle, timeout_ms=HUB_LIBUSB_TIMEOUT_MS):
    """Block on EP1 IN for an event.

    Returns (state, transient_error, fatal_error):
      - state is 0 or 1 on a real event, None on ZLP/timeout
      - transient_error True for IO/PIPE: caller should pause and retry
      - fatal_error is a string when the device is gone or unrecoverable
    """
    try:
        data = handle.interruptRead(0x81, 2, timeout=timeout_ms)
    except usb1.USBError as exc:
        val = getattr(exc, "value", None)
        if val == usb1.ERROR_TIMEOUT:
            return None, False, None
        if val == usb1.ERROR_NO_DEVICE:
            return None, False, "device gone"
        if val in (usb1.ERROR_IO, usb1.ERROR_PIPE, usb1.ERROR_INTERRUPTED,
                   usb1.ERROR_OVERFLOW):
            logging.warning("hub read transient error: %s", exc)
            return None, True, None
        return None, False, f"hub read error: {exc}"

    if not data:
        return None, False, None
    return data[0], False, None


# ---------------------------------------------------------------------------
# Polling loop (legacy v1 behaviour)
# ---------------------------------------------------------------------------


def polling_loop(host, vidpids, poll_waiting_s, poll_attached_s, once):
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # M2: if already attached at startup, begin in ATTACHED state.
    port_num, _ = attached_port(vidpids)
    if port_num is not None:
        logging.info("already attached on vhci port %d at startup", port_num)
        state = "ATTACHED"
        attached_port_num = port_num
    else:
        state = "WAITING"
        attached_port_num = None

    list_fail_count = 0
    logging.info("pod-connect (polling) starting: host=%s vidpids=%s state=%s",
                 host, ",".join(vidpids), state)

    while True:
        # B6: check shutdown flag at top of each iteration.
        if _shutdown:
            break

        if state == "WAITING":
            listing, err = list_remote(host)
            if _shutdown:
                break
            if err:
                list_fail_count += 1
                logging.warning("usbip list failed (%d): %s", list_fail_count, err)
                # H1: after 3 consecutive failures, re-resolve the hostname.
                if list_fail_count >= 3:
                    logging.warning("3 consecutive list failures; re-resolving %s", _DEFAULT_HOST)
                    host = resolve_host(_DEFAULT_HOST)
                    list_fail_count = 0
                time.sleep(poll_waiting_s)
                continue

            list_fail_count = 0
            busid, matched_vp = find_busid(listing, vidpids)
            if busid is None:
                time.sleep(poll_waiting_s)
                continue

            logging.info("found %s as busid %s, attaching", matched_vp, busid)
            attach_err = do_attach(host, busid)
            if _shutdown:
                break
            if attach_err:
                logging.warning("attach failed: %s", attach_err)
                time.sleep(poll_waiting_s)
                continue

            # B5/H7: kernel sysfs may not be ready immediately after attach.
            port_num, _ = _attached_port_with_retry(vidpids)
            if port_num is None:
                logging.warning("post-attach port lookup failed after retries; cleaning up")
                do_detach_all()
                time.sleep(poll_waiting_s)
                continue

            attached_port_num = port_num
            logging.info("attached %s busid %s on vhci port %d", matched_vp, busid, port_num)
            state = "ATTACHED"

            if once:
                logging.info("--once: exiting after attach")
                # --once means "attach and leave it attached for the caller".
                # Clear the port number so the finally cleanup does NOT detach
                # what we just attached (also blocks late-signal teardown).
                attached_port_num = None
                sys.exit(0)

        elif state == "ATTACHED":
            time.sleep(poll_attached_s)
            if _shutdown:
                break
            port_num, _ = attached_port(vidpids)
            if port_num is None:
                logging.info("device gone from vhci, returning to WAITING")
                attached_port_num = None
                state = "WAITING"

    # B6: clean shutdown after signal.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if attached_port_num is not None:
        logging.info("shutting down: detaching port %d", attached_port_num)
        ok = do_detach(attached_port_num)
        if not ok:
            sys.exit(1)
    logging.info("pod-connect stopped")


# ---------------------------------------------------------------------------
# Hub-mode loop (event-driven path)
# ---------------------------------------------------------------------------


class _DutAttachState:
    """Mutable state shared between _attach_dut_now invocations within a session."""
    __slots__ = ("last_unmatched_attach_at",)

    def __init__(self):
        self.last_unmatched_attach_at = 0.0


def _attach_dut_now(host, vidpids, attach_state):
    """Attempt one full attach cycle for the DUT. Returns (port, vp) or (None, None).

    H3: if the previous call within the last 2 seconds failed to find a matching
    VID:PID, skip the `usbip list` round trip to avoid hammering the server when
    a non-target device is flapping.
    """
    now = time.monotonic()
    if now - attach_state.last_unmatched_attach_at < 2.0:
        logging.debug("skipping list_remote: recent unmatched attach (%.2fs ago)",
                      now - attach_state.last_unmatched_attach_at)
        return None, None

    listing, err = list_remote(host)
    if err:
        logging.warning("usbip list failed during hub-driven attach: %s", err)
        return None, None
    busid, matched_vp = find_busid(listing, vidpids)
    if busid is None:
        # Event fired but the DUT VID:PID is not (yet) listed - the device
        # may still be enumerating on the server. Caller will retry.
        attach_state.last_unmatched_attach_at = time.monotonic()
        return None, None
    logging.info("hub event: attaching %s busid %s", matched_vp, busid)
    attach_err = do_attach(host, busid)
    if attach_err:
        logging.warning("attach failed: %s", attach_err)
        return None, None
    port_num, _ = _attached_port_with_retry(vidpids)
    if port_num is None:
        logging.warning("post-attach port lookup failed; cleaning up")
        do_detach_all()
        return None, None
    logging.info("attached %s busid %s on vhci port %d", matched_vp, busid, port_num)
    return port_num, matched_vp


def _hub_event_loop(handle, host, vidpids, once):
    """Run the event-driven state machine against an already-claimed hub handle.

    Returns True for clean shutdown via signal, False for libusb/hub failure
    (caller should fall back to polling_loop).
    """
    drain_hub_events(handle)
    attach_state = _DutAttachState()
    attached_port_num = None

    # Ground truth at startup: if DUT is already attached on vhci, adopt
    # that state; otherwise check the remote listing in case the DUT is
    # already present and we just need to attach it.
    port_num, vp = attached_port(vidpids)
    if port_num is not None:
        attached_port_num = port_num
        logging.info("hub mode: DUT already attached on vhci port %d (%s)", port_num, vp)
        if once:
            logging.info("--once: exiting; DUT already attached")
            # --once means "attach and leave it attached for the caller". Clear
            # the port number so the finally cleanup does NOT detach what we
            # just confirmed attached.
            return True
    else:
        # Try an immediate attach in case the DUT is already up on the server.
        port_num, _ = _attach_dut_now(host, vidpids, attach_state)
        if port_num is not None:
            attached_port_num = port_num
            if once:
                logging.info("--once: exiting after attach")
                # --once means "attach and leave it attached for the caller".
                # The caller is responsible for the port from here.
                return True

    state = "ATTACHED" if attached_port_num is not None else "WAITING"
    logging.info("pod-connect (hub) starting: host=%s vidpids=%s state=%s",
                 host, ",".join(vidpids), state)

    zlp_count = 0
    consecutive_transient = 0

    try:
        while True:
            if _shutdown:
                return True

            ev_state, transient, fatal = read_hub_event(handle)
            if _shutdown:
                # Signal arrived during interruptRead; treat libusb wakeup or
                # spurious return as shutdown - caller's cleanup will detach.
                return True
            if fatal:
                logging.warning("hub mode lost device: %s", fatal)
                return False
            if transient:
                consecutive_transient += 1
                if consecutive_transient > HUB_MAX_TRANSIENT:
                    logging.warning(
                        "hub mode: %d consecutive transient errors; falling back to polling",
                        consecutive_transient)
                    return False
                time.sleep(0.5)
                continue

            if ev_state is None:
                # ZLP / timeout: periodic reconciliation guards against missed events.
                consecutive_transient = 0
                zlp_count += 1
                if zlp_count >= HUB_RECONCILE_EVERY:
                    zlp_count = 0
                    port_num, _ = attached_port(vidpids)
                    if state == "ATTACHED" and port_num is None:
                        logging.info("reconcile: DUT vanished from vhci, returning to WAITING")
                        attached_port_num = None
                        state = "WAITING"
                    elif state == "WAITING" and port_num is not None:
                        logging.info("reconcile: DUT present on vhci port %d, adopting", port_num)
                        attached_port_num = port_num
                        state = "ATTACHED"
                continue

            zlp_count = 0
            consecutive_transient = 0

            if ev_state == 1:
                if state == "ATTACHED":
                    logging.debug("hub mount event while already ATTACHED; ignoring")
                    continue
                # Brief settle - firmware fires on mount edge, server-side
                # device enumeration may lag by a few ms.
                time.sleep(0.05)
                port_num, _ = _attach_dut_now(host, vidpids, attach_state)
                if port_num is None:
                    # Could not attach right now; stay in WAITING and rely on
                    # next event or reconcile.
                    continue
                attached_port_num = port_num
                state = "ATTACHED"
                if once:
                    logging.info("--once: exiting after attach")
                    # --once means "attach and leave it attached for the caller".
                    # Clear the port number so the finally cleanup does NOT
                    # detach what we just attached.
                    attached_port_num = None
                    return True
            elif ev_state == 0:
                if state == "ATTACHED":
                    logging.info("hub umount event: DUT disconnect")
                    # M3: vhci usually tears down its own port when the upstream
                    # device vanishes, but there is a race where the port can
                    # linger. Give it a brief moment, then verify and force a
                    # detach if it is still around.
                    time.sleep(0.1)
                    port_num, _ = attached_port(vidpids)
                    if port_num is not None:
                        logging.warning(
                            "vhci still holds port %d after umount event; explicit detach",
                            port_num)
                        do_detach(port_num)
                    attached_port_num = None
                    state = "WAITING"
            else:
                logging.debug("hub event with unknown state byte: %r", ev_state)
    finally:
        # Detach the DUT port on shutdown if we still own it. The hub device
        # itself is intentionally left attached so subsequent runs start fast.
        if attached_port_num is not None:
            logging.info("shutting down: detaching port %d", attached_port_num)
            do_detach(attached_port_num)


def hub_mode_loop(host, vidpids, hub_vidpid, poll_attached_s, once):
    """Event-driven main loop.

    Returns True for clean shutdown via signal, False for hub failure (caller
    should fall back to polling_loop).

    Resource ownership:
      - USBContext: created here, closed via `with` (B3).
      - libusb handle: opened by open_hub_libusb_handle, closed via try/finally
        because python-libusb1's USBDeviceHandle does not implement __enter__.
      - claimed interface 0: released via try/finally inside the handle scope.
    """
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # _parse_hub_vidpid validation has happened in main(); use unchecked here.
    hub_vid, hub_pid = _parse_hub_vidpid(hub_vidpid)

    hub_busid, err = attach_hub_via_usbip(host, hub_vidpid)
    if err:
        logging.warning("hub mode unavailable: %s", err)
        return False
    logging.info("hub attached on busid %s", hub_busid)

    try:
        with usb1.USBContext() as ctx:
            handle = open_hub_libusb_handle(ctx, hub_vid, hub_pid)
            if handle is None:
                logging.warning("hub mode unavailable: could not open hub device")
                return False
            try:
                try:
                    handle.claimInterface(0)
                except usb1.USBError as exc:
                    if getattr(exc, "value", None) == usb1.ERROR_ACCESS:
                        logging.warning(
                            "hub mode unavailable: access denied "
                            "(install src/tools/99-annealage-pod-hub.rules under "
                            "/etc/udev/rules.d/ and reload udev)")
                    elif getattr(exc, "value", None) == usb1.ERROR_BUSY:
                        logging.warning("hub mode unavailable: interface busy")
                    else:
                        logging.warning("hub mode unavailable: claimInterface failed: %s", exc)
                    return False
                try:
                    return _hub_event_loop(handle, host, vidpids, once)
                finally:
                    try:
                        handle.releaseInterface(0)
                    except usb1.USBError as exc:
                        logging.debug("hub releaseInterface: %s", exc)
            finally:
                try:
                    handle.close()
                except Exception as exc:
                    logging.debug("hub handle.close: %s", exc)
    except usb1.USBError as exc:
        logging.warning("hub mode failed: %s", exc)
        return False
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        logging.info("pod-connect stopped")


def main():
    parser = argparse.ArgumentParser(
        description="Re-attach a USB/IP DUT automatically when it disconnects."
    )
    parser.add_argument("--host", default=None,
                        help="USB/IP server hostname or IP (default: ANNEALAGE_POD_HOST env / annealage_pod-dabao.local)")
    parser.add_argument("--vidpid", default=_DEFAULT_VIDPID,
                        help="VID:PID to watch, comma-separated (default: c251:f00b)")
    parser.add_argument("--poll-waiting", type=int, default=150,
                        metavar="MS", help="poll interval while waiting (ms, default 150)")
    parser.add_argument("--poll-attached", type=int, default=1000,
                        metavar="MS", help="poll interval while attached (ms, default 1000)")
    parser.add_argument("--mode", choices=("auto", "hub", "poll"), default="auto",
                        help="auto: try hub then fall back to polling (default); "
                             "hub: hub only (error if unavailable); "
                             "poll: legacy polling only")
    parser.add_argument("--hub-vidpid", default=_DEFAULT_HUB_VIDPID,
                        help=f"VID:PID of the annealage_pod hub_device (default: {_DEFAULT_HUB_VIDPID})")
    parser.add_argument("--once", action="store_true",
                        help="attach once and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    if args.host:
        host = args.host
    else:
        host = resolve_host(_DEFAULT_HOST)

    vidpids = [v.strip() for v in args.vidpid.split(",") if v.strip()]
    poll_waiting_s = args.poll_waiting / 1000.0
    poll_attached_s = args.poll_attached / 1000.0

    # Mode dispatch.
    if args.mode == "poll":
        polling_loop(host, vidpids, poll_waiting_s, poll_attached_s, args.once)
        return

    if not HAVE_LIBUSB:
        if args.mode == "hub":
            logging.error("--mode hub requested but libusb1 not available; install python-libusb1")
            sys.exit(2)
        logging.warning("python-libusb1 not available; using polling mode")
        polling_loop(host, vidpids, poll_waiting_s, poll_attached_s, args.once)
        return

    # M7: validate hub VID:PID format up front so a typo fails fast with a
    # clear error rather than later inside the hub loop.
    try:
        _parse_hub_vidpid(args.hub_vidpid)
    except ValueError as exc:
        logging.error("%s", exc)
        sys.exit(2)

    # auto or hub: try hub first.
    shutdown_clean = hub_mode_loop(host, vidpids, args.hub_vidpid, poll_attached_s, args.once)
    if shutdown_clean:
        return

    if args.mode == "hub":
        logging.error("hub mode failed and --mode hub specified; exiting")
        sys.exit(1)

    # auto fallback: stay in polling for the rest of the session.
    logging.warning("falling back to polling mode for the remainder of this session")
    polling_loop(host, vidpids, poll_waiting_s, poll_attached_s, args.once)


if __name__ == "__main__":
    main()
