"""Tests for the host-side CMSIS .FLM parser (pod.flm).

A .FLM is an ELF32 little-endian ARM image, so the fixtures here assemble one
byte by byte rather than depending on a vendor pack being present: PrgCode,
PrgData and a DevDscr holding a struct FlashDevice, plus a symbol table
carrying the CMSIS entry points. That keeps the layout arithmetic and the
FlashDevice decode (including the non-uniform sector map, the case a uniform
page-size assumption gets wrong) checkable offline.
"""

import struct

import pytest

from pod import flm

# ── synthetic .FLM construction ──────────────────────────────────────────

_SHT_PROGBITS, _SHT_SYMTAB, _SHT_STRTAB, _SHT_NOBITS = 1, 2, 3, 8
_SHF_WRITE, _SHF_ALLOC, _SHF_EXEC = 0x1, 0x2, 0x4

_CODE_ADDR = 0x00000000
_CODE = bytes(range(0x40))
_DATA_ADDR = 0x00000040
_DATA = bytes([0xA5]) * 0x10
_ENTRIES = {"Init": 0x04, "UnInit": 0x08, "EraseSector": 0x0C,
            "ProgramPage": 0x10, "EraseChip": 0x14}


def _flash_device(name=b"TestDev", base=0x08000000, size=0x100000,
                  page=0x400, empty=0xFF, sectors=((0x4000, 0x0),
                                                   (0x10000, 0x10000))):
    """Pack a struct FlashDevice (CMSIS FlashOS.H) plus its sector map."""
    blob = struct.pack(
        "<H128sHIIIIB3xII",
        0x0101, name.ljust(128, b"\x00"), 1, base, size, page, 0, empty,
        100, 3000)
    for sz, addr in sectors:
        blob += struct.pack("<II", sz, addr)
    return blob + struct.pack("<II", 0xFFFFFFFF, 0xFFFFFFFF)


def _build_flm(code=_CODE, data=_DATA, device=None, entries=None,
               data_nobits=False, with_devdscr=True):
    """Assemble a minimal but well-formed ELF32 ARM .FLM."""
    device = _flash_device() if device is None else device
    entries = _ENTRIES if entries is None else entries

    names, name_blob = {}, b"\x00"

    def _name(s):
        if s not in names:
            nonlocal name_blob
            names[s] = len(name_blob)
            name_blob += s.encode() + b"\x00"
        return names[s]

    # Section table: index 0 is the mandatory null entry.
    secs = [dict(name="", type=0, flags=0, addr=0, data=b"", link=0,
                 entsize=0, align=0)]
    secs.append(dict(name="PrgCode", type=_SHT_PROGBITS,
                     flags=_SHF_ALLOC | _SHF_EXEC, addr=_CODE_ADDR, data=code,
                     link=0, entsize=0, align=4))
    if data is not None:
        secs.append(dict(name="PrgData",
                         type=_SHT_NOBITS if data_nobits else _SHT_PROGBITS,
                         flags=_SHF_ALLOC | _SHF_WRITE, addr=_DATA_ADDR,
                         data=data, link=0, entsize=0, align=4))
    if with_devdscr:
        secs.append(dict(name="DevDscr", type=_SHT_PROGBITS, flags=0, addr=0,
                         data=device, link=0, entsize=0, align=4))

    # Symbol table: a null symbol then one STT_FUNC per entry point, all bound
    # to the PrgCode section.
    sym_names, sym_blob = b"\x00", struct.pack("<IIIBBH", 0, 0, 0, 0, 0, 0)
    for sym, value in entries.items():
        sym_blob += struct.pack("<IIIBBH", len(sym_names), value, 4,
                                (1 << 4) | 2,   # GLOBAL | STT_FUNC
                                0, 1)           # shndx 1 = PrgCode
        sym_names += sym.encode() + b"\x00"

    symtab_idx = len(secs)
    secs.append(dict(name=".symtab", type=_SHT_SYMTAB, flags=0, addr=0,
                     data=sym_blob, link=symtab_idx + 1, entsize=16, align=4))
    secs.append(dict(name=".strtab", type=_SHT_STRTAB, flags=0, addr=0,
                     data=sym_names, link=0, entsize=0, align=1))
    shstrndx = len(secs)
    secs.append(dict(name=".shstrtab", type=_SHT_STRTAB, flags=0, addr=0,
                     data=None, link=0, entsize=0, align=1))

    for s in secs:
        s["name_off"] = _name(s["name"]) if s["name"] else 0
    secs[shstrndx]["data"] = name_blob

    # Lay the payloads out after the ELF header, then the section table.
    body, offsets = b"", []
    cursor = 52
    for s in secs:
        payload = s["data"] or b""
        if s["type"] == _SHT_NOBITS:
            offsets.append((cursor, len(payload)))
            continue
        offsets.append((cursor, len(payload)))
        body += payload
        cursor += len(payload)

    shoff = cursor
    header = struct.pack(
        "<4sBBBB8xHHIIIIIHHHHHH",
        b"\x7fELF", 1, 1, 1, 0,          # ELFCLASS32, ELFDATA2LSB, EV_CURRENT
        2, 40, 1,                         # ET_EXEC, EM_ARM, version
        0, 0, shoff, 0x05000000,          # entry, phoff, shoff, EABI v5 flags
        52, 32, 0, 40, len(secs), shstrndx)

    shtab = b""
    for s, (off, size) in zip(secs, offsets):
        shtab += struct.pack(
            "<IIIIIIIIII", s["name_off"], s["type"], s["flags"], s["addr"],
            off, size, s["link"], 0, s["align"], s["entsize"])
    return header + body + shtab


