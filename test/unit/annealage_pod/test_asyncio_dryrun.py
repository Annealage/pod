# Phase 2 exit criterion: the annealage_pod.* package must run cleanly
# inside an asyncio loop on the Unix port. Replicates the boot path's
# coroutine pattern without any networking.

import asyncio

from annealage_pod import dut, power, relays, supervisor


async def _exercise():
    power.vtarget.off()
    power.vtarget.on()
    assert power.vtarget.is_on() is True
    relays.relays.batch(open=relays.relays.numbers())
    for n in range(1, 8):
        await asyncio.sleep(0)  # yield each iteration
        assert dut.reset(mode="relay", relay=n, pulse_ms=1) is True
    supervisor.clear_hooks()
    captured = []
    supervisor.register_cleanup(lambda: captured.append("ran"))
    supervisor.run_cleanup()
    return captured


def test_asyncio_loop():
    out = asyncio.run(_exercise())
    assert out == ["ran"]
