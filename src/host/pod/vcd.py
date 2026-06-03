"""Decode the pod logic analyser's packed capture to VCD.

The pod streams a capture as little-endian 32-bit words (see
annealage_pod.debug.ops.la_stream). The PIO samples `width` contiguous pins once
per clock and packs MSB-first per word (SHIFT_LEFT, autopush at 32): reading each
word from bit 31 down to bit 0 yields the chronological bit stream, grouped into
`width`-bit samples. Within a sample, channel k (pin base+k) is bit k of the
sample value.

VCD (Value Change Dump) opens in GTKWave, PulseView/sigrok, and most viewers.
"""

import struct


def unpack_words(raw: bytes, words: int) -> list:
    """Unpack `words` little-endian uint32 from raw bytes."""
    return list(struct.unpack("<%dI" % words, raw[:words * 4]))


def words_to_samples(words, width: int, count=None) -> list:
    """Convert packed 32-bit words to a list of `width`-bit sample values.

    Channel k is bit k of each returned sample (pin base+k).
    """
    bits = []
    for w in words:
        for i in range(31, -1, -1):
            bits.append((w >> i) & 1)
    nsamp = len(bits) // width
    if count is not None:
        nsamp = min(nsamp, count)
    samples = []
    for s in range(nsamp):
        grp = bits[s * width:(s + 1) * width]   # MSB-first: grp[0] = channel width-1
        v = 0
        for b in grp:
            v = (v << 1) | b
        samples.append(v)
    return samples


def to_vcd(samples, width: int, rate: float, names=None) -> str:
    """Emit a VCD string for `samples` (each a width-bit int) at `rate` Hz.

    Time is in picoseconds (`$timescale 1 ps`); each sample advances by the
    rounded sample period, so the viewer timebase matches the capture rate.
    """
    if names is None:
        names = ["ch%d" % i for i in range(width)]
    period_ps = max(1, round(1e12 / rate))
    ids = [chr(33 + i) for i in range(width)]   # printable VCD id chars: ! " # ...

    out = []
    out.append("$comment Annealage Pod logic analyser, %d ch @ %.6g Hz $end"
               % (width, rate))
    out.append("$timescale 1 ps $end")
    out.append("$scope module la $end")
    for i in range(width):
        out.append("$var wire 1 %s %s $end" % (ids[i], names[i]))
    out.append("$upscope $end")
    out.append("$enddefinitions $end")

    prev = None
    for s, v in enumerate(samples):
        changes = []
        for i in range(width):
            bit = (v >> i) & 1
            if prev is None or ((prev >> i) & 1) != bit:
                changes.append("%d%s" % (bit, ids[i]))
        if changes:
            out.append("#%d" % (s * period_ps))
            out.extend(changes)
        prev = v
    return "\n".join(out) + "\n"


def decode_to_vcd(raw: bytes, words: int, width: int, rate: float,
                  out_path: str, names=None, count=None) -> int:
    """Unpack a raw capture, write VCD to `out_path`, return the sample count."""
    samples = words_to_samples(unpack_words(raw, words), width, count=count)
    with open(out_path, "w") as f:
        f.write(to_vcd(samples, width, rate, names=names))
    return len(samples)
