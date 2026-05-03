# R25 stage A: IDF v5.5.4 bump attempt

## Verdict

**Build failed. Reverted to v5.5.1. No bench possible.**

The v5.5.4 IDF tree pulled in a newer NimBLE submodule
(`b45dcedcafb7888174c3567002c36b342ec0b723`, vs v5.5.1's earlier pin)
which restructures the BLE host's global state. Several previously-
global `extern uint16_t` symbols are now macro accessors that expand
to a struct member dereference. Our pinned MicroPython fork
(`andrewleech/micropython` `dfa0adc44`, machine-usbhost branch)
references them as old-style globals in `extmod/nimble/modbluetooth_nimble.c`.

## Compile errors observed

```
nimble/host/src/ble_hs_priv.h:109:56: error: expected ')' before '->' token
  109 | #define ble_hs_max_attrs              (ble_hs_state_ctx->max_attrs)
extmod/nimble/modbluetooth_nimble.c:587:21: note: in expansion of macro 'ble_hs_max_attrs'
  587 |     extern uint16_t ble_hs_max_attrs;

nimble/host/src/ble_hs_priv.h:110:56: error: expected ')' before '->' token
  110 | #define ble_hs_max_services           (ble_hs_state_ctx->max_services)
extmod/nimble/modbluetooth_nimble.c:588:21: note: in expansion of macro 'ble_hs_max_services'
  588 |     extern uint16_t ble_hs_max_services;

nimble/host/src/ble_hs_priv.h:111:56: error: expected ')' before '->' token
  111 | #define ble_hs_max_client_configs     (ble_hs_state_ctx->max_client_configs)
extmod/nimble/modbluetooth_nimble.c:589:21: note: in expansion of macro 'ble_hs_max_client_configs'
  589 |     extern uint16_t ble_hs_max_client_configs;
```

This is an upstream MicroPython issue (not in our tree); a fix in MP
would either:
- detect the IDF v5.5.4-aligned NimBLE and use a different access
  path, or
- gate the `extern` declarations behind a NimBLE-version preprocessor
  guard.

Either way it is out of scope here (we do not modify
`src/micropython/`).

## Steps taken

1. `git -C <idf> status` clean at `fcae3288` (v5.5.1).
2. `git -C <idf> checkout v5.5.4` → `735507283d`. Submodule SHAs
   shifted; `git submodule update --init --recursive` resyncs them.
3. `bash install.sh esp32s3` to update Python deps (added
   `tree_sitter`, `tree_sitter_c` for BLE log testing) and toolchain
   (xtensa-esp-elf-gdb 16.2 → 16.3, esptool 4.11 → 4.12, etc.).
4. `rm -rf build-ESP32_S3_ANNEALAGE_POD` for clean configure.
5. `bash src/tools/build.sh` failed at the `modbluetooth_nimble.c`
   compile step (errors above).

## Revert

```
git -C /home/corona/cyd/lvgl-micropython-ref/lvgl_micropython/lib/esp-idf checkout fcae3288
git -C ... submodule update --init --recursive --force
bash install.sh esp32s3   # restore v5.5.1 toolchain
rm -rf src/micropython/ports/esp32/build-ESP32_S3_ANNEALAGE_POD
bash src/tools/build.sh   # rebuild on v5.5.1, clean pass
```

The openthread submodule needed `git clean -fdx` inside it to remove
v5.5.4-era third_party content before the submodule update could
checkout the v5.5.1-pinned SHA. Recorded for any future bump.

## USB-host commit list (v5.5.1 → v5.5.4) for reference

`git log --oneline --no-merges v5.5.1..v5.5.4 -- components/usb/ components/hal/usb_dwc_hal.c components/hal/include/hal/usb_dwc_hal.h` returns these commits, none of which read as bulk-IN throughput fixes:

- `33794f6b52` feat(esp_hw_support): set USB2.0 phy to suspend mode
  at startup for active power saving (P4-only, doesn't apply to S3
  bulk-IN).
- `63f020f8e2` fix(host/usb): Fixed deadlock that prevented closing
  devices from high priority tasks (interesting given our priority
  20 worker, but this is the close path, not the steady-state read
  path).
- `6e972acc98` feat(esp_hal_usb): Add remote wakeup support (feature,
  not a fix).
- `a032b61bd0` feat(usb_host): Add power and clock gating LL, HAL
  (P4-related).
- `14a3d623b3` feat(usb_host): Add hal check for the global root
  port suspend (P4-related).
- `94f81da483 / 39be672f8e / ab5e48b026 / d0e0c188fb` P4 device/host
  support and FIFO config (P4-related).
- `99e5203d89 / c48b74805f` ext_hub state machine fixes (multi-hub
  topology only).
- `9cf4ddf797` deepsleep leakage on HS-PHY init (P4 HS only).

So even if the build had worked, none of the USB diff was a
plausible fix for our bulk-IN ~165 ms `avg_round` issue. The bump was
worth trying as a free experiment but the cost ended up non-zero
(install.sh roundtrip + revert) without any throughput data.

## Implications for stage B

- Stage B (instrument `_intr_hdlr_chan` directly) remains the only
  remaining lever short of moving to ESP32-P4 / TinyUSB / custom HAL.
- Patching v5.5.1 IDF for stage B is fine; no need to bump first.
- Independently, getting MP to compile against newer IDF NimBLE is
  worth filing as a separate upstream-MP issue if we ever want
  v5.5.4+ on this branch. Out of scope here.

## Files unchanged in our tree

The IDF tree and our build artifacts are restored to pre-attempt
state. No commits in our repo mutate code. This findings file is the
only artefact of stage A. `src/VERSIONS` left at v5.5.1 / `fcae3288`.
