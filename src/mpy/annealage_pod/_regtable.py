# Pure register-table state logic for the SPI-target register-file
# personality, split out so it is importable and unit-testable under CPython
# (spi_target.py itself imports rp2/machine and cannot be loaded off-target).
#
# Protocol: a write transaction's first byte is a register pointer and
# subsequent bytes are stored at write_table[reg_ptr+]; a read transaction
# serves read_table[reg_ptr+], advancing per byte; both wrap modulo the table
# size. Full-duplex SPI has no inherent read/write bit, so bit7 of the command
# byte carries the direction (the usual SPI register-device convention):
# bit7=0 is a write (payload stored, pointer repointed to the 7-bit offset),
# bit7=1 is a read (no store, pointer still repointed so a repeated read is
# idempotent).
#
# Only the separate-CS turnaround is supported: a zero-payload pointer-set
# write CS, then a following read CS that names its own starting offset via
# the command byte. A combined write-then-read inside one CS is not, so there
# is no in-transaction advance to model: `reg_ptr` and `last_offset` are
# always set to the same value, the transaction's starting offset.
# `RegTable.read()` advances `reg_ptr` on its own for the host tests that
# exercise the MISO ring's wrap behaviour; it does not model a real
# feed()-driven read CS, which never advances the pointer (the CS-boundary
# IRQ always repoints MISO back to the CS's own starting offset, so a
# repeated read is idempotent - see spi_target.py's _arm_read).

READ_FLAG = 0x80
OFFSET_MASK = 0x7F


def parse_cmd(cmd, mask=OFFSET_MASK):
    """Split a command byte into (offset, is_read) per the bit7 convention."""
    return cmd & mask, bool(cmd & READ_FLAG)


def apply_write(table, offset, payload, size):
    """Copy `payload` into `table` starting at `offset`, wrapping modulo `size`.

    `table` is any writable byte-indexable object (bytearray/memoryview).
    Returns the number of bytes written.
    """
    n = 0
    for b in payload:
        table[(offset + n) % size] = b & 0xFF
        n += 1
    return n


class RegTable:
    """Reference register-table state machine, used only by host unit tests.

    The on-pod path calls `parse_cmd`/`apply_write` directly against the
    DMA-backed tables; this class models the same protocol end-to-end (one
    CS transaction per `feed()` call) against plain bytearrays so the tests
    can exercise write-then-read, pointer-only updates, and wrap behaviour
    without any hardware.
    """

    def __init__(self, size=256, mask=OFFSET_MASK):
        self.size = size
        self.mask = mask
        self.read_table = bytearray(size)
        self.write_table = bytearray(size)
        self.reg_ptr = 0
        self.last_offset = 0
        self.last_length = 0
        self.last_was_write = False

    def feed(self, mosi):
        """Process one CS transaction's captured MOSI bytes.

        `mosi[0]` is the command byte; `mosi[1:]` is the payload. A write
        (bit7 clear) stores the payload into write_table[offset+] when a
        payload is present. Either direction repoints reg_ptr to `offset`.

        The command byte's offset field is `mask`-wide (7 bits, 0..127)
        independent of `size`, so it is reduced modulo `size` before use:
        a table smaller than the offset field still wraps into range
        instead of indexing out of bounds.
        """
        offset, is_read = parse_cmd(mosi[0], self.mask)
        offset %= self.size
        self.reg_ptr = offset
        self.last_offset = offset
        self.last_length = len(mosi)
        self.last_was_write = not is_read
        if not is_read and len(mosi) > 1:
            apply_write(self.write_table, offset, mosi[1:], self.size)

    def read(self, n):
        """Serve `n` bytes from read_table[reg_ptr+], advancing/wrapping reg_ptr."""
        out = []
        for _ in range(n):
            out.append(self.read_table[self.reg_ptr])
            self.reg_ptr = (self.reg_ptr + 1) % self.size
        return out
