# Pure ring-buffer index math for the SPI-target MOSI capture ring, split out
# so it is importable and unit-testable under CPython (spi_target.py itself
# imports rp2/machine and cannot be loaded off-target).


def ring_order(bx, size, wp):
    """Chronological entry indices into a `size`-entry overwrite ring.

    `bx` bytes have been captured; `wp` is the ring write-cursor, in entries.
    Before the ring first wraps (bx <= size) the live data is [0, bx). After a
    wrap the oldest live entry is at `wp` and the window wraps modulo `size`.
    Pure integer math (no hardware) so it unit-tests off-target, which is the
    only coverage the capture path gets outside the bench.
    """
    if bx <= size:
        return list(range(bx))
    return [(wp + i) % size for i in range(size)]
