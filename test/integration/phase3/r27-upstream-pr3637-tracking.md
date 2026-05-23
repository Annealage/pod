# R27 follow-up: upstream TinyUSB PR #3637 tracking

This note records the relationship between the four TinyUSB host-stack fixes mpy-pod carries today and the upstream PR that consolidates them.

## What's upstream

[hathach/tinyusb#3637](https://github.com/hathach/tinyusb/pull/3637) ("host: USB/IP-over-Ethernet bridge example, plus DWC2 / control_xfer fixes") proposes six commits against `hathach/tinyusb:master`:

| # | Commit | Status here |
|---|---|---|
| 1 | `hcd/dwc2: fix txfifo full check` (cherry-pick from HiFiPhile's #3632) | already in vendored TinyUSB; verified at `src/micropython/lib/tinyusb/src/portable/synopsys/dwc2/hcd_dwc2.c:931` (re-read inside per-packet loop). |
| 2 | `hcd/dwc2: save post-transfer PID in DMA-mode IN handler` | already in vendored TinyUSB; bumped via MicroPython commit `16da59b16` (`lib/tinyusb: Bump pin to include DMA-mode IN PID-save fix`). |
| 3 | `hcd/dwc2: fire xfer_complete callback after hcd_edpt_abort_xfer` | already in vendored TinyUSB; bumped via MicroPython commit `dbb5d71c7`. |
| 4 | `host: honour tuh_xfer_t.timeout_ms in tuh_control_xfer` | already in vendored TinyUSB; bumped via MicroPython commit `dbb5d71c7`. |
| 5 | `hw/bsp/stm32f4: add optional Ethernet support and LAN8742 PHY driver` | not applicable; mpy-pod uses ESP-IDF networking. |
| 6 | `examples/host/usbipd: add USB/IP-over-Ethernet host bridge` | not applicable; mpy-pod is the production-shape sibling of this example. |

Commits 1-4 affect every TinyUSB host application on DWC2 silicon (STM32 F/H/U series, ESP32-S2/S3/P4, Renesas). They are not mpy-pod-specific.

## What this means for mpy-pod

Until #3637 merges, mpy-pod carries the four fixes as cherry-picks on its MicroPython submodule pin (see commits `16da59b16`, `dbb5d71c7`). When upstream merges:

1. MicroPython's TinyUSB pin will eventually move to a ref containing the merged versions (under their final commit hashes).
2. Our cherry-picked SHAs become obsolete but the patch content is identical, so the merge is a no-op functional change.
3. Re-run the phase-4 regression bench (`test/integration/phase4-regressions/`) on the post-bump submodule to confirm none of the four fixes were silently dropped through the rename.

## Wider sharing of the work

The `examples/host/usbipd` example in #3637 is the single-board, single-device reference for the same architecture mpy-pod implements at production complexity (multiplexed real + synthetic devices, per-EP lane tasks, Wi-Fi transport). The example exists partly to share the pattern, partly to give the host-stack fixes a reproducible test target outside mpy-pod's hardware bench.

If the example doesn't merge upstream, the author (Andrew Leech) will publish it as a standalone project; the four host-stack fixes can land independently as their own PR. Either path the fixes still propagate to mpy-pod via MicroPython.

## Open items

* Verify all four fixes are still present after the next MicroPython submodule bump. Hook into the regression bench (Phase 4 tests above).
* Watch for the upstream merge and pull the merged versions into MicroPython rather than keep cherry-picks on the fork.
* If #3637 sits unmerged for >3 months, consider extracting just the four host-stack commits into a smaller upstream PR (without the example) since they have independent value.
