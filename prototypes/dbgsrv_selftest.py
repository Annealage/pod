# Bring-up harness for the on-pod binary debug server (dbgsrv), Phase 3.
#
# Throwaway spike code, not built into firmware (prototypes/ per repo
# conventions). Exercises the §2 wire protocol against the real DUT independently
# of the host RSP layer, so the pod side can be validated before the host
# gdbserver exists.
#
# Usage on the pod (over the REPL), after Wi-Fi is up:
#   import dbgsrv_selftest as t
#   t.run()                       # starts the server, connects a loopback
#                                 # client, drives PING/INFO/HALT/READ_REGS/
#                                 # READ_MEM/RESUME_WAIT, prints results
#
# The server is started in a background thread so this one process can be both
# server and client over 127.0.0.1; on the deployed pod the host is the client
# and this harness is unnecessary.

import struct
import socket
import time
import _thread

import annealage_pod.debug.ops as ops
from annealage_pod.debug import dbgsrv

PORT = 3335


def _txn(sock, opcode, args=b""):
    # Send one request frame, read one response frame; returns (status, data).
    sock.sendall(struct.pack("<BBH", opcode, 0, len(args)) + args)
    hdr = _recv_exact(sock, 4)
    status, _reserved, data_len = struct.unpack("<BBH", hdr)
    data = _recv_exact(sock, data_len) if data_len else b""
    return status, _reserved, data


def _recv_exact(sock, n):
    buf = bytearray(n)
    mv = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(mv[got:])
        if not r:
            raise OSError("eof")
        got += r
    return bytes(buf)


def _server_thread(dp, ap, cm, fpb):
    try:
        dbgsrv.serve(dp, ap, cm, fpb, port=PORT)
    except Exception as e:  # noqa: BLE001
        print("server exited:", repr(e))


def run(clkdiv=8):
    dp, ap, cm, fl = ops._ensure(clkdiv)
    cm.reset_and_halt()
    _thread.start_new_thread(_server_thread, (dp, ap, cm, ops._fpb))
    time.sleep_ms(200)

    cl = socket.socket()
    cl.connect(("127.0.0.1", PORT))
    try:
        st, _, d = _txn(cl, dbgsrv.OP_PING)
        print("PING status=%d proto=%d" % (st, struct.unpack("<I", d)[0]))

        st, _, d = _txn(cl, dbgsrv.OP_INFO)
        dpidr, cpuid, part, fkb, rkb = struct.unpack("<IIIII", d)
        print("INFO status=%d dpidr=0x%08x cpuid=0x%08x part=0x%08x "
              "flash_kb=%d ram_kb=%d" % (st, dpidr, cpuid, part, fkb, rkb))

        st, _, d = _txn(cl, dbgsrv.OP_HALT)
        print("HALT status=%d dhcsr=0x%08x" % (st, struct.unpack("<I", d)[0]))

        # READ_REGS over PC(15) and SP(13).
        mask = (1 << 13) | (1 << 15)
        st, _, d = _txn(cl, dbgsrv.OP_READ_REGS, struct.pack("<I", mask))
        vals = struct.unpack("<%dI" % (len(d) // 4), d)
        print("READ_REGS status=%d sp=0x%08x pc=0x%08x" % (st, vals[0], vals[1]))

        st, _, d = _txn(cl, dbgsrv.OP_READ_MEM, struct.pack("<II", 0, 16))
        print("READ_MEM@0 status=%d %s" % (st, d.hex()))

        # RESUME_WAIT: short window, expect timeout while the core runs.
        st, _, d = _txn(cl, dbgsrv.OP_RESUME_WAIT, struct.pack("<IB", 200, 0))
        print("RESUME_WAIT status=%d (%s)"
              % (st, "timeout/running" if st == dbgsrv.STATUS_TIMEOUT else "halt"))
    finally:
        cl.close()
    print("selftest done; server thread will exit on socket close")
