# Phase 3: GDB server and debug control

Workstream D. Make the pod a full debug target for host tools, not just a flasher.

Goal: a host `gdb` (and pyOCD/probe-rs where they speak GDB-remote) can halt,
step, set breakpoints, and read/write memory and registers on a DUT through the
pod, over the network.

## Dependencies

- D2 (DP/AP/MEM-AP, halt/run/memory primitives).

## Tasks

### D3.1 Debug control primitives
- Core control via the debug registers: halt/resume/step (DHCSR), reset
  (AIRCR.SYSRESETREQ / VECTRESET), DEMCR vector-catch, register file read/write
  (DCRSR/DCRDR), hardware breakpoints (FPB) and watchpoints (DWT).

### D3.2 GDB server (implemented from the ARM/GDB specs)
- Implement a GDB-remote (RSP) server to MicroPython: RSP packet framing, the core
  command set (`?`, `g`/`G`, `m`/`M`, `c`, `s`, `Z`/`z`, `qSupported`,
  vCont, memory-map and target XML), served over a TCP socket on the pod.
- Trim to the subset `pico_debug`'s server implements where a full desktop GDB server is too heavy
  for RAM; add features lazily.
- Reuse the memory cache idea from `pico_debug` (coalesce GDB's many small reads)
  to keep stepping responsive over Wi-Fi.

### D3.3 Reset paths
- Implement the reset paths from the S3 spec §3.4 addressable from the MP API:
  `swd` (AIRCR via the on-pod probe) and `nrst` (open-drain GPIO to the DUT reset
  line) on the bare Pico 2 W; `power` (rail switch) gated on custom carrier
  hardware. (The S3 spec's `relay` path is dropped from this design.)

### D3.4 Validate
- A host `gdb` extended-remote (or pyOCD/probe-rs GDB path) session over the
  network: connect, halt, breakpoint, step, inspect memory/registers, continue,
  reset.

## Deliverables

- `gdb_server` MicroPython module + debug-control layer.
- Reset-path API.
- A working remote gdb session against a DUT through the pod.

## Exit gate

A host debugger drives a DUT through the pod over the network: halt, step,
breakpoint, memory/register access, reset. Step-over latency is usable over
Wi-Fi.

## Risks

- R5 (GDB server RAM footprint): trim to the working subset; lazy features.
- R6 (Wi-Fi jitter on stepping): the memory-cache coalescing mitigates; measure.

## References

- pyOCD `gdbserver`; `github.com/essele/pico_debug` (`gdb.c`, memory cache)
- S3 spec §3.4 (reset paths)
