# annealage_pod.* unit tests

Host-side tests for the pod's MicroPython package, no board needed.

    python3 -m pytest test/unit/annealage_pod/

`conftest.py` prepends `src/mpy/` to `sys.path`.

| File                        | Covers                                                        |
|-----------------------------|---------------------------------------------------------------|
| `test_imports.py`           | the package imports without a board build                     |
| `test_netboot_nrst_pin.py`  | netboot's copy of the DUT reset pin matches `_rp2_pinmap`     |
| `test_netutil.py`           | `debug.netutil` send/recv retry loops under backpressure      |
| `test_pio_arbiter.py`       | the PIO arbiter's per-(block, SM) claim model                 |
