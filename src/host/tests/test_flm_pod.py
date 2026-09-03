"""Host-side unit tests for the on-pod generic CMSIS FLM runner
(annealage_pod.debug.flm).

flm imports swd_dap for the DHCSR bit constants, and swd_dap pulls in
machine/rp2, so it is stubbed in sys.modules with the handful of constants flm
needs (values mirror the real swd_dap). The tests here cover the parts that are
pure logic and were otherwise only reachable on hardware: the blob-word
conversion, the CMSIS erase-sector map (uniform and non-uniform devices), and
load() idempotency. The algorithm-call path (_call) needs MicroPython's
time.ticks_ms and a live target, so it is not exercised.
"""

import os
import sys
import types

import pytest

_MPY = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mpy"))
if _MPY not in sys.path:
    sys.path.insert(0, _MPY)

# Share the stub with test_ops_swd (whichever module imports first creates it);
# set the constants unconditionally so import order does not matter.
_swd_dap = sys.modules.setdefault(
    "annealage_pod.debug.swd_dap", types.ModuleType("annealage_pod.debug.swd_dap"))
_swd_dap.DHCSR = 0xE000EDF0
_swd_dap.DBGKEY = 0xA05F << 16
_swd_dap.C_DEBUGEN = 1 << 0
_swd_dap.C_HALT = 1 << 1
_swd_dap.C_MASKINTS = 1 << 3
_swd_dap.S_HALT = 1 << 17

import annealage_pod.debug.flm as flm          # noqa: E402


class _FakeAP:
    def __init__(self):
        self.blocks = []

    def write_block32(self, addr, words):
        self.blocks.append((addr, list(words)))

    def write32(self, addr, value):
        pass


class _FakeCM:
    def __init__(self, halted=True):
        self._halted = halted
        self.halts = 0

    def is_halted(self):
        return self._halted

    def halt(self):
        self.halts += 1
        self._halted = True


# nRF52840-shaped: uniform 4 KB sectors over 1 MB at 0.
_UNIFORM = {
    "instructions": b"\x00\xbe\x00\xbe",
    "load_address": 0x20000000,
    "static_base": 0x20000400,
    "begin_stack": 0x20001000,
    "begin_data": 0x20002000,
    "pc_init": 0x20000021,
    "pc_unInit": 0x20000041,
    "pc_erase_sector": 0x20000061,
    "pc_program_page": 0x20000081,
    "flash_base": 0x00000000,
    "flash_size": 0x00100000,
    "page_size": 0x1000,
}

# STM32F4-shaped: 4x16K, 1x64K, then 128K sectors - the case a uniform
# page_size sweep gets wrong.
_NON_UNIFORM = dict(
    _UNIFORM,
    flash_base=0x08000000,
    flash_size=0x00100000,
    page_size=0x4000,
    sectors=[(0x00000, 0x4000), (0x10000, 0x10000), (0x20000, 0x20000)],
)


def _flasher(algo):
    return flm.FLMFlasher(_FakeAP(), _FakeCM(), algo)


# ── blob conversion ──────────────────────────────────────────────────────

def test_to_words_from_bytes_little_endian():
    assert flm._to_words(b"\x01\x02\x03\x04\xff\x00\x00\x00") == [0x04030201, 0xff]


def test_to_words_pads_short_tail():
    # A blob whose length is not a multiple of 4 is zero-padded, not truncated.
    assert flm._to_words(b"\x01\x02\x03") == [0x00030201]


def test_to_words_passes_through_word_sequence():
    assert flm._to_words([1, 2, 3]) == [1, 2, 3]


# ── sector map ───────────────────────────────────────────────────────────

def test_uniform_sector_map_defaults_to_page_size():
    f = _flasher(_UNIFORM)
    assert f.sectors == [(0, 0x1000)]
    assert f.sector_size(0x0) == 0x1000
    assert f.sector_size(0xF8123) == 0x1000
    assert f.sector_base(0xF8123) == 0xF8000


