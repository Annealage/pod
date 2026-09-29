"""Tests for CMSIS Device Family Pack lookup (pod.cmsis_pack).

Packs are large vendor zips, so the fixtures here build a miniature one: a pdsc
plus the synthetic .FLM from test_flm. That covers the parts that decide which
algorithm runs and where - pdsc property inheritance, algorithm and RAM
selection, and the case-insensitive Windows-authored paths inside the archive -
without a network fetch or a vendor blob in the tree.

Nothing here performs network I/O; index_entries/download_pack are exercised
through injected fakes only.
"""

import zipfile

import pytest

from pod import cmsis_pack
from tests.test_flm import _build_flm, _flash_device

# A pdsc shaped like a vendor's: memory declared at family level, refined at
# device level, two algorithms of which one is the default.
_PDSC = """<?xml version="1.0" encoding="UTF-8"?>
<package>
  <vendor>TestVendor</vendor>
  <name>TestSeries_DFP</name>
  <devices>
    <family Dfamily="Test Series" Dvendor="TestVendor:99">
      <memory id="IRAM1" start="0x20000000" size="0x1000" default="1"/>
      <subFamily DsubFamily="TestSub">
        <device Dname="TESTDEV_xxAA">
          <memory id="IROM1" start="0x08000000" size="0x100000" default="1"/>
          <memory id="IRAM1" start="0x20000000" size="0x40000" default="1"/>
          <algorithm name="Flash\\uicr.FLM" start="0x10001000" size="0x220"/>
          <algorithm name="Flash\\main.FLM" start="0x08000000" size="0x100000"
                     RAMstart="0x20000000" RAMsize="0x8000" default="1"/>
        </device>
        <device Dname="TESTDEV_NOALGO">
          <memory id="IROM1" start="0x08000000" size="0x1000" default="1"/>
        </device>
      </subFamily>
    </family>
  </devices>
</package>
"""


@pytest.fixture
def pack_path(tmp_path):
    """A miniature .pack: pdsc + two algorithms, Windows-cased member paths."""
    path = tmp_path / "TestVendor.TestSeries_DFP.1.0.0.pack"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("TestVendor.TestSeries_DFP.pdsc", _PDSC)
        z.writestr("Flash/main.FLM", _build_flm())
        z.writestr("Flash/uicr.FLM",
                   _build_flm(device=_flash_device(name=b"UICR",
                                                   base=0x10001000,
                                                   size=0x220, page=0x100,
                                                   sectors=((0x220, 0x0),))))
    return path


# ── pdsc parsing ─────────────────────────────────────────────────────────

def test_devices_are_discovered_through_the_family_tree():
    devices = cmsis_pack.parse_pdsc(_PDSC)
    assert set(devices) == {"TESTDEV_xxAA", "TESTDEV_NOALGO"}
    assert devices["TESTDEV_xxAA"].vendor == "TestVendor"


def test_device_level_memory_refines_the_inherited_family_memory():
    # The family declares a nominal 4 KB IRAM1; the device's IRAM1 gives the
    # real 256 KB and must replace it, not sit alongside it.
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_xxAA"]
    rams = [m for m in dev.memories if cmsis_pack._is_ram(m)]
    assert [m["size"] for m in rams] == [0x40000]


def test_inherited_memory_reaches_a_device_that_does_not_redeclare_it():
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_NOALGO"]
    assert dev.ram_region() == (0x20000000, 0x1000)   # the family's


def test_algorithms_do_not_leak_between_siblings():
    devices = cmsis_pack.parse_pdsc(_PDSC)
    assert len(devices["TESTDEV_xxAA"].algorithms) == 2
    assert devices["TESTDEV_NOALGO"].algorithms == []


def test_flash_regions_come_from_rom_memories():
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_xxAA"]
    assert dev.flash_regions() == [(0x08000000, 0x100000)]


def test_access_string_classifies_memory_when_there_is_no_id():
    devices = cmsis_pack.parse_pdsc("""<package><vendor>V</vendor><devices>
      <family Dfamily="F"><device Dname="D">
        <memory name="ram" access="rw" start="0x20000000" size="0x1000"/>
        <memory name="rom" access="rx" start="0x0" size="0x1000"/>
        <algorithm name="a.FLM" start="0x0" size="0x1000"/>
      </device></family></devices></package>""")
    dev = devices["D"]
    assert dev.ram_region() == (0x20000000, 0x1000)
    assert dev.flash_regions() == [(0x0, 0x1000)]


