"""CMSIS flash-algorithm (.FLM) parsing, host side.

A .FLM file is an ELF32 little-endian ARM image built to ARM's CMSIS flash
programming contract (``FlashOS.H`` in the CMSIS-Pack specification). It carries:

* ``PrgCode`` - position-independent Thumb code exporting the fixed entry points
  ``Init``, ``UnInit``, ``EraseSector``, ``ProgramPage`` and optionally
  ``EraseChip`` / ``Verify`` / ``BlankCheck``;
* ``PrgData`` - the algorithm's read-write data, addressed through R9 (the
  static base), since the algorithm is built read-write-position-independent;
* ``DevDscr`` - one ``struct FlashDevice``, giving the device name, the flash
  base address and size, the programming page size, the erased byte value, the
  operation timeouts, and the erase-sector map.

This module turns such a file into the ``algo`` dict that the on-pod runner
(``annealage_pod.debug.flm.FLMFlasher``) executes, laid out for a specific
target RAM region. The pod stores no algorithms of its own; the host resolves
one per DUT and installs it.

Public API
----------
parse_flm(data) -> FlmImage
    Parse .FLM bytes into its image, symbols and device descriptor.

FlmImage.build_algo(ram_start, ram_size, ...) -> dict
    Lay the image out in the target's RAM and emit the pod's algo dict.

load_algo(path, ram_start, ram_size, ...) -> dict
    parse_flm + build_algo in one call.
"""

import struct

# struct FlashDevice, CMSIS FlashOS.H. Natural ARM alignment puts valEmpty at
# offset 148 with three bytes of padding before toProg, so the fixed header is
# 160 bytes and the sector map follows it.
_DEV_FMT = "<H128sHIIIIB3xII"
_DEV_LEN = struct.calcsize(_DEV_FMT)         # 160

# struct FlashSectors[]: (szSector, AddrSector) pairs, AddrSector relative to
# the device base, terminated by an all-ones entry.
_SECTOR_FMT = "<II"
_SECTOR_LEN = struct.calcsize(_SECTOR_FMT)   # 8
_SECTOR_END = 0xFFFFFFFF

# Entry points. The first four are required for the pod to flash anything; the
# rest are used when present.
_REQUIRED_SYMS = ("Init", "UnInit", "EraseSector", "ProgramPage")
_OPTIONAL_SYMS = ("EraseChip", "Verify", "BlankCheck")

# The pod calls into the algorithm with LR pointing at load_address|1, so the
# blob starts with BKPT instructions: returning there halts the core and hands
# control back to the debugger. Four of them keep the code that follows
# 8-byte aligned.
_TRAMPOLINE = b"\x00\xbe" * 4

_DEFAULT_STACK = 0x400


class FlmError(Exception):
    """The .FLM is not a usable CMSIS flash algorithm."""


class FlmImage:
    """A parsed .FLM: its loadable image, entry-point symbols and device info.

    Attributes:
        image: the concatenated ALLOC sections as one contiguous blob.
        image_base: the address image[0] is linked at (symbol values are in
            this same space).
        data_offset: offset of PrgData within image, or None if there is none.
        symbols: {name: address} for the CMSIS entry points that are present.
        device: the decoded FlashDevice fields.
        sectors: [(offset_from_flash_base, sector_size), ...], ascending.
    """

    def __init__(self, image, image_base, data_offset, symbols, device, sectors):
        self.image = image
        self.image_base = image_base
        self.data_offset = data_offset
        self.symbols = symbols
        self.device = device
        self.sectors = sectors

    @property
    def name(self):
        return self.device["name"]

    @property
    def flash_base(self):
        return self.device["flash_base"]

    @property
    def flash_size(self):
        return self.device["flash_size"]

    @property
    def page_size(self):
        return self.device["page_size"]

    def ram_needed(self, stack_size=_DEFAULT_STACK, page_buffer=None):
        """Bytes of target RAM the laid-out algorithm occupies.

        Blob (trampoline + image), then the algorithm's stack, then the page
        buffer ProgramPage reads its payload from.
        """
        if page_buffer is None:
            page_buffer = self.page_size
        blob = len(_TRAMPOLINE) + len(self.image)
        return _align(blob, 8) + stack_size + page_buffer

    def build_algo(self, ram_start, ram_size, stack_size=_DEFAULT_STACK,
                   page_buffer=None, name=None):
        """Lay the algorithm out in [ram_start, ram_start+ram_size) for the pod.

        Returns the algo dict consumed by the on-pod FLMFlasher: the blob to
        upload, the target addresses to run it at, and the device geometry.

        Raises:
            FlmError: if the region cannot hold the blob, stack and page buffer.
        """
        if page_buffer is None:
            page_buffer = self.page_size
        if ram_start % 4:
            raise FlmError("ram_start 0x%08x is not word-aligned" % ram_start)

        blob = _TRAMPOLINE + self.image
        code_base = ram_start + len(_TRAMPOLINE)      # where image[0] lands

        stack_base = _align(ram_start + len(blob), 8)
        begin_stack = stack_base + stack_size          # SP: full descending
        begin_data = _align(begin_stack, 4)
        end = begin_data + page_buffer
        if end > ram_start + ram_size:
            raise FlmError(
                "algorithm needs %d bytes of RAM but only %d available at "
                "0x%08x (blob %d, stack %d, page buffer %d)"
                % (end - ram_start, ram_size, ram_start, len(blob), stack_size,
                   page_buffer))

        def _entry(sym):
            # Symbol values are in image space; rebase onto the load address.
            # The Thumb bit is not carried in the PC - the pod sets xPSR.T.
            return (code_base + (self.symbols[sym] - self.image_base)) & ~1

        algo = {
            "name": name or self.name,
            "instructions": bytes(blob),
            "load_address": ram_start,
            "begin_stack": begin_stack,
            "begin_data": begin_data,
            "static_base": (code_base + self.data_offset
                            if self.data_offset is not None else begin_data),
            "pc_init": _entry("Init"),
            "pc_unInit": _entry("UnInit"),
            "pc_erase_sector": _entry("EraseSector"),
            "pc_program_page": _entry("ProgramPage"),
            "flash_base": self.flash_base,
            "flash_size": self.flash_size,
            "page_size": self.page_size,
            "sectors": self.sectors,
            "erased_byte": self.device["erased_byte"],
        }
        if "EraseChip" in self.symbols:
            algo["pc_eraseAll"] = _entry("EraseChip")
        return algo


