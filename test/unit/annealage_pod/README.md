# annealage_pod.* unit tests

WS-E unit-test suite. Exercises the MP-side surface added in Phase 2
without requiring a board.

## Running

CPython (fastest, used in CI):

    python3 -m pytest test/unit/annealage_pod/

MicroPython Unix port:

    cd src/micropython/ports/unix && make submodules && make
    MICROPYPATH=".frozen:../../src/mpy" \
        ./build-standard/micropython -c "import test_relays; ..."

The CPython path uses `conftest.py` to prepend `src/mpy/` to
`sys.path`. The MicroPython path needs `MICROPYPATH` set explicitly.

## Coverage

| File                        | Surface                                                      |
|-----------------------------|--------------------------------------------------------------|
| `test_imports.py`           | every submodule + `_pinmap` constants                        |
| `test_relays.py`            | `annealage_pod.relays.relays.{set,get,batch,pulse,all_off}`       |
| `test_power.py`             | `annealage_pod.power.{vtarget,dut_usb}.{on,off,cycle,is_on,...}`  |
| `test_dut_reset.py`         | `annealage_pod.dut.reset(mode='swd'|'nrst'|'power'|'relay')`      |
| `test_compat_shim.py`       | RP_INFRA shim incl. `get_relays` bug fix and strict-mode     |
| `test_supervisor.py`        | cleanup-hook registration / ordering / exception-swallowing  |
| `test_asyncio_dryrun.py`    | every primitive callable from inside `asyncio.run()`         |
