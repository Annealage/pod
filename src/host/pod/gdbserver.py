"""Host-side GDB RSP server for the Annealage Pod debug stack.

Translates arm-none-eabi-gdb's Remote Serial Protocol into the pod's binary
debug-command protocol (the dbgsrv on TCP port 3335). The pod is a stateless
DAP RPC; all RSP semantics, target.xml, breakpoint bookkeeping, software
breakpoint memory filtering, and per-stop register/memory caching live here on
CPython where they reuse the binary primitives and are unit-testable offline.

Dependency-free; only the standard library. gdb connects with:
    target extended-remote 127.0.0.1:<listen_port>

Two cooperating pieces:
  PodLink   - wraps the pod socket; one method per binary opcode (the §2 wire
              contract). Builds a request frame, sends, reads the response
              header (capturing the reserved byte), reads the data, and raises
              PodError on a non-zero status. Unit-testable against a fake socket.
  GdbServer - the gdb-facing RSP endpoint. Owns one PodLink, a local TCP
              listener, the per-stop register/memory caches, and the
              breakpoint realisation policy (FPB for flash, SW 0xBE00 for RAM).

The three fatal-flaw resolutions of the design are implemented in the
run-control loop (§4.6): the continue path is a sequence of RESUME_WAIT calls;
a 0x03 (Ctrl-C) byte from gdb is forwarded to the pod as the framed
RESUME_WAIT interrupt flag on the next re-arm request (never a bare byte that
would corrupt the pod's request framing), so the pod halts the core and replies
host_halted; a gdb EOF during a continue ends the session cleanly rather than
spinning; the pod clears DFSR on every resume/step so the host always reads a
fresh halt cause.
"""

import socket
import struct
import time


# ── binary protocol constants (FROZEN CONTRACT §2) ───────────────────────────

PROTOCOL_VERSION = 1
MAX_DATA = 4096
FLASH_TOP = 0x20000000          # writes below this are flash (NVMC); RAM at/above

# Opcodes (§2.2)
OP_PING = 0x01
OP_INFO = 0x02
OP_HALT = 0x10
OP_RESUME = 0x11
OP_STEP = 0x12
OP_RUN_STATE = 0x13
OP_RESET = 0x14
OP_RESUME_WAIT = 0x16
OP_READ_REG = 0x20
OP_WRITE_REG = 0x21
OP_READ_REGS = 0x22
OP_WRITE_REGS = 0x23
OP_READ_MEM = 0x30
OP_WRITE_MEM = 0x31
OP_BP_SET = 0x40
OP_BP_CLEAR = 0x41
OP_BP_CLEAR_ALL = 0x42

# Status codes (§2.1)
STATUS_OK = 0
STATUS_XFER = 1
STATUS_BADOP = 2
STATUS_NOTHALTED = 3
STATUS_TIMEOUT = 4
STATUS_BADARGS = 5
STATUS_WRPROT = 6

_STATUS_NAMES = {
    STATUS_OK: "ok",
    STATUS_XFER: "transfer error",
    STATUS_BADOP: "bad opcode",
    STATUS_NOTHALTED: "not halted",
    STATUS_TIMEOUT: "timeout",
    STATUS_BADARGS: "bad args",
    STATUS_WRPROT: "write protected",
}

# Ctrl-C byte gdb sends out of band on the RSP socket (§1.1). The host catches
# it and forwards the interrupt to the pod as a framed RESUME_WAIT flag bit (not
# as a bare byte, which would corrupt the pod's request framing).
INTERRUPT_BYTE = 0x03

# RESUME_WAIT request flags (§2.2). already_running re-arms a wait without
# re-resuming; interrupt asks the pod to halt the core immediately and reply.
RW_FLAG_ALREADY_RUNNING = 1 << 0
RW_FLAG_INTERRUPT = 1 << 1

# DHCSR / DFSR bits the host decodes (mirrors swd_dap.py).
S_HALT = 1 << 17
S_LOCKUP = 1 << 19
DFSR_HALTED = 1 << 0
DFSR_BKPT = 1 << 1
DFSR_DWTTRAP = 1 << 2
DFSR_VCATCH = 1 << 3
DFSR_EXTERNAL = 1 << 4

# gdb signal numbers used in stop replies.
SIGINT = 2
SIGTRAP = 5
SIGBUS = 7
SIGSEGV = 11


# ── register map (the ONE place gdb-regnum <-> pod-regsel is reconciled, §4.5) ─
#
# pod regsel numbering follows CortexM.read_core_reg exactly:
#   0..12 = R0..R12, 13 = SP, 14 = LR, 15 = PC, 16 = xPSR, 17 = MSP, 18 = PSP.
# gdb's m-profile feature numbers xpsr at regnum 19, msp at 17, psp at 18.
# Getting xpsr->regsel-16 wrong silently corrupts `info registers`.
#
# Ordered by gdb_regnum; the g-packet emits in this order, each value 8 LE hex.
REG_MAP = [
    (0, 0, "r0"),
    (1, 1, "r1"),
    (2, 2, "r2"),
    (3, 3, "r3"),
    (4, 4, "r4"),
    (5, 5, "r5"),
    (6, 6, "r6"),
    (7, 7, "r7"),
    (8, 8, "r8"),
    (9, 9, "r9"),
    (10, 10, "r10"),
    (11, 11, "r11"),
    (12, 12, "r12"),
    (13, 13, "sp"),
    (14, 14, "lr"),
    (15, 15, "pc"),
    (17, 17, "msp"),
    (18, 18, "psp"),
    (19, 16, "xpsr"),
]

