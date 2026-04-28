# Annealage Pod I2C/SPI slave personality stubs.
#
# Phase 1: signatures only. Phase 2 wires the C `slaveio` module's
# register-table responder to these classes. I2C and SPI are mutually
# exclusive on the shared translator pins (spec.md §4.8).


class _I2CSlave:
    def start(self, addr=0x42, read_buf_size=256, write_buf_size=256):
        raise NotImplementedError("slave.i2c.start() not implemented in Phase 1")

    def read_table(self):
        raise NotImplementedError("slave.i2c.read_table() not implemented in Phase 1")

    def write_table(self):
        raise NotImplementedError("slave.i2c.write_table() not implemented in Phase 1")

    def on_write(self, start, end, callback):
        raise NotImplementedError("slave.i2c.on_write() not implemented in Phase 1")


class _SPISlave:
    def start(self, mode=0, freq_max=10_000_000):
        raise NotImplementedError("slave.spi.start() not implemented in Phase 1")


i2c = _I2CSlave()
spi = _SPISlave()