def test_rejects_malformed_pdsc():
    with pytest.raises(cmsis_pack.PackError, match="not valid XML"):
        cmsis_pack.parse_pdsc("<package><unclosed>")


# ── algorithm and RAM selection ──────────────────────────────────────────

def test_default_algorithm_is_preferred():
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_xxAA"]
    assert dev.flash_algorithm()["file"] == "Flash/main.FLM"


def test_algorithm_covering_the_address_wins_over_the_default():
    # Flashing the UICR region must pick the UICR algorithm, not the default.
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_xxAA"]
    assert dev.flash_algorithm(0x10001000)["file"] == "Flash/uicr.FLM"


def test_address_outside_every_region_falls_back_to_the_default():
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_xxAA"]
    assert dev.flash_algorithm(0xF0000000)["file"] == "Flash/main.FLM"


def test_named_algorithm_wins_over_default_and_address():
    # A board whose external flash is not the pack default names its algorithm;
    # both cover the same address, so neither addr nor default can pick it.
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_xxAA"]
    assert dev.flash_algorithm(0x0, name="UICR")["file"] == "Flash/uicr.FLM"


def test_unknown_algorithm_name_lists_the_real_ones():
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_xxAA"]
    with pytest.raises(cmsis_pack.PackError, match="it has: uicr, main"):
        dev.flash_algorithm(name="hyper")


def test_algorithm_ram_bounds_win_over_the_device_ram():
    # The vendor sized RAMstart/RAMsize so the algorithm does not collide with
    # whatever else the part needs; that must beat the larger device region.
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_xxAA"]
    algo = dev.flash_algorithm()
    assert dev.ram_region(algo) == (0x20000000, 0x8000)


def test_device_ram_used_when_the_algorithm_declares_none():
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_xxAA"]
    uicr = dev.flash_algorithm(0x10001000)
    assert dev.ram_region(uicr) == (0x20000000, 0x40000)   # the default IRAM1


def test_missing_ram_is_an_error():
    dev = cmsis_pack.parse_pdsc("""<package><vendor>V</vendor><devices>
      <family Dfamily="F"><device Dname="D">
        <memory id="IROM1" start="0x0" size="0x1000" default="1"/>
        <algorithm name="a.FLM" start="0x0" size="0x1000"/>
      </device></family></devices></package>""")["D"]
    with pytest.raises(cmsis_pack.PackError, match="no RAM region"):
        dev.ram_region()


def test_missing_algorithm_is_an_error():
    dev = cmsis_pack.parse_pdsc(_PDSC)["TESTDEV_NOALGO"]
    with pytest.raises(cmsis_pack.PackError, match="no flash algorithm"):
        dev.flash_algorithm()


# ── pack container ───────────────────────────────────────────────────────

def test_reads_members_despite_windows_separators_and_case(pack_path):
    with cmsis_pack.Pack(pack_path) as pack:
        assert pack.read("Flash\\main.FLM")[:4] == b"\x7fELF"
        assert pack.read("flash/MAIN.flm")[:4] == b"\x7fELF"


def test_unknown_member_is_an_error(pack_path):
    with cmsis_pack.Pack(pack_path) as pack:
        with pytest.raises(cmsis_pack.PackError, match="is not in"):
            pack.read("Flash/nope.FLM")


def test_device_lookup_is_case_insensitive(pack_path):
    with cmsis_pack.Pack(pack_path) as pack:
        assert pack.device("testdev_xxaa").name == "TESTDEV_xxAA"


def test_unknown_device_error_lists_what_the_pack_has(pack_path):
    with cmsis_pack.Pack(pack_path) as pack:
        with pytest.raises(cmsis_pack.PackError, match="TESTDEV_xxAA"):
            pack.device("nRF52840_xxAA")


def test_rejects_a_non_zip(tmp_path):
    bogus = tmp_path / "broken.pack"
    bogus.write_bytes(b"not a zip")
    with pytest.raises(cmsis_pack.PackError, match="not a readable"):
        cmsis_pack.Pack(bogus)


def test_rejects_a_zip_with_no_pdsc(tmp_path):
    path = tmp_path / "empty.pack"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("readme.txt", "nothing here")
    with pytest.raises(cmsis_pack.PackError, match="no .pdsc"):
        cmsis_pack.Pack(path)


# ── cache search ─────────────────────────────────────────────────────────

def test_find_device_searches_the_cache(pack_path):
    pack, info = cmsis_pack.find_device("TESTDEV_xxAA",
                                        cache=pack_path.parent)
    try:
        assert info.name == "TESTDEV_xxAA"
    finally:
        pack.close()