# gdb_regnum -> pod_regsel and the reverse, for p/P handling.
_GDBNUM_TO_REGSEL = {gdb: regsel for gdb, regsel, _ in REG_MAP}
_REGSEL_TO_GDBNUM = {regsel: gdb for gdb, regsel, _ in REG_MAP}

# The full READ_REGS mask issued once per stop (all pod regsels in the map).
ALL_REGS_MASK = 0
for _g, _regsel, _n in REG_MAP:
    ALL_REGS_MASK |= 1 << _regsel

# The subset of regsels emitted in a T stop reply (r7, sp, lr, pc), §4.5.
STOP_REPLY_REGSELS = [7, 13, 14, 15]


# Static M-profile target description (§4.4). VFP deferred; m-profile only.
TARGET_XML = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE feature SYSTEM "gdb-target.dtd">'
    '<target version="1.0">'
    '<architecture>arm</architecture>'
    '<feature name="org.gnu.gdb.arm.m-profile">'
    '<reg name="r0" bitsize="32" regnum="0"/>'
    '<reg name="r1" bitsize="32" regnum="1"/>'
    '<reg name="r2" bitsize="32" regnum="2"/>'
    '<reg name="r3" bitsize="32" regnum="3"/>'
    '<reg name="r4" bitsize="32" regnum="4"/>'
    '<reg name="r5" bitsize="32" regnum="5"/>'
    '<reg name="r6" bitsize="32" regnum="6"/>'
    '<reg name="r7" bitsize="32" regnum="7"/>'
    '<reg name="r8" bitsize="32" regnum="8"/>'
    '<reg name="r9" bitsize="32" regnum="9"/>'
    '<reg name="r10" bitsize="32" regnum="10"/>'
    '<reg name="r11" bitsize="32" regnum="11"/>'
    '<reg name="r12" bitsize="32" regnum="12"/>'
    '<reg name="sp" bitsize="32" type="data_ptr" regnum="13"/>'
    '<reg name="lr" bitsize="32" regnum="14"/>'
    '<reg name="pc" bitsize="32" type="code_ptr" regnum="15"/>'
    '<reg name="msp" bitsize="32" regnum="17"/>'
    '<reg name="psp" bitsize="32" regnum="18"/>'
    '<reg name="xpsr" bitsize="32" regnum="19"/>'
    '</feature>'
    '</target>'
)


def popcount(value):
    """Return the number of set bits in value (Python <3.10 compatible)."""
    return bin(value & 0xFFFFFFFF).count("1")


class PodError(Exception):
    """A pod debug command returned a non-zero status code."""

    def __init__(self, status):
        self.status = status
        super().__init__(
            "pod returned status %d (%s)"
            % (status, _STATUS_NAMES.get(status, "unknown")))


class _GdbDisconnect(Exception):
    """The gdb peer closed mid-session (EOF). Treated as a clean session end."""


# ── PodLink: the binary wire codec (one method per opcode) ────────────────────


