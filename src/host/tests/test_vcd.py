"""Tests for pod.vcd - logic-analyser capture decode to VCD."""

import struct
import pytest

from pod import vcd


class TestUnpack:
    def test_unpack_words(self):
        raw = struct.pack("<3I", 0x11223344, 0xAABBCCDD, 0)
        assert vcd.unpack_words(raw, 3) == [0x11223344, 0xAABBCCDD, 0]

    def test_unpack_truncates_to_words(self):
        raw = struct.pack("<4I", 1, 2, 3, 4)
        assert vcd.unpack_words(raw, 2) == [1, 2]


class TestSamplesWidth1:
    def test_alternating(self):
        # 0xAAAAAAAA = 1010... MSB-first -> [1,0,1,0,...]
        s = vcd.words_to_samples([0xAAAAAAAA], 1)
        assert len(s) == 32
        assert s[:4] == [1, 0, 1, 0]
        assert s[-1] == 0

    def test_all_high_then_low(self):
        s = vcd.words_to_samples([0xFFFFFFFF, 0x00000000], 1)
        assert s[:32] == [1] * 32
        assert s[32:] == [0] * 32

    def test_count_clamps(self):
        s = vcd.words_to_samples([0xFFFFFFFF], 1, count=5)
        assert s == [1, 1, 1, 1, 1]

    def test_run_lengths_recover_square_wave(self):
        # build a word stream that is 4-high, 4-low repeating (MSB-first)
        pat = 0b11110000111100001111000011110000
        s = vcd.words_to_samples([pat], 1)
        # runs of 4
        runs = []
        c = 1
        for i in range(1, len(s)):
            if s[i] == s[i - 1]:
                c += 1
            else:
                runs.append(c)
                c = 1
        runs.append(c)
        assert all(r == 4 for r in runs)


class TestToVcd:
    def test_structure_width1(self):
        text = vcd.to_vcd([0, 1, 1, 0], 1, 1_000_000, names=["clk"])
        assert "$timescale 1 ps $end" in text
        assert "$var wire 1 ! clk $end" in text
        assert "$enddefinitions $end" in text
        # value changes: low at 0, high at 1us, low at 3us; sample 2 == sample 1
        assert "#0" in text
        assert "0!" in text and "1!" in text
        # period 1e6 ps; rising at sample 1 -> #1000000
        assert "#1000000" in text
        assert "#3000000" in text
        # no timestamp for the unchanged sample 2 (#2000000 absent)
        assert "#2000000" not in text

    def test_default_names(self):
        text = vcd.to_vcd([0, 3, 0], 2, 1_000_000)
        assert "$var wire 1 ! ch0 $end" in text
        assert "$var wire 1 \" ch1 $end" in text

    def test_decode_to_vcd_roundtrip(self, tmp_path):
        raw = struct.pack("<2I", 0xFFFF0000, 0x0000FFFF)
        out = tmp_path / "cap.vcd"
        n = vcd.decode_to_vcd(raw, 2, 1, 1_000_000, str(out))
        assert n == 64
        text = out.read_text()
        assert "$timescale 1 ps $end" in text
        assert text.endswith("\n")
