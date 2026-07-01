#!/usr/bin/env python3
"""Flatten a multi-section RP2350 MicroPython UF2 into a raw program-region bin.

probe-rs's UF2 loader mishandles the multi-section RP2350 UF2: it flashes only
the first (metadata) section and skips the real program, so the board does not
boot even though it re-enumerates. Flatten the UF2 to a single program-region
bin and flash it as raw bin at 0x10000000 instead. See docs/pod/dev-notes.md.
"""

import struct
import sys

# Pico 2 W: 4 MB flash mapped at 0x10000000.
FLASH, END = 0x10000000, 0x10400000


def flatten(uf2_path, bin_path):
    data = open(uf2_path, "rb").read()
    blocks = {}
    for i in range(len(data) // 512):
        b = data[i * 512:(i + 1) * 512]
        # UF2 block header: magic0, magic1, flags, target_addr, payload_size,
        # block_no, num_blocks, family_id (8 LE uint32).
        _, _, _flags, addr, size, _, _, _fam = struct.unpack("<8I", b[:32])
        if FLASH <= addr < END:
            blocks[addr] = b[32:32 + size]
    if not blocks:
        sys.exit("uf2_to_bin: no program-region blocks in {}".format(uf2_path))
    lo = min(blocks)
    hi = max(a + len(blocks[a]) for a in blocks)
    buf = bytearray(b"\xff" * (hi - lo))
    for a, d in blocks.items():
        buf[a - lo:a - lo + len(d)] = d
    open(bin_path, "wb").write(buf)
    print("uf2_to_bin: wrote {} ({} bytes, base 0x{:08x})".format(bin_path, len(buf), lo))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: uf2_to_bin.py <in.uf2> <out.bin>")
    flatten(sys.argv[1], sys.argv[2])
