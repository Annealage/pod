"""ELF image helpers for the pod flash path.

Dispatch on the 4-byte ELF magic (b"\\x7fELF"), not the file extension, so
.bin and .elf and extension-less files are all handled correctly.

Public API
----------
is_elf(path) -> bool
    Returns True if the file starts with the ELF magic.

parse_load_segments(path, flash_ranges) -> list of (lma, data, region)
    Extracts PT_LOAD segments with p_filesz > 0 from the ELF, classifies each
    as "flash" or "ram" using flash_ranges, and returns them sorted by lma
    (ascending, flash only; ram segments are appended after flash segments in
    their original order).

flash_ranges is a list of (lo, hi) half-open address ranges.  A segment that
straddles a flash/RAM boundary raises ValueError - split the ELF before
passing it here.
"""

_ELF_MAGIC = b"\x7fELF"


def is_elf(path: str) -> bool:
    """Return True if the file at path starts with the ELF magic bytes."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == _ELF_MAGIC
    except OSError:
        return False


def parse_load_segments(path: str, flash_ranges) -> list:
    """Parse PT_LOAD segments from an ELF file and classify them by region.

    Args:
        path: path to the ELF file.
        flash_ranges: iterable of (lo, hi) half-open address ranges that
            constitute the DUT flash geometry.  Segments landing entirely
            within one of these ranges are classified "flash"; all others
            are classified "ram".

    Returns:
        A list of (lma, data, region) tuples where:
            lma    - physical load address (p_paddr)
            data   - bytes to program (segment data truncated to p_filesz;
                     the zero-fill tail for .bss-style padding is NOT included)
            region - "flash" or "ram"

        Flash segments are sorted ascending by lma.  RAM segments follow in
        their original ELF order.

    Raises:
        ValueError: if a segment straddles a flash/RAM boundary (the segment
            starts in one region and ends in another).
        ImportError: if pyelftools is not installed.
    """
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError as exc:
        raise ImportError(
            "pyelftools is required for ELF flashing: "
            "pip install pyelftools") from exc

    flash_ranges = list(flash_ranges)

    def _in_flash(lma: int, size: int) -> bool:
        for lo, hi in flash_ranges:
            if lma >= lo and (lma + size) <= hi:
                return True
        return False

    def _any_flash(lma: int, size: int) -> bool:
        for lo, hi in flash_ranges:
            if lma < hi and (lma + size) > lo:
                return True
        return False

    flash_segs = []
    ram_segs = []

    with open(path, "rb") as f:
        elf = ELFFile(f)
        for seg in elf.iter_segments():
            if seg["p_type"] != "PT_LOAD":
                continue
            filesz = seg["p_filesz"]
            if filesz == 0:
                # BSS-only segment - nothing to transmit
                continue
            lma = seg["p_paddr"]
            data = seg.data()[:filesz]

            entirely_flash = _in_flash(lma, filesz)
            touches_flash = _any_flash(lma, filesz)

            if entirely_flash:
                flash_segs.append((lma, data, "flash"))
            elif touches_flash:
                raise ValueError(
                    "ELF segment 0x%08x..0x%08x straddles a flash/RAM boundary; "
                    "split the ELF before flashing" % (lma, lma + filesz))
            else:
                ram_segs.append((lma, data, "ram"))

    flash_segs.sort(key=lambda s: s[0])
    return flash_segs + ram_segs
