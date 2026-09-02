# Annealage Pod: I2C/SPI slave personality MicroPython wrappers.
#
# Thin wrappers over the C `slaveio` module. The hardware-driven
# register-table responder lives in C (spec.md §4.8); the MicroPython
# side just exposes start/read_table/write_table/on_write so test
# scripts can drive personalities without dropping into C.
#
# I2C-slave and SPI-slave personalities are mutually exclusive on the
# shared translator pins. Activating one disarms the other.
#
# Note: the C slaveio module currently exposes only `start()`. Wider
# API (i2c.read_table, spi.start, on_write, ...) will land with WS-F.
# Until then these methods invoke start() once and raise
# NotImplementedError so tests get a clear signal rather than silently
# returning empty data.


def _try_import():
    try:
        import slaveio  # type: ignore

        return slaveio
    except ImportError:
        return None


_started = False


def _ensure_started():
    global _started
    mod = _try_import()
    if mod is None:
        return None
    if not _started:
        start = getattr(mod, "start", None)
        if start is not None:
            start()
        _started = True
    return mod


def _gap(name):
    raise NotImplementedError(
        "annealage_pod.slave.{}: WS-F C surface not yet exposed; "
        "see plan/phase-2-parallel-implementation.md".format(name)
    )


class _I2CSlave:
    """I2C-slave personality control."""

    def start(self, addr=0x42, read_buf_size=256, write_buf_size=256):
        """Activate the I2C-slave personality at `addr` with the given table sizes."""
        mod = _ensure_started()
        if mod is None:
            return False
        i2c_start = getattr(mod, "i2c_start", None)
        if i2c_start is None:
            # TODO(WS-F): slaveio.i2c_start(addr, read_size, write_size)
            _gap("i2c.start")
        i2c_start(addr, read_buf_size, write_buf_size)
        return True

    def read_table(self):
        """Return the read-table buffer (DUT reads from this)."""
        mod = _ensure_started()
        if mod is None:
            return b""
        rt = getattr(mod, "i2c_read_table", None)
        if rt is None:
            _gap("i2c.read_table")
        return rt()

    def write_table(self):
        """Return the write-table buffer (DUT writes into this)."""
        mod = _ensure_started()
        if mod is None:
            return b""
        wt = getattr(mod, "i2c_write_table", None)
        if wt is None:
            _gap("i2c.write_table")
        return wt()

    def on_write(self, start, end, callback):
        """Register a callback fired (non-ISR) on writes to [start, end)."""
        mod = _ensure_started()
        if mod is None:
            return False
        ow = getattr(mod, "i2c_on_write", None)
        if ow is None:
            _gap("i2c.on_write")
        ow(start, end, callback)
        return True


class _SPISlave:
    """SPI-slave personality control."""

    def start(self, mode=0, freq_max=10_000_000, read_buf_size=256, write_buf_size=256):
        """Activate the SPI-slave personality with the given mode and clock cap."""
        mod = _ensure_started()
        if mod is None:
            return False
        spi_start = getattr(mod, "spi_start", None)
        if spi_start is None:
            _gap("spi.start")
        spi_start(mode, freq_max, read_buf_size, write_buf_size)
        return True

    def read_table(self):
        """Return the read-table buffer."""
        mod = _ensure_started()
        if mod is None:
            return b""
        rt = getattr(mod, "spi_read_table", None)
        if rt is None:
            _gap("spi.read_table")
        return rt()

    def write_table(self):
        """Return the write-table buffer."""
        mod = _ensure_started()
        if mod is None:
            return b""
        wt = getattr(mod, "spi_write_table", None)
        if wt is None:
            _gap("spi.write_table")
        return wt()

    def on_read(self, start, end, callback):
        """Register a callback fired (non-ISR) on reads from [start, end)."""
        mod = _ensure_started()
        if mod is None:
            return False
        orcb = getattr(mod, "spi_on_read", None)
        if orcb is None:
            _gap("spi.on_read")
        orcb(start, end, callback)
        return True


i2c = _I2CSlave()
spi = _SPISlave()