# ── parsing ──────────────────────────────────────────────────────────────

def test_parses_image_symbols_and_device():
    img = flm.parse_flm(_build_flm())
    assert img.image_base == _CODE_ADDR
    assert img.image[:0x40] == _CODE
    assert img.image[0x40:0x50] == _DATA
    assert img.data_offset == 0x40
    assert img.symbols == _ENTRIES
    assert img.name == "TestDev"
    assert (img.flash_base, img.flash_size, img.page_size) == (
        0x08000000, 0x100000, 0x400)
    assert img.device["erased_byte"] == 0xFF
    assert img.device["timeout_erase_ms"] == 3000


def test_sector_map_is_offset_size_pairs_ascending():
    # FlashDevice stores (szSector, AddrSector); the pod wants (offset, size).
    img = flm.parse_flm(_build_flm())
    assert img.sectors == [(0x0, 0x4000), (0x10000, 0x10000)]


def test_sector_map_sorted_when_the_file_lists_it_out_of_order():
    dev = _flash_device(sectors=((0x10000, 0x10000), (0x4000, 0x0)))
    assert flm.parse_flm(_build_flm(device=dev)).sectors == [
        (0x0, 0x4000), (0x10000, 0x10000)]


def test_nobits_prgdata_is_reserved_and_zero_filled():
    # Uninitialised RW data occupies image space but transmits as zeros.
    img = flm.parse_flm(_build_flm(data_nobits=True))
    assert len(img.image) == 0x50
    assert img.image[0x40:0x50] == bytes(0x10)
    assert img.data_offset == 0x40


def test_rejects_non_elf():
    with pytest.raises(flm.FlmError, match="not an ELF"):
        flm.parse_flm(b"not an elf at all")


def test_rejects_missing_devdscr():
    with pytest.raises(flm.FlmError, match="no DevDscr"):
        flm.parse_flm(_build_flm(with_devdscr=False))


def test_rejects_missing_required_entry_point():
    entries = dict(_ENTRIES)
    del entries["ProgramPage"]
    with pytest.raises(flm.FlmError, match="ProgramPage"):
        flm.parse_flm(_build_flm(entries=entries))


def test_rejects_empty_sector_map():
    dev = _flash_device(sectors=())
    with pytest.raises(flm.FlmError, match="no sector map"):
        flm.parse_flm(_build_flm(device=dev))


def test_rejects_zero_page_size():
    with pytest.raises(flm.FlmError, match="page size is zero"):
        flm.parse_flm(_build_flm(device=_flash_device(page=0)))


# ── layout ───────────────────────────────────────────────────────────────

def test_build_algo_layout():
    img = flm.parse_flm(_build_flm())
    algo = img.build_algo(0x20000000, 0x10000)

    # blob = 8-byte BKPT trampoline + the 0x50-byte image
    assert algo["instructions"][:2] == b"\x00\xbe"
    assert len(algo["instructions"]) == 8 + 0x50
    assert algo["load_address"] == 0x20000000

    code_base = 0x20000008
    assert algo["pc_init"] == code_base + 0x04
    assert algo["pc_unInit"] == code_base + 0x08
    assert algo["pc_erase_sector"] == code_base + 0x0C
    assert algo["pc_program_page"] == code_base + 0x10
    assert algo["pc_eraseAll"] == code_base + 0x14
    assert algo["static_base"] == code_base + 0x40

    # stack sits above the blob, the page buffer above the stack
    assert algo["begin_stack"] == 0x20000058 + 0x400
    assert algo["begin_data"] == algo["begin_stack"]

    assert algo["flash_base"] == 0x08000000
    assert algo["page_size"] == 0x400
    assert algo["sectors"] == [(0x0, 0x4000), (0x10000, 0x10000)]
    assert algo["name"] == "TestDev"


def test_build_algo_omits_erase_all_when_the_algorithm_has_none():
    entries = {k: v for k, v in _ENTRIES.items() if k != "EraseChip"}
    algo = flm.parse_flm(_build_flm(entries=entries)).build_algo(
        0x20000000, 0x10000)
    assert "pc_eraseAll" not in algo


def test_entry_points_are_even_addresses():
    # Some toolchains set the Thumb bit in the symbol value; the pod puts the
    # Thumb bit in xPSR, so the PC it is given must be even.
    entries = {k: v | 1 for k, v in _ENTRIES.items()}
    algo = flm.parse_flm(_build_flm(entries=entries)).build_algo(
        0x20000000, 0x10000)
    for key in ("pc_init", "pc_unInit", "pc_erase_sector", "pc_program_page"):
        assert algo[key] % 2 == 0