def _align(value, to):
    return (value + to - 1) & ~(to - 1)


def _decode_device(blob):
    """Decode struct FlashDevice, returning its fields and the sector map."""
    if len(blob) < _DEV_LEN:
        raise FlmError("DevDscr is %d bytes, need at least %d"
                       % (len(blob), _DEV_LEN))
    (vers, name, dev_type, dev_adr, sz_dev, sz_page, _res, val_empty,
     to_prog, to_erase) = struct.unpack(_DEV_FMT, blob[:_DEV_LEN])

    sectors = []
    off = _DEV_LEN
    while off + _SECTOR_LEN <= len(blob):
        size, addr = struct.unpack(_SECTOR_FMT, blob[off:off + _SECTOR_LEN])
        if size == _SECTOR_END and addr == _SECTOR_END:
            break
        sectors.append((addr, size))
        off += _SECTOR_LEN
    if not sectors:
        raise FlmError("FlashDevice carries no sector map")
    sectors.sort()

    if not sz_page:
        raise FlmError("FlashDevice page size is zero")
    if not sz_dev:
        raise FlmError("FlashDevice size is zero")

    device = {
        "version": vers,
        "name": name.split(b"\x00", 1)[0].decode("ascii", "replace"),
        "type": dev_type,
        "flash_base": dev_adr,
        "flash_size": sz_dev,
        "page_size": sz_page,
        "erased_byte": val_empty,
        "timeout_prog_ms": to_prog,
        "timeout_erase_ms": to_erase,
    }
    return device, sectors


def parse_flm(data):
    """Parse .FLM bytes into an FlmImage.

    Args:
        data: the .FLM file contents.

    Raises:
        FlmError: if the file is not an ELF, lacks DevDscr or PrgCode, or is
            missing a required CMSIS entry point.
        ImportError: if pyelftools is not installed.
    """
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError as exc:
        raise ImportError(
            "pyelftools is required to parse CMSIS .FLM algorithms: "
            "pip install pyelftools") from exc
    import io

    if data[:4] != b"\x7fELF":
        raise FlmError("not an ELF file (a .FLM is ELF32 little-endian ARM)")

    elf = ELFFile(io.BytesIO(data))

    dev_blob = None
    alloc = []          # (addr, size, bytes-or-None) for the loadable sections
    data_addr = None

    for sec in elf.iter_sections():
        header = sec.header
        name = sec.name
        if name == "DevDscr":
            dev_blob = sec.data()
            continue
        if not header["sh_flags"] & 0x2:        # SHF_ALLOC
            continue
        size = header["sh_size"]
        if not size:
            continue
        # SHT_NOBITS (8) is zero-initialised data: reserve it, transmit zeros.
        payload = None if header["sh_type"] == "SHT_NOBITS" else sec.data()
        alloc.append((header["sh_addr"], size, payload))
        if name == "PrgData":
            data_addr = header["sh_addr"]

    if dev_blob is None:
        raise FlmError("no DevDscr section; not a CMSIS flash algorithm")
    if not alloc:
        raise FlmError("no loadable sections (expected PrgCode)")

    alloc.sort(key=lambda s: s[0])
    image_base = alloc[0][0]
    image_end = max(addr + size for addr, size, _ in alloc)
    image = bytearray(image_end - image_base)
    for addr, size, payload in alloc:
        if payload is not None:
            off = addr - image_base
            image[off:off + size] = payload[:size]

    symbols = {}
    for sec in elf.iter_sections():
        if sec.header["sh_type"] not in ("SHT_SYMTAB", "SHT_DYNSYM"):
            continue
        for sym in sec.iter_symbols():
            if sym.name in _REQUIRED_SYMS or sym.name in _OPTIONAL_SYMS:
                symbols[sym.name] = sym["st_value"]

    missing = [s for s in _REQUIRED_SYMS if s not in symbols]
    if missing:
        raise FlmError("algorithm is missing required entry points: %s"
                       % ", ".join(missing))

    device, sectors = _decode_device(dev_blob)
    return FlmImage(bytes(image), image_base,
                    None if data_addr is None else data_addr - image_base,
                    symbols, device, sectors)


def load_algo(path, ram_start, ram_size, stack_size=_DEFAULT_STACK,
              page_buffer=None, name=None):
    """Read a .FLM from disk and lay it out for a target RAM region."""
    with open(path, "rb") as f:
        return parse_flm(f.read()).build_algo(
            ram_start, ram_size, stack_size=stack_size,
            page_buffer=page_buffer, name=name)
