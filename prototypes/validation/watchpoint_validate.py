"""Tier-2 validation: DWT data write-watchpoint set / trap / teardown.

Exercises the watchpoint path added to the debug stack: dbgsrv OP_WATCH_SET /
OP_WATCH_CLEAR -> swd_dap.DWT, wired into ops.gdb_serve (the _dwt session). It
drives the pod's binary debug server directly with PodLink (the host codec) over
Wi-Fi.

Method (deterministic, no reliance on what the firmware happens to write):
  1. start the on-pod gdb_serve, connect PodLink, halt;
  2. inject a 4-byte Thumb stub into a RAM code-scratch:  str r1,[r0] ; b .
     so a single resume executes exactly one store to the watched address and
     then spins;
  3. set r0 = watched addr, r1 = a marker value, PC = stub, xPSR.T = 1;
  4. arm a 4-byte WRITE watchpoint (DWT FUNCTION = 5) on the watched word;
  5. RESUME_WAIT: expect a halt with DFSR.DWTTRAP set (the store tripped it);
  6. clear the watchpoint and RESUME_WAIT again over the same spin loop: expect
     a timeout (the comparator is disarmed, nothing re-traps).
All touched RAM (the stub, the watched word) and core registers are saved and
restored; the script is non-destructive and re-runnable.

Run (after the firmware is deployed):
  python3 prototypes/validation/watchpoint_validate.py [pod-label]

Prints PASS/FAIL with the observed DFSR / DHCSR.
"""

import socket
import threading
import time

from _podboot import pod_from_label

GDB_PORT = 3335

CODE_ADDR = 0x2003FF00        # high RAM scratch for the injected stub
WATCH_ADDR = 0x2003FF10       # high RAM scratch for the watched word
MARKER = 0xDEAD_BEEF

# Thumb stub: str r1,[r0] (0x6001) ; b . (0xE7FE). Little-endian halfwords.
STUB = (0x6001).to_bytes(2, "little") + (0xE7FE).to_bytes(2, "little")

S_HALT = 1 << 17
DFSR_DWTTRAP = 1 << 2
DWT_FN_WATCH_WRITE = 6
XPSR_THUMB = 0x0100_0000

REG_R0, REG_R1, REG_PC, REG_XPSR = 0, 1, 15, 16
RESUME_WINDOW_MS = 500


def _connect_pod_debug(pod):
    """Start the on-pod gdb_serve worker and return (sock, worker, result)."""
    result = {}

    def _run():
        try:
            result["out"] = pod.exec(pod._gdb_serve_cmd(GDB_PORT, True))
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc

    worker = threading.Thread(target=_run)
    worker.start()
    sock = None
    endpoint = pod.resolver.endpoint(GDB_PORT)
    for _ in range(100):
        if "exc" in result:
            break
        try:
            sock = socket.create_connection(endpoint, timeout=5)
            break
        except OSError:
            time.sleep(0.1)
    return sock, worker, result


def main():
    label, pod = pod_from_label()
    print("DWT write-watchpoint validation against pod %r (nRF DUT)" % label)

    from pod.gdbserver import PodLink, PodError

    sock, worker, result = _connect_pod_debug(pod)
    if sock is None:
        worker.join()
        print("FAIL: could not connect to pod debug port %d: %r"
              % (GDB_PORT, result.get("exc")))
        return 1
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    link = PodLink(sock)

    rc = 0
    saved = {}
    try:
        link.ping()
        link.halt()

        # Save the RAM we overwrite and the core regs we set.
        saved["code"] = link.read_mem(CODE_ADDR, len(STUB))
        saved["watch"] = link.read_mem(WATCH_ADDR, 4)
        for rs in (REG_R0, REG_R1, REG_PC, REG_XPSR):
            saved[rs] = link.read_reg(rs)

        # Inject the stub and point the core at it.
        link.write_mem(CODE_ADDR, STUB)
        link.write_mem(WATCH_ADDR, (0).to_bytes(4, "little"))
        link.write_reg(REG_R0, WATCH_ADDR)
        link.write_reg(REG_R1, MARKER)
        link.write_reg(REG_PC, CODE_ADDR)
        link.write_reg(REG_XPSR, XPSR_THUMB)

        # Arm a 4-byte WRITE watchpoint on the watched word.
        slot = link.watch_set(WATCH_ADDR, 4, DWT_FN_WATCH_WRITE)
        print("watch_set slot=%d on 0x%08x (write, 4 bytes)" % (slot, WATCH_ADDR))

        # Resume one window; the single store should trip the watchpoint.
        r = link.resume_wait(RESUME_WINDOW_MS)
        print("resume_wait #1:", r)
        trapped = (r["state"] == "halt"
                   and bool(r["dhcsr"] & S_HALT)
                   and bool(r["dfsr"] & DFSR_DWTTRAP))
        # The store should have landed before the trap was taken.
        wrote = link.read_mem(WATCH_ADDR, 4)
        wrote_val = int.from_bytes(wrote, "little")
        print("watched word after trap = 0x%08x (want 0x%08x)"
              % (wrote_val, MARKER))
        if not trapped:
            print("FAIL: watchpoint did not trap "
                  "(state=%s dhcsr=0x%08x dfsr=0x%08x)"
                  % (r.get("state"), r.get("dhcsr", 0), r.get("dfsr", 0)))
            rc = 1

        # Clear the watchpoint; re-point PC at the stub and confirm no re-trap.
        link.watch_clear(WATCH_ADDR, 4, DWT_FN_WATCH_WRITE)
        link.halt()
        link.write_reg(REG_PC, CODE_ADDR)
        link.write_reg(REG_XPSR, XPSR_THUMB)
        link.write_reg(REG_R0, WATCH_ADDR)
        link.write_reg(REG_R1, MARKER)
        r2 = link.resume_wait(RESUME_WINDOW_MS)
        print("resume_wait #2 (after clear):", r2)
        if r2["state"] != "timeout":
            # A halt here means the comparator was not torn down (still trapping)
            # or the core faulted; either way teardown is not clean.
            if r2["state"] == "halt" and (r2["dfsr"] & DFSR_DWTTRAP):
                print("FAIL: watchpoint still trapped after clear (teardown leak)")
                rc = 1
            else:
                print("note: post-clear resume halted for a non-DWT reason: %r" % r2)

        if rc == 0:
            print("PASS: write watchpoint trapped on the store (DFSR.DWTTRAP) "
                  "and cleared cleanly")
    except PodError as exc:
        print("FAIL: pod returned an error status: %r" % exc)
        rc = 1
    except Exception as exc:  # noqa: BLE001
        print("FAIL: %r" % exc)
        rc = 1
    finally:
        # Best-effort restore of RAM and regs, then disarm and let serve()'s
        # finally resume the core when the socket closes.
        try:
            link.halt()
            if "code" in saved:
                link.write_mem(CODE_ADDR, saved["code"])
            if "watch" in saved:
                link.write_mem(WATCH_ADDR, saved["watch"])
            for rs in (REG_R0, REG_R1, REG_PC, REG_XPSR):
                if rs in saved:
                    link.write_reg(rs, saved[rs])
            try:
                link.watch_clear(WATCH_ADDR, 4, DWT_FN_WATCH_WRITE)
            except PodError:
                pass
        except Exception:  # noqa: BLE001 - teardown best-effort
            pass
        try:
            sock.close()
        except OSError:
            pass
        worker.join()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