def test_build_algo_rejects_a_region_that_is_too_small():
    img = flm.parse_flm(_build_flm())
    with pytest.raises(flm.FlmError, match="needs .* bytes of RAM"):
        img.build_algo(0x20000000, 0x100)


def test_build_algo_rejects_unaligned_ram_start():
    img = flm.parse_flm(_build_flm())
    with pytest.raises(flm.FlmError, match="not word-aligned"):
        img.build_algo(0x20000002, 0x10000)


def test_build_algo_forwards_the_devices_declared_timeouts():
    # Gap 3 of cmsis-flash-completion.md: _decode_device already parses these,
    # but build_algo dropped them, so the on-pod runner always fell back to its
    # hardcoded default regardless of what the pack declared.
    img = flm.parse_flm(_build_flm())  # _flash_device() default: to_prog=100, to_erase=3000
    algo = img.build_algo(0x20000000, 0x10000)
    assert algo["timeout_prog_ms"] == 100
    assert algo["timeout_erase_ms"] == 3000


def test_build_algo_reports_unset_timeouts_as_none_not_zero():
    dev = struct.pack(
        "<H128sHIIIIB3xII",
        0x0101, b"TestDev".ljust(128, b"\x00"), 1, 0x08000000, 0x100000, 0x400,
        0, 0xFF, 0, 0) + struct.pack("<II", 0x4000, 0x0) + struct.pack(
            "<II", 0xFFFFFFFF, 0xFFFFFFFF)
    algo = flm.parse_flm(_build_flm(device=dev)).build_algo(0x20000000, 0x10000)
    assert algo["timeout_prog_ms"] is None
    assert algo["timeout_erase_ms"] is None


def test_ram_needed_matches_the_built_layout():
    img = flm.parse_flm(_build_flm())
    need = img.ram_needed()
    algo = img.build_algo(0x20000000, need)
    assert algo["begin_data"] + img.page_size == 0x20000000 + need


def test_stack_and_page_buffer_are_configurable():
    img = flm.parse_flm(_build_flm())
    algo = img.build_algo(0x20000000, 0x10000, stack_size=0x800,
                          page_buffer=0x200)
    assert algo["begin_stack"] == 0x20000058 + 0x800


def test_static_base_falls_back_when_there_is_no_prgdata():
    algo = flm.parse_flm(_build_flm(data=None)).build_algo(0x20000000, 0x10000)
    assert algo["static_base"] == algo["begin_data"]


# ── real vendor algorithm (opt-in) ───────────────────────────────────────

def test_real_vendor_flm(request):
    """Parse an actual vendor-shipped .FLM, if one is pointed at.

    Vendor algorithms are proprietary blobs, so none is checked in; set
    ANNEALAGE_TEST_FLM=/path/to/x.FLM to run this against a real file. It
    catches what a synthetic fixture cannot: real algorithms are built by
    armlink, whose symbol values carry the Thumb bit, and are far larger than
    their loadable image (debug sections dominate the file).
    """
    import os
    path = os.environ.get("ANNEALAGE_TEST_FLM")
    if not path:
        pytest.skip("set ANNEALAGE_TEST_FLM to a vendor .FLM to run this")

    img = flm.parse_flm(open(path, "rb").read())
    assert img.name
    assert img.image and len(img.image) <= os.path.getsize(path)
    assert img.sectors and all(s > 0 for _, s in img.sectors)
    assert img.page_size > 0

    algo = img.build_algo(0x20000000, img.ram_needed())
    for key in ("pc_init", "pc_unInit", "pc_erase_sector", "pc_program_page"):
        assert algo[key] % 2 == 0, "%s must be an even PC" % key
        assert algo["load_address"] <= algo[key] < algo["begin_stack"]


def test_real_vendor_flm_from_the_local_pack_cache():
    """Parse the real nRF52840 algorithm out of whatever CMSIS pack this
    machine already has cached, catching what the synthetic fixtures above
    cannot: real algorithms are built by armlink, whose symbol values carry
    the Thumb bit, and are far larger than their loadable image (debug
    sections dominate the file). No vendor binary is committed to this repo -
    the cache is machine-local, so this skips rather than fails when it is
    empty (e.g. a fresh checkout or CI).
    """
    from pod import cmsis_pack
    try:
        pack, info = cmsis_pack.find_device("nRF52840_xxAA")
    except cmsis_pack.PackError:
        pytest.skip("no nRF52840 CMSIS pack cached locally")
    try:
        algorithm = info.flash_algorithm()
        data = pack.read(algorithm["file"])
    finally:
        pack.close()

    img = flm.parse_flm(data)
    assert img.name
    assert img.image and len(img.image) <= len(data)
    assert img.sectors and all(s > 0 for _, s in img.sectors)
    assert img.page_size > 0

    algo = img.build_algo(0x20000000, img.ram_needed())
    for key in ("pc_init", "pc_unInit", "pc_erase_sector", "pc_program_page"):
        assert algo[key] % 2 == 0, "%s must be an even PC" % key
        assert algo["load_address"] <= algo[key] < algo["begin_stack"]