@pytest.mark.parametrize("addr,size,base", [
    (0x08000000, 0x4000, 0x08000000),     # first 16K sector
    (0x08003FFF, 0x4000, 0x08000000),     # last byte of it
    (0x08004000, 0x4000, 0x08004000),     # second 16K sector
    (0x08010000, 0x10000, 0x08010000),    # the 64K sector
    (0x0801FFFF, 0x10000, 0x08010000),
    (0x08020000, 0x20000, 0x08020000),    # first 128K sector
    (0x08051234, 0x20000, 0x08040000),    # inside the second 128K sector
])
def test_non_uniform_sector_geometry(addr, size, base):
    f = _flasher(_NON_UNIFORM)
    assert f.sector_size(addr) == size
    assert f.sector_base(addr) == base


def test_sector_lookup_rejects_addr_below_flash_base():
    f = _flasher(_NON_UNIFORM)
    with pytest.raises(flm.FLMError):
        f.sector_size(0x07FFFFFF)


def test_erase_range_walks_non_uniform_sectors(monkeypatch):
    # A span crossing the 16K -> 64K -> 128K boundaries must step by each
    # region's own sector size; a page_size sweep would erase the same 64K and
    # 128K sectors repeatedly and stop short.
    f = _flasher(_NON_UNIFORM)
    erased = []
    monkeypatch.setattr(f, "erase_sector", erased.append)
    f.erase_range(0x0800C000, 0x4000 + 0x10000 + 0x20000)
    assert erased == [0x0800C000, 0x08010000, 0x08020000]


def test_erase_range_starts_at_containing_sector_base():
    # An unaligned start erases from the base of the sector that contains it.
    f = _flasher(_UNIFORM)
    erased = []
    f.erase_sector = erased.append
    f.erase_range(0x1234, 0x10)
    assert erased == [0x1000]


def test_erase_range_covers_final_partial_sector():
    f = _flasher(_UNIFORM)
    erased = []
    f.erase_sector = erased.append
    f.erase_range(0x0, 0x1001)
    assert erased == [0x0, 0x1000]


# ── load / reload ────────────────────────────────────────────────────────

def test_load_is_idempotent_within_an_operation():
    # A multi-page program must not re-upload the algorithm per page.
    f = _flasher(_UNIFORM)
    f.load()
    f.load()
    f.load()
    assert len(f.ap.blocks) == 1
    assert f.ap.blocks[0][0] == 0x20000000


def test_reload_forces_a_fresh_copy():
    # Each operation ends by resuming the DUT, which runs over the load region,
    # so the next operation must re-upload rather than trust the stale copy.
    f = _flasher(_UNIFORM)
    f.load()
    f.reload()
    assert len(f.ap.blocks) == 2


def test_load_halts_a_running_core():
    f = flm.FLMFlasher(_FakeAP(), _FakeCM(halted=False), _UNIFORM)
    f.load()
    assert f.cm.halts == 1


# ── per-operation timeouts (gap 3) ───────────────────────────────────────
# _call itself needs MicroPython's time.ticks_ms (see module docstring), so
# these stand in for it to check what timeout_ms each caller passes.

class _RecordingCall:
    def __init__(self):
        self.calls = []          # [(pc, timeout_ms), ...]

    def __call__(self, pc, r0=0, r1=0, r2=0, r3=0, timeout_ms=8000):
        self.calls.append((pc, timeout_ms))
        return 0


def test_erase_sector_uses_the_algorithms_erase_timeout():
    f = _flasher(dict(_UNIFORM, timeout_erase_ms=5000))
    f._call = _RecordingCall()
    f.erase_sector(0x1000)
    assert f._call.calls == [(_UNIFORM["pc_erase_sector"], 5000)]


def test_erase_sector_falls_back_to_8s_when_the_pack_declares_none():
    f = _flasher(_UNIFORM)
    f._call = _RecordingCall()
    f.erase_sector(0x1000)
    assert f._call.calls == [(_UNIFORM["pc_erase_sector"], 8000)]


def test_program_page_uses_the_algorithms_program_timeout():
    f = _flasher(dict(_UNIFORM, timeout_prog_ms=750))
    f._call = _RecordingCall()
    f.program_page(0x1000, b"\x00" * 16)
    assert f._call.calls == [(_UNIFORM["pc_program_page"], 750)]


def test_erase_all_uses_the_erase_timeout_for_erasechip():
    algo = dict(_UNIFORM, pc_eraseAll=0x200000A1, timeout_erase_ms=9000)
    f = _flasher(algo)
    f._call = _RecordingCall()
    f.erase_all()
    assert (algo["pc_eraseAll"], 9000) in f._call.calls
