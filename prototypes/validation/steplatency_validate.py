"""Tier-1 / Phase-3 exit gate: GDB-through-pod round-trip and step latency.

Measures the per-operation latency of the on-pod binary debug server (dbgsrv)
over Wi-Fi - the transport the host GDB RSP layer rides on. It starts the on-pod
gdb_serve in a worker thread (as Pod.gdb_endpoint does), connects a PodLink (the
host binary codec) straight to the pod's debug port over Wi-Fi, halts the core,
then times N single-steps and N single register reads. Reports mean/min/max ms
for each. This is the Phase-3 exit-gate latency measurement.

It drives PodLink directly rather than going through a gdb client, so the
numbers are the pod round-trip itself, free of any local gdb scheduling.

Run (after the firmware is deployed):
  python3 prototypes/validation/steplatency_validate.py [pod-label]

Prints the latency summary and a PASS line (it is a measurement, not a hard
gate; it FAILs only if the path does not function).
"""

import socket
import threading
import time

from _podboot import pod_from_label

GDB_PORT = 3335
N_STEPS = 50
N_REGREADS = 50
# DHCSR S_HALT bit (mirror of swd_dap / gdbserver) for the post-step halt check.
S_HALT = 1 << 17


def _summary(samples_ms):
    return (sum(samples_ms) / len(samples_ms), min(samples_ms), max(samples_ms))


def main():
    label, pod = pod_from_label()
    print("GDB-over-pod step-latency measurement against pod %r (nRF DUT)" % label)

    from pod.gdbserver import PodLink

    result = {}

    def _run():
        # Blocking on-pod server; reset_halt so the core is halted at the vector
        # and stepping is well defined from the first instruction.
        try:
            result["out"] = pod.exec(pod._gdb_serve_cmd(GDB_PORT, True))
        except Exception as exc:  # noqa: BLE001 - surfaced after join
            result["exc"] = exc

    worker = threading.Thread(target=_run)
    worker.start()

    # Poll-connect to the pod debug port over Wi-Fi (the on-pod server takes a
    # moment to bind), mirroring GdbServer.connect_pod.
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
    if sock is None:
        worker.join()
        print("FAIL: could not connect to pod debug port %d: %r"
              % (GDB_PORT, result.get("exc")))
        return 1

    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    link = PodLink(sock)
    rc = 0
    try:
        proto = link.ping()
        print("PING ok, pod protocol v%d" % proto)
        dhcsr = link.halt()
        print("HALT dhcsr=0x%08x (halted=%s)" % (dhcsr, bool(dhcsr & S_HALT)))

        # Time N single-steps. maskints=False matches the host stepi path.
        step_ms = []
        last_dhcsr = dhcsr
        for _ in range(N_STEPS):
            t0 = time.perf_counter()
            last_dhcsr, _dfsr = link.step(maskints=False)
            step_ms.append((time.perf_counter() - t0) * 1000.0)
        if not (last_dhcsr & S_HALT):
            print("WARN: core not halted after stepping (dhcsr=0x%08x)" % last_dhcsr)

        # Time N single register reads (PC).
        reg_ms = []
        for _ in range(N_REGREADS):
            t0 = time.perf_counter()
            link.read_reg(15)
            reg_ms.append((time.perf_counter() - t0) * 1000.0)

        s_mean, s_min, s_max = _summary(step_ms)
        r_mean, r_min, r_max = _summary(reg_ms)
        print("STEP  (%d): mean %.2f ms  min %.2f ms  max %.2f ms"
              % (N_STEPS, s_mean, s_min, s_max))
        print("REGRD (%d): mean %.2f ms  min %.2f ms  max %.2f ms"
              % (N_REGREADS, r_mean, r_min, r_max))
        print("PASS: GDB-through-pod path functional; latency reported above")
    except Exception as exc:  # noqa: BLE001
        print("FAIL: %r" % exc)
        rc = 1
    finally:
        # Closing the link socket drives EOF into the pod's _read_exact; serve()
        # hits its finally (resume + clear), gdb_serve() returns, the worker
        # joins, and the debug port frees for the next session.
        try:
            sock.close()
        except OSError:
            pass
        worker.join()
        if "exc" in result and rc == 0:
            print("note: on-pod gdb_serve raised: %r" % result["exc"])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