def test_find_device_skips_unreadable_packs(tmp_path, pack_path):
    (tmp_path / "aaa.broken.pack").write_bytes(b"not a zip")
    pack, info = cmsis_pack.find_device("TESTDEV_xxAA", cache=tmp_path)
    try:
        assert info.name == "TESTDEV_xxAA"
    finally:
        pack.close()


def test_empty_cache_says_so(tmp_path):
    with pytest.raises(cmsis_pack.PackError, match="no CMSIS packs cached"):
        cmsis_pack.find_device("TESTDEV_xxAA", cache=tmp_path)


def test_device_absent_from_every_cached_pack(pack_path):
    with pytest.raises(cmsis_pack.PackError, match="no cached pack describes"):
        cmsis_pack.find_device("STM32F407VG", cache=pack_path.parent)


def test_cache_dir_honours_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ANNEALAGE_POD_PACK_CACHE", str(tmp_path / "packs"))
    assert cmsis_pack.cache_dir() == tmp_path / "packs"


# ── end to end (offline) ─────────────────────────────────────────────────

def test_algo_for_device_from_a_cached_pack(pack_path):
    algo = cmsis_pack.algo_for_device("TESTDEV_xxAA", cache=pack_path.parent)
    assert algo["name"] == "TESTDEV_xxAA"
    assert algo["instructions"][:2] == b"\x00\xbe"
    # Laid out in the algorithm's declared RAM window, not the device's.
    assert algo["load_address"] == 0x20000000
    assert algo["begin_data"] < 0x20008000
    assert algo["flash_base"] == 0x08000000
    assert algo["sectors"] == [(0x0, 0x4000), (0x10000, 0x10000)]


def test_algo_for_device_honours_an_explicit_pack_path(pack_path):
    algo = cmsis_pack.algo_for_device("TESTDEV_xxAA", pack=pack_path)
    assert algo["name"] == "TESTDEV_xxAA"


def test_algo_for_device_selects_by_address(pack_path):
    algo = cmsis_pack.algo_for_device("TESTDEV_xxAA", pack=pack_path,
                                      addr=0x10001000)
    assert algo["flash_base"] == 0x10001000
    assert algo["page_size"] == 0x100


def test_bare_flm_needs_an_explicit_ram_window(tmp_path):
    path = tmp_path / "bare.FLM"
    path.write_bytes(_build_flm())
    with pytest.raises(cmsis_pack.PackError, match="carries no RAM"):
        cmsis_pack.algo_for_device("TESTDEV_xxAA", pack=path)


def test_bare_flm_with_a_ram_window(tmp_path):
    path = tmp_path / "bare.FLM"
    path.write_bytes(_build_flm())
    algo = cmsis_pack.algo_for_device("TESTDEV_xxAA", pack=path,
                                      ram=(0x20000000, 0x10000))
    assert algo["load_address"] == 0x20000000
    assert algo["name"] == "TESTDEV_xxAA"


def test_download_is_not_attempted_unless_allowed(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(cmsis_pack, "download_pack", _boom)
    monkeypatch.setattr(cmsis_pack, "index_entries", _boom)
    with pytest.raises(cmsis_pack.PackError):
        cmsis_pack.algo_for_device("TESTDEV_xxAA", cache=tmp_path)


def test_download_is_used_when_allowed_and_nothing_is_cached(
        tmp_path, pack_path, monkeypatch):
    empty = tmp_path / "empty-cache"      # pack_path lives in tmp_path itself
    empty.mkdir()
    calls = []

    def _fake_download(vendor, name, version=None, cache=None, **kw):
        calls.append((vendor, name))
        return pack_path

    monkeypatch.setattr(cmsis_pack, "download_pack", _fake_download)
    algo = cmsis_pack.algo_for_device(
        "TESTDEV_xxAA", cache=empty, allow_download=True,
        vendor="TestVendor", pack_name="TestSeries_DFP")
    assert calls == [("TestVendor", "TestSeries_DFP")]
    assert algo["name"] == "TESTDEV_xxAA"


def test_download_needs_a_vendor_and_pack_name(tmp_path, monkeypatch):
    monkeypatch.setattr(cmsis_pack, "download_pack",
                        lambda *a, **k: pytest.fail("no pack named"))
    with pytest.raises(cmsis_pack.PackError, match="no CMSIS packs cached"):
        cmsis_pack.algo_for_device("TESTDEV_xxAA", cache=tmp_path,
                                   allow_download=True)