class PodLink:
    """Speaks the pod binary debug protocol over one persistent TCP socket.

    Each method builds a request frame (§2.1), sends it, reads the 4-byte
    response header, reads the declared data, and raises PodError on a non-zero
    status. STATUS_TIMEOUT from resume_wait is a normal control-flow return,
    not an error.

    The socket is injected so tests can pass a fake. Any object with sendall()
    and a recv()/readinto-style read works; recv_exact() reads with recv().
    """

    def __init__(self, sock):
        self.sock = sock

    # framing helpers

    def _recv_exact(self, n):
        """Read exactly n bytes from the socket, raising on premature EOF."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise PodError(STATUS_XFER)
            buf += chunk
        return bytes(buf)

    def _request(self, opcode, args=b"", flags=0):
        """Send a request frame and return (status, reserved, data)."""
        if len(args) > MAX_DATA:
            raise ValueError("arg_len %d exceeds MAX_DATA" % len(args))
        self.sock.sendall(
            struct.pack("<BBH", opcode, flags, len(args)) + args)
        hdr = self._recv_exact(4)
        status, reserved, data_len = struct.unpack("<BBH", hdr)
        data = self._recv_exact(data_len) if data_len else b""
        return status, reserved, data

    def _checked(self, opcode, args=b"", flags=0):
        """Send a request; raise PodError unless status is OK. Returns data."""
        status, _reserved, data = self._request(opcode, args, flags)
        if status != STATUS_OK:
            raise PodError(status)
        return data

    # info / control

    def ping(self):
        data = self._checked(OP_PING)
        return struct.unpack("<I", data)[0]

    def info(self):
        data = self._checked(OP_INFO)
        return struct.unpack("<5I", data)

    def halt(self):
        data = self._checked(OP_HALT)
        return struct.unpack("<I", data)[0]

    def resume(self):
        self._checked(OP_RESUME)

    def step(self, maskints=True):
        data = self._checked(OP_STEP, struct.pack("<B", 1 if maskints else 0))
        return struct.unpack("<2I", data)

    def run_state(self):
        data = self._checked(OP_RUN_STATE)
        return struct.unpack("<2I", data)

    def reset(self, mode):
        data = self._checked(OP_RESET, struct.pack("<B", mode))
        return struct.unpack("<I", data)[0]

    def resume_wait(self, window_ms, already_running=False, interrupt=False):
        """Bounded, interruptible continue (§1.1, §1.2).

        Returns a dict:
          {"state": "halt",    "dhcsr": int, "dfsr": int, "host_halted": bool}
          {"state": "timeout", "dhcsr": int}

        STATUS_TIMEOUT is returned, not raised; it is normal control flow.

        interrupt=True sets the RESUME_WAIT interrupt flag so the pod halts the
        core at the top of the window and replies host_halted. The interrupt
        rides as a framed flag bit on this request, never as a bare 0x03 byte
        between frames (which would corrupt the pod's request framing).
        """
        flags = 0
        if already_running:
            flags |= RW_FLAG_ALREADY_RUNNING
        if interrupt:
            flags |= RW_FLAG_INTERRUPT
        status, reserved, data = self._request(
            OP_RESUME_WAIT, struct.pack("<IB", window_ms, flags))
        if status == STATUS_TIMEOUT:
            dhcsr = struct.unpack("<I", data)[0] if len(data) >= 4 else 0
            return {"state": "timeout", "dhcsr": dhcsr}
        if status != STATUS_OK:
            raise PodError(status)
        dhcsr, dfsr = struct.unpack("<2I", data)
        return {
            "state": "halt",
            "dhcsr": dhcsr,
            "dfsr": dfsr,
            "host_halted": bool(reserved & 1),
        }

    # registers (core must be halted)

    def read_reg(self, regsel):
        data = self._checked(OP_READ_REG, struct.pack("<B", regsel))
        return struct.unpack("<I", data)[0]

    def write_reg(self, regsel, value):
        self._checked(OP_WRITE_REG, struct.pack("<BI", regsel, value & 0xFFFFFFFF))

    def read_regs(self, mask):
        """Read the regsels selected by mask; return a {regsel: value} dict.

        The pod returns popcount(mask) words in ascending regsel order.
        """
        data = self._checked(OP_READ_REGS, struct.pack("<I", mask))
        count = popcount(mask)
        values = struct.unpack("<%dI" % count, data) if count else ()
        out = {}
        i = 0
        for regsel in range(32):
            if mask & (1 << regsel):
                out[regsel] = values[i]
                i += 1
        return out

    def write_regs(self, mask, values):
        """Write regsels selected by mask; values is a {regsel: value} dict."""
        ordered = []
        for regsel in range(32):
            if mask & (1 << regsel):
                ordered.append(values[regsel] & 0xFFFFFFFF)
        payload = struct.pack("<I", mask) + struct.pack("<%dI" % len(ordered), *ordered)
        self._checked(OP_WRITE_REGS, payload)

    # memory (host fans out reads/writes larger than MAX_DATA)

    def read_mem(self, addr, length):
        """Read length bytes from addr, splitting into <= MAX_DATA chunks."""
        out = bytearray()
        remaining = length
        cur = addr
        while remaining > 0:
            chunk = min(remaining, MAX_DATA)
            data = self._checked(OP_READ_MEM, struct.pack("<II", cur, chunk))
            out += data
            cur += chunk
            remaining -= chunk
        return bytes(out)

    def write_mem(self, addr, data):
        """Write data to addr, splitting into <= (MAX_DATA - 8) byte chunks.

        Each frame carries an 8-byte addr/len prefix, so the payload fits when
        the byte count stays under MAX_DATA - 8. Raises PodError(STATUS_WRPROT)
        if the pod rejects a flash-region write.
        """
        chunk_max = MAX_DATA - 8
        i = 0
        cur = addr
        n = len(data)
        while i < n:
            chunk = data[i:i + chunk_max]
            payload = struct.pack("<II", cur, len(chunk)) + chunk
            self._checked(OP_WRITE_MEM, payload)
            cur += len(chunk)
            i += len(chunk)

    # hardware breakpoints (FPB)

    def bp_set(self, addr):
        data = self._checked(OP_BP_SET, struct.pack("<I", addr))
        return data[0]

    def bp_clear(self, addr):
        self._checked(OP_BP_CLEAR, struct.pack("<I", addr))

    def bp_clear_all(self):
        self._checked(OP_BP_CLEAR_ALL)


# ── RSP framing helpers (pure, testable) ──────────────────────────────────────


def rsp_checksum(data):
    """Return the two-lowercase-hex RSP checksum of a payload bytes object."""
    return "%02x" % (sum(data) & 0xFF)


def build_packet(payload):
    """Wrap a payload (str or bytes) as $<payload>#<cksum>, returning bytes."""
    if isinstance(payload, str):
        payload = payload.encode("ascii")
    return b"$" + payload + b"#" + rsp_checksum(payload).encode("ascii")


def parse_packet(buf):
    """Extract the first complete RSP packet from buf.

    Returns (payload_bytes, checksum_ok, consumed) where consumed is the number
    of bytes of buf the packet (including $...#cs) occupies, or
    (None, None, 0) if no complete packet is present yet.
    """
    start = buf.find(b"$")
    if start < 0:
        return None, None, 0
    hash_idx = buf.find(b"#", start)
    if hash_idx < 0 or hash_idx + 2 >= len(buf):
        return None, None, 0
    payload = buf[start + 1:hash_idx]
    cksum = buf[hash_idx + 1:hash_idx + 3]
    ok = cksum.lower() == rsp_checksum(payload).encode("ascii")
    consumed = hash_idx + 3
    return bytes(payload), ok, consumed


def rsp_rle_expand(data):
    """Expand RSP run-length encoding in a packet payload.

    A run is encoded as <byte><'*'><count_char>, repeating <byte> an additional
    (count_char - 29) times. Applied to validated payloads (the checksum is
    computed over the compressed bytes) before any hex/binary decode, so G/M/X
    bodies are correct even when gdb compresses them. The escape byte 0x7d is
    transparent here; unescape runs after expansion on the binary X path.
    """
    if b"*" not in data:
        return data
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x2a and out and i + 1 < n:   # '*' with a preceding byte
            repeat = data[i + 1] - 29
            if repeat > 0:
                out.extend([out[-1]] * repeat)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def rsp_unescape(data):
    """Decode RSP binary escaping: 0x7d <next ^ 0x20>."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x7d and i + 1 < n:
            out.append(data[i + 1] ^ 0x20)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def rsp_escape(data):
    """Encode the RSP-significant bytes (# $ } *) with the 0x7d escape."""
    out = bytearray()
    for b in data:
        if b in (0x23, 0x24, 0x7d, 0x2a):  # # $ } *
            out.append(0x7d)
            out.append(b ^ 0x20)
        else:
            out.append(b)
    return bytes(out)


def encode_u32_le_hex(value):
    """Encode a 32-bit value as 8 little-endian hex chars (g-packet form)."""
    return "".join("%02x" % b for b in struct.pack("<I", value & 0xFFFFFFFF))


def decode_u32_le_hex(text):
    """Decode 8 little-endian hex chars into a 32-bit int."""
    raw = bytes.fromhex(text)
    return struct.unpack("<I", raw)[0]


def hex_to_bytes(text):
    """Decode a hex string to bytes (used for M-packet data)."""
    return bytes.fromhex(text)


def bytes_to_hex(data):
    """Encode bytes to a lowercase hex string (used for m-packet replies)."""
    return "".join("%02x" % b for b in data)


# ── GdbServer: the gdb-facing RSP endpoint ────────────────────────────────────


class GdbServer:
    """RSP endpoint that drives a PodLink and serves one gdb client.

    Owns the per-stop register and memory caches, the breakpoint realisation
    table, and the run-control loop with the in-band interrupt path. Most of
    the surface is unit-testable by feeding RSP byte strings to handle_packet()
    against a fake PodLink.
    """

    MEM_CACHE_LINE = 64

    def __init__(self, pod_addr=None, pod_port=3335, listen_host="127.0.0.1",
                 listen_port=0, resume_window_ms=200, link=None,
                 connect_timeout=10.0, resolver=None):
        """Construct a GdbServer.

        link may be injected (for tests / reuse); otherwise the pod socket is
        opened lazily by connect_pod() with the same poll-with-backoff as
        flash_dut while the REPL-started gdb_serve comes up. A resolver (from the
        Pod) supplies the connect endpoint so the gdb path shares the same
        IPv6-first/identity-checked target as every other transport; pod_addr is
        the back-compat fallback when no resolver is given.
        """
        self.pod_addr = pod_addr
        self._resolver = resolver
        self.pod_port = pod_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.resume_window_ms = resume_window_ms
        self.connect_timeout = connect_timeout
        self.link = link

        self.no_ack = False
        self._last_packet = None

        # per-stop caches (invalidated on every resume/step and on any write)
        self._reg_cache = None          # {regsel: value} or None
        self._mem_cache = {}            # {aligned_line_addr: bytes}

        # breakpoint bookkeeping (host owns the realisation table)
        # sw_bps: {addr: original_halfword_bytes}
        # hw_bps: set of addrs realised via FPB
        # promoted: addrs where a gdb hardware bp (Z1) fell back to SW
        self._sw_bps = {}
        self._hw_bps = set()
        self._promoted = set()

        self._gdb_sock = None           # set during serve_forever for interrupts

    # ── pod connection (poll-with-backoff, mirrors flash_dut) ─────────────

    def connect_pod(self):
        """Open the pod socket with poll-with-backoff and build the PodLink."""
        if self.link is not None:
            return self.link
        endpoint = (self._resolver.endpoint(self.pod_port)
                    if self._resolver is not None
                    else (self.pod_addr, self.pod_port))
        sock = None
        for _ in range(100):
            try:
                sock = socket.create_connection(endpoint, timeout=5)
                break
            except OSError:
                time.sleep(0.1)
        if sock is None:
            raise RuntimeError(
                "could not connect to pod debug port %d" % self.pod_port)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.link = PodLink(sock)
        return self.link

    # ── cache management ──────────────────────────────────────────────────

    def _invalidate_caches(self):
        self._reg_cache = None
        self._mem_cache = {}

    def _regs(self):
        """Return the per-stop register cache, filling it with one READ_REGS."""
        if self._reg_cache is None:
            self._reg_cache = self.link.read_regs(ALL_REGS_MASK)
        return self._reg_cache

    # ── memory with SW-breakpoint filtering (§4.3) ────────────────────────

    def _apply_sw_bp_filter(self, addr, data):
        """Substitute saved originals for any byte overlapping a SW breakpoint.

        gdb must never see the injected 0xBE00. Handles 1/2/4-byte straddles by
        overlaying each active SW breakpoint's two original bytes.
        """
        if not self._sw_bps:
            return data
        out = bytearray(data)
        end = addr + len(data)
        for bp_addr, original in self._sw_bps.items():
            for off in range(len(original)):
                byte_addr = bp_addr + off
                if addr <= byte_addr < end:
                    out[byte_addr - addr] = original[off]
        return bytes(out)

    def _read_mem_cached(self, addr, length):
        """Read memory for an `m` packet, served via the per-stop line cache."""
        line = self.MEM_CACHE_LINE
        first = (addr // line) * line
        last = ((addr + length - 1) // line) * line
        for base in range(first, last + 1, line):
            if base not in self._mem_cache:
                self._mem_cache[base] = self.link.read_mem(base, line)
        out = bytearray()
        for i in range(length):
            byte_addr = addr + i
            base = (byte_addr // line) * line
            out.append(self._mem_cache[base][byte_addr - base])
        return self._apply_sw_bp_filter(addr, bytes(out))

    # ── breakpoint policy (the realisation table, §4.3) ───────────────────

    def _set_breakpoint(self, addr, gdb_hw):
        """Realise a breakpoint at addr; return an RSP reply payload.

        Policy by address: flash (addr < FLASH_TOP) -> FPB; RAM -> SW 0xBE00.
        A gdb hardware bp (Z1) on RAM falls back to SW when the FPB rejects it.
        """
        if addr < FLASH_TOP:
            # flash: must use FPB (flash is not host-writable)
            try:
                self.link.bp_set(addr)
                self._hw_bps.add(addr)
                return "OK"
            except PodError:
                return "E01"
        # RAM
        if gdb_hw:
            # gdb asked for a hardware bp in RAM; FPBv1 cannot. Try FPB, fall
            # back to SW and record the promotion so z1 clears the right one.
            try:
                self.link.bp_set(addr)
                self._hw_bps.add(addr)
                return "OK"
            except PodError:
                self._set_sw_breakpoint(addr)
                self._promoted.add(addr)
                return "OK"
        self._set_sw_breakpoint(addr)
        return "OK"

    def _set_sw_breakpoint(self, addr):
        """Save the original halfword and patch 0xBE00 (Thumb BKPT)."""
        if addr in self._sw_bps:
            return
        original = self.link.read_mem(addr, 2)
        self._sw_bps[addr] = original
        self.link.write_mem(addr, b"\x00\xbe")
        self._mem_cache = {}

    def _clear_breakpoint(self, addr, gdb_hw):
        """Clear a breakpoint at addr; return an RSP reply payload."""
        if addr in self._hw_bps:
            try:
                self.link.bp_clear(addr)
            except PodError:
                return "E01"
            self._hw_bps.discard(addr)
            self._promoted.discard(addr)
            return "OK"
        if addr in self._sw_bps:
            original = self._sw_bps.pop(addr)
            self.link.write_mem(addr, original)
            self._promoted.discard(addr)
            self._mem_cache = {}
            return "OK"
        return "OK"

    def _clear_all_breakpoints(self):
        """Clear every realised breakpoint (FPB and SW)."""
        try:
            self.link.bp_clear_all()
        except PodError:
            pass
        self._hw_bps.clear()
        for addr, original in list(self._sw_bps.items()):
            try:
                self.link.write_mem(addr, original)
            except PodError:
                pass
        self._sw_bps.clear()
        self._promoted.clear()
        self._mem_cache = {}

    # ── stop replies / signal decode (§4.6) ───────────────────────────────

    def _signal_for(self, dhcsr, dfsr, host_halted):
        """Map a halt cause to a gdb signal number.

        host-requested halt -> SIGINT; core lockup -> SIGSEGV; an external debug
        event (DFSR.EXTERNAL, e.g. a fault that took the core while halt-on-debug
        was armed) -> SIGBUS; every debug-trap cause (BKPT / DWT / vector-catch /
        halted) -> SIGTRAP.
        """
        if host_halted:
            return SIGINT
        if dhcsr & S_LOCKUP:
            return SIGSEGV
        if dfsr & DFSR_EXTERNAL:
            return SIGBUS
        # BKPT / DWTTRAP / VCATCH / HALTED, and the no-bits-set fallthrough, are
        # reported as a debug trap.
        return SIGTRAP

    def _stop_reply(self, signal):
        """Build a T stop reply with r7/sp/lr/pc pairs + thread, from cache."""
        regs = self._regs()
        parts = ["T%02x" % signal]
        for regsel in STOP_REPLY_REGSELS:
            gdbnum = _REGSEL_TO_GDBNUM[regsel]
            parts.append("%02x:%s;" % (gdbnum, encode_u32_le_hex(regs[regsel])))
        parts.append("thread:1;")
        return "".join(parts)

    # ── run control with interrupt (§4.6) ─────────────────────────────────

    def _gdb_poll(self):
        """Non-blocking poll of the gdb socket during a continue.

        Returns one of:
          "interrupt" - a 0x03 (Ctrl-C) byte was read and consumed
          "eof"       - the gdb peer closed (select readable, recv b'')
          None        - nothing pending
        The returned interrupt is delivered to the pod as a framed RESUME_WAIT
        flag (never a bare byte). EOF is propagated so the continue loop and the
        serve loop treat it as a clean session end rather than spinning forever.
        No-op (returns None) when no gdb socket is set, so unit tests can drive
        the loop directly via scripted resume_wait results.
        """
        sock = self._gdb_sock
        if sock is None:
            return None
        # A fake socket may expose poll_event() ("interrupt"/"eof"/None) or
        # poll_interrupt() (bool) so tests can drive the loop without a real
        # select(); fall back to select() for real sockets.
        poll_event = getattr(sock, "poll_event", None)
        if poll_event is not None:
            return poll_event()
        poll = getattr(sock, "poll_interrupt", None)
        if poll is not None:
            return "interrupt" if poll() else None
        import select as _select
        try:
            r, _, _ = _select.select([sock], [], [], 0)
        except (OSError, ValueError):
            return None
        if not r:
            return None
        try:
            b = sock.recv(1)
        except OSError:
            return "eof"
        if not b:
            # select readable + recv b'' is a closed peer, NOT "no interrupt"
            # (the blocker-2 EOF/no-interrupt distinction).
            return "eof"
        if b[0] == INTERRUPT_BYTE:
            return "interrupt"
        # A stray non-0x03 byte between packets is benign; ignore it.
        return None

    def _continue(self):
        """Run the RESUME_WAIT cycle until the core halts; return a stop reply.

        This is the fatal-flaw-1.1/1.2 path: each cycle is one round-trip per
        wait-window. A 0x03 (Ctrl-C) from gdb during a continue is forwarded to
        the pod as the RESUME_WAIT interrupt flag on the next re-arm frame, so
        the pod halts the core and replies cleanly with the host_halted flag;
        no bare byte is ever written into the framed request stream. A gdb
        disconnect (EOF) raises _GdbDisconnect, which the serve loop treats as a
        clean session end.
        """
        self._invalidate_caches()
        interrupt = False
        result = self.link.resume_wait(
            self.resume_window_ms, already_running=False)
        while result["state"] == "timeout":
            event = self._gdb_poll()
            if event == "eof":
                raise _GdbDisconnect()
            if event == "interrupt":
                interrupt = True
            result = self.link.resume_wait(
                self.resume_window_ms, already_running=True,
                interrupt=interrupt)
            if interrupt:
                # the interrupt was delivered on this re-arm; do not repeat it
                interrupt = False
        signal = self._signal_for(
            result["dhcsr"], result["dfsr"], result["host_halted"])
        return self._stop_reply(signal)

    def _step(self):
        """Single-step one instruction; return a stop reply.

        stepi must let the target's own interrupts fire (maskints=False); a step
        that masks interrupts would silently step the ISR-less mainline and never
        observe a pending interrupt.
        """
        self._invalidate_caches()
        dhcsr, dfsr = self.link.step(maskints=False)
        signal = self._signal_for(dhcsr, dfsr, False)
        return self._stop_reply(signal)

    # ── RSP packet dispatch ────────────────────────────────────────────────

    def handle_packet(self, payload):
        """Translate one RSP packet payload (bytes) into a reply payload.

        Returns one of:
          bytes / str  - a reply payload to wrap in $...#cs
          None         - no reply (e.g. 'k')
        The serve loop owns ack/no-ack framing; this method is pure dispatch
        over the pod link and the caches.
        """
        if isinstance(payload, (bytes, bytearray)):
            text = payload.decode("ascii", "replace")
        else:
            text = payload
        raw = bytes(payload) if isinstance(payload, (bytes, bytearray)) else payload.encode("ascii")

        if text.startswith("qSupported"):
            # swbreak+/hwbreak+ are deliberately NOT advertised: the stop reply
            # does not emit the swbreak:/hwbreak: fields the negotiated contract
            # then requires, so gdb is left to fall back to PC-vs-breakpoint
            # matching (which works because m-reads are SW-bp-filtered).
            return ("PacketSize=1000;qXfer:features:read+;"
                    "QStartNoAckMode+")
        if text == "QStartNoAckMode":
            self.no_ack = True
            return "OK"
        if text.startswith("qXfer:features:read:target.xml:"):
            return self._handle_qxfer(text)
        if text == "?":
            dhcsr, dfsr = self.link.run_state()
            host_halted = False
            signal = self._signal_for(dhcsr, dfsr, host_halted)
            return self._stop_reply(signal)
        if text == "qAttached":
            return "1"
        if text == "qC":
            return "QC1"
        if text.startswith("qRcmd,"):
            return self._handle_qrcmd(text)
        if text == "qfThreadInfo":
            return "m1"
        if text == "qsThreadInfo":
            return "l"
        if text.startswith("H"):
            return "OK"
        if text.startswith("qSymbol::"):
            return "OK"
        if text == "g":
            return self._handle_g()
        if text.startswith("G"):
            return self._handle_G(text)
        if text.startswith("p"):
            return self._handle_p(text)
        if text.startswith("P"):
            return self._handle_P(text)
        if text.startswith("m"):
            return self._handle_m(text)
        if text.startswith("M"):
            return self._handle_M(text)
        if text.startswith("X"):
            return self._handle_X(raw)
        if text.startswith("c") or text.startswith("C"):
            return self._continue()
        if text.startswith("s") or text.startswith("S"):
            return self._step()
        if text == "vCont?":
            return "vCont;c;C;s;S"
        if text.startswith("vCont"):
            return self._handle_vcont(text)
        if text.startswith("Z"):
            return self._handle_Z(text)
        if text.startswith("z"):
            return self._handle_z(text)
        if text == "k":
            return None
        if text == "D":
            self._clear_all_breakpoints()
            try:
                self.link.resume()
            except PodError:
                pass
            return "OK"
        if text == "!":
            return "OK"
        if text.startswith("R"):
            # Extended-mode restart ('R XX'): reset the DUT through the debugger
            # and send NO reply (per the RSP spec). Reset-through-the-debugger is
            # part of the Phase 3 exit gate.
            self._reset_target(mode=1)
            return None
        # Unknown packet: empty reply tells gdb the stub does not support it.
        return ""

    # individual handlers

    def _handle_qxfer(self, text):
        # qXfer:features:read:target.xml:off,len
        spec = text[len("qXfer:features:read:target.xml:"):]
        off_s, _, len_s = spec.partition(",")
        off = int(off_s, 16)
        length = int(len_s, 16)
        blob = TARGET_XML.encode("ascii")
        chunk = blob[off:off + length]
        prefix = b"l" if off + length >= len(blob) else b"m"
        return prefix + chunk

    def _reset_target(self, mode):
        """Reset the DUT through the debugger and refresh run state (§reset).

        mode 0 = sysreset + run; mode 1 = reset & halt at the vector. Clears the
        breakpoint-realisation caches' staleness by invalidating the per-stop
        register/memory caches so the next read reflects the post-reset core.
        """
        self.link.reset(mode)
        self._invalidate_caches()

    def _handle_qrcmd(self, text):
        # qRcmd,<hex of an ASCII monitor command>. Supports "reset" (reset+run)
        # and "reset halt" (reset & halt). Reset-through-the-debugger is part of
        # the Phase 3 gate. Unknown monitor commands reply empty.
        hex_cmd = text[len("qRcmd,"):]
        try:
            cmd = bytes.fromhex(hex_cmd).decode("ascii", "replace").strip()
        except ValueError:
            return "E01"
        parts = cmd.split()
        if parts and parts[0] == "reset":
            mode = 1 if (len(parts) > 1 and parts[1] == "halt") else 0
            self._reset_target(mode)
            return "OK"
        return ""

    def _handle_g(self):
        regs = self._regs()
        return "".join(encode_u32_le_hex(regs[regsel])
                       for _gdb, regsel, _n in REG_MAP)

    def _handle_G(self, text):
        body = text[1:]
        if len(body) < len(REG_MAP) * 8:
            # A short G body cannot fill the all-regs write; reject rather than
            # building a partial dict and writing ALL_REGS_MASK (which would
            # KeyError in write_regs). gdb always sends the full block.
            return "E01"
        values = {}
        for idx, (_gdb, regsel, _n) in enumerate(REG_MAP):
            field = body[idx * 8:idx * 8 + 8]
            values[regsel] = decode_u32_le_hex(field)
        self.link.write_regs(ALL_REGS_MASK, values)
        self._reg_cache = dict(values)
        return "OK"

    def _handle_p(self, text):
        gdbnum = int(text[1:], 16)
        regsel = _GDBNUM_TO_REGSEL.get(gdbnum)
        if regsel is None:
            # Unknown register: error reply (matches _handle_P), not a fabricated
            # zero value. gdb only asks for regnums it parsed from target.xml.
            return "E01"
        regs = self._regs()
        if regsel in regs:
            return encode_u32_le_hex(regs[regsel])
        value = self.link.read_reg(regsel)
        regs[regsel] = value
        return encode_u32_le_hex(value)

    def _handle_P(self, text):
        body = text[1:]
        num_s, _, val_s = body.partition("=")
        gdbnum = int(num_s, 16)
        regsel = _GDBNUM_TO_REGSEL.get(gdbnum)
        if regsel is None:
            return "E01"
        value = decode_u32_le_hex(val_s)
        self.link.write_reg(regsel, value)
        if self._reg_cache is not None:
            self._reg_cache[regsel] = value
        return "OK"

    def _handle_m(self, text):
        body = text[1:]
        addr_s, _, len_s = body.partition(",")
        addr = int(addr_s, 16)
        length = int(len_s, 16)
        data = self._read_mem_cached(addr, length)
        return bytes_to_hex(data)

    def _handle_M(self, text):
        body = text[1:]
        head, _, data_hex = body.partition(":")
        addr_s, _, len_s = head.partition(",")
        addr = int(addr_s, 16)
        declared = int(len_s, 16)
        if addr < FLASH_TOP:
            return "E01"
        data = hex_to_bytes(data_hex)
        if len(data) != declared:
            # The length field validates the payload; a mismatch is malformed.
            return "E01"
        self.link.write_mem(addr, data)
        self._mem_cache = {}
        return "OK"

    def _handle_X(self, raw):
        # X addr,len:<binary data>  - raw is the undecoded packet bytes.
        body = raw[1:]
        colon = body.find(b":")
        head = body[:colon].decode("ascii")
        addr_s, _, len_s = head.partition(",")
        addr = int(addr_s, 16)
        declared = int(len_s, 16)
        if addr < FLASH_TOP:
            return "E01"
        data = rsp_unescape(body[colon + 1:])
        if len(data) != declared:
            return "E01"
        self.link.write_mem(addr, data)
        self._mem_cache = {}
        return "OK"

    def _handle_vcont(self, text):
        # vCont[;action[:thread]]... - parse the action list and apply the first
        # action's leading char. The target reports a single thread, so the
        # first action governs; c/C continue, s/S step. Branch on the action
        # char rather than substring membership (which conflated c/C and could
        # misclassify a multi-action spec).
        spec = text[len("vCont"):]
        for action in spec.split(";"):
            if not action:
                continue
            kind = action[0]
            if kind in ("c", "C"):
                return self._continue()
            if kind in ("s", "S"):
                return self._step()
        return ""

    def _parse_z(self, text):
        # Z<type>,addr,kind  or  z<type>,addr,kind
        bp_type = int(text[1:2], 16)
        rest = text[3:] if text[2:3] == "," else text[2:]
        addr_s, _, _kind = rest.partition(",")
        addr = int(addr_s, 16)
        return bp_type, addr

    def _handle_Z(self, text):
        bp_type, addr = self._parse_z(text)
        if bp_type == 0:
            return self._set_breakpoint(addr, gdb_hw=False)
        if bp_type == 1:
            return self._set_breakpoint(addr, gdb_hw=True)
        # Z2-Z4 watchpoints out of scope: empty reply.
        return ""

    def _handle_z(self, text):
        bp_type, addr = self._parse_z(text)
        if bp_type == 0:
            return self._clear_breakpoint(addr, gdb_hw=False)
        if bp_type == 1:
            return self._clear_breakpoint(addr, gdb_hw=True)
        return ""

    # ── serve loop ──────────────────────────────────────────────────────────

    def serve_forever(self, on_listen=None):
        """Accept one gdb client, run the RSP loop, return on detach/close.

        Connects to the pod first (so the dbgsrv must already be listening),
        then binds the local gdb listener. If on_listen is given it is called
        with (host, port) once the listener is bound, before accept() blocks
        (so the caller can print the endpoint before gdb attaches). Returns the
        (host, port) it bound, only after the gdb session ends.
        """
        self.connect_pod()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.listen_host, self.listen_port))
        bound_host, bound_port = listener.getsockname()[:2]
        self.listen_port = bound_port
        listener.listen(1)
        print("listening for gdb on %s:%d" % (bound_host, bound_port))
        if on_listen is not None:
            on_listen(bound_host, bound_port)
        try:
            gdb_sock, _ = listener.accept()
            gdb_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._gdb_sock = gdb_sock
            try:
                self._rsp_loop(gdb_sock)
            finally:
                self._gdb_sock = None
                gdb_sock.close()
        finally:
            listener.close()
            # Close the pod-facing socket so EOF is driven into the pod's
            # _read_exact: the pod serve() loop hits its finally, gdb_serve()
            # returns its result dict (unblocking the host worker.join()), and
            # the pod re-listens so the debug port frees for the next session.
            if self.link is not None:
                try:
                    self.link.sock.close()
                except OSError:
                    pass
                self.link = None
        return (bound_host, bound_port)

    def _send_packet(self, sock, payload):
        pkt = build_packet(payload)
        self._last_packet = pkt
        sock.sendall(pkt)

    def _rsp_loop(self, sock):
        """Run the RSP receive/dispatch loop on a connected gdb socket."""
        buf = bytearray()
        while True:
            # Drain any complete packets in the buffer before reading more.
            while True:
                # A bare interrupt 0x03 from gdb (outside a packet) is handled
                # by the _continue() loop; here we strip it from the front so
                # framing stays aligned when it arrives between packets.
                while buf[:1] == bytes([INTERRUPT_BYTE]):
                    del buf[0]
                if buf[:1] == b"+":
                    del buf[0]
                    continue
                if buf[:1] == b"-":
                    del buf[0]
                    if self._last_packet is not None:
                        sock.sendall(self._last_packet)
                    continue
                payload, ok, consumed = parse_packet(buf)
                if payload is None:
                    break
                del buf[:consumed]
                if not self.no_ack:
                    sock.sendall(b"-" if not ok else b"+")
                if not ok:
                    continue
                # Expand inbound run-length encoding on the validated payload
                # (checksum is over the compressed bytes) before dispatch, so
                # G/M/X bodies decode correctly.
                payload = rsp_rle_expand(payload)
                if payload == b"k":
                    return
                # A handler that touches the pod can raise PodError on a
                # recoverable status (a transient SWD glitch, a read while the
                # core is running). RSP requires an 'Enn' reply, never a session
                # teardown. A malformed packet is likewise answered, not fatal.
                # Only a genuine gdb EOF/disconnect ends the session.
                try:
                    reply = self.handle_packet(payload)
                except _GdbDisconnect:
                    return
                except PodError as exc:
                    reply = "E%02x" % min(exc.status or 1, 0xFF)
                except Exception:  # noqa: BLE001 - any handler bug -> Enn, not teardown
                    reply = "E01"
                if reply is None:
                    # No reply (extended-restart 'R'): keep the session open.
                    continue
                self._send_packet(sock, reply)
            chunk = sock.recv(4096)
            if not chunk:
                return
            buf += chunk
