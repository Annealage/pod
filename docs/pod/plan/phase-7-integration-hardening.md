# Phase 7: Integration, CI, self-update, hardening

All workstreams. Make the RP2350 pod a dependable, updatable, tested unit.

Goal: testbed_micropython runs against a pod; a reliability run passes; the pod
self-updates without physical access.

## Dependencies

- Phases 1-6 (a feature-complete pod and host tooling).

## Tasks

### 7.1 testbed_micropython integration
- Wire a transport adapter so octoprobe/testbed_micropython drives the pod over
  the `pod` tooling / RP_INFRA-compatible API (S3 spec appendix B). Run a real
  test session against a DUT through the pod.

### 7.2 CI and integration benches
- Port/extend the S3 integration benches (`test/integration/`) to the RP2350: a
  host-stack regression and a debug/flash regression. Add to CI where hardware is
  available; keep buildable-without-hardware unit tests for the host tooling.

### 7.3 Pod self-update (open decision 3)
- Choose and implement the self-update path: A/B flash partitions with a
  bootloader shim, update-over-`mount`, or BOOTSEL-assisted recovery. The
  `pico_debug`/probe-rs flatten-and-flash recipe (`dev-notes.md` §2) covers
  probe-assisted recovery; field update should not require the wired probe.
- Validate roll-forward and recovery from a bad image.

### 7.4 Reliability and hardening
- Watchdog coverage (per-task / main-loop), supervisor cleanup correctness.
- Reliability run: sustained flash/debug + USB/IP + telemetry over Wi-Fi; record
  failure modes (Wi-Fi disconnects, RTT spikes), confirm the on-pod-probe design
  keeps SWD off the network hot path under load.
- Trust-model documentation (carry the S3 spec §5.5 unauthenticated-network
  posture; recommend isolated lab VLAN).

## Deliverables

- A testbed_micropython session driven through a pod.
- RP2350 integration benches in CI.
- A working, validated self-update path.
- A reliability report and hardening checklist.

## Exit gate

testbed_micropython runs a real DUT test session through the pod; the reliability
run passes its thresholds; the pod self-updates and recovers from a bad image
without the wired probe.

## References

- `docs/esp32-s3/spec.md` §6 (logging/watchdog/time), §5.5 (trust model)
- `test/integration/` (S3 benches to port)
- `docs/pod/dev-notes.md` §2 (probe-assisted flash/recovery)
