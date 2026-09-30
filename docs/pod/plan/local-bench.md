# Local bench: pod tooling for dev boards with a built-in probe

Status: built. Validated on hardware: pyOCD identify/flash (bin and ELF)/erase/reset/halt/regs/mem/gdb and the host lock on a NUCLEO-H563ZI, `dut_exec` and UART tail over its host ttys, pod-a and pod-b identify/reset through the backend split, and pod-b's GPIO borrowed from the Nucleo entry. Not validated: capturing the Nucleo's SPI with pod-b's logic analyser (needs wiring between them), and moving pod-b's own UART to a host tty.

## Goal

Make the host `pod` CLI and MCP surface usable against a DUT that isn't behind a pod, e.g. a NUCLEO-H563ZI with its on-board ST-LINK, or any dev board with an SWD programmer, while keeping pod registration exactly as it is today.

## Model: a bench is a DUT plus per-channel routes

A registry entry describes a DUT and, per channel, how the host reaches it. A pod is one possible route, not the entry's type.

| Channel | Routes |
|---|---|
| `debug` (identify, flash, erase, reset, halt/resume, mem, regs, gdb) | `pod` (on-pod SWD stack), `pyocd` (host probe by uid) |
| `uart` | `pod` (TCP bridge), `tty` (`/dev/serial/by-id/...`) |
| `dut.usb` | `pod` (usbip forward), `agent-direct` (existing) |
| `instruments` (gpio, adc, LA, I2C/SPI targets) | `pod` (this entry's pod, or a named other pod), none |

Any route not declared defaults to `pod`, so existing entries are unchanged and need no migration. Pod-backed channels still use the entry's own pod handles (hostname, addrs, fingerprint).

Examples:

```json
"pod-b": {
  "hostname": "annealage-pod-8b97a.local", "fingerprint": "8e495826ee18b97a",
  "uart": {"via": "tty", "tty": "/dev/serial/by-id/usb-alelec_USB2CAN_997ee163d2a92530-if00"},
  "dut": {"target_family": "MIMXRT1052xxxxB", "usb": {"conn": "agent-direct"}}
}

"nucleo-h5": {
  "debug": {"via": "pyocd", "uid": "<stlink serial>"},
  "uart":  {"via": "tty", "tty": "/dev/serial/by-id/usb-STMicroelectronics_STLINK-V3_<serial>-if02", "baud": 115200},
  "dut":   {"target_family": "STM32H563ZITx", "flash_base": 134217728, "flash_size": 2097152,
            "usb": {"conn": "agent-direct", "tty": "/dev/serial/by-id/usb-MicroPython_..."}},
  "instruments": {"via": "pod", "pod": "pod-b"}
}
```

An entry with no pod handles and no pod-routed channel is a local bench; `pod_*` tools (exec, info, repl, mount) refuse it with an explicit error.

## Debug backend

The `dut_*` operations move behind a `DebugBackend` interface:

- `PodBackend`: the current `Pod` code, moved without behaviour change.
- `PyocdBackend`: pyOCD in-process (`ConnectHelper.session_with_chosen_probe(unique_id=...)`), target from `dut.target_family` via the CMSIS pack cache already used by `cmsis_pack.py`, `flash_algorithm` honoured when set. gdb uses pyOCD's own gdbserver.

pyOCD is a default dependency of the host package, so a new user with only a dev board gets a working bench from a plain install, with no pod and no extras.

Results carry the backend name on flash, erase and reset (`backend`, plus `pack` from pyOCD). The two flash paths differ (our FLM runner vs pyOCD's loader), so failures must be attributable.

Unavailable operations (e.g. `bench_la` with no instruments route) return a "this bench has no X" error, not a missing tool.

## Pack cache: one cache, owned by `cmsis_pack`

Both backends resolve targets and flash algorithms from the same local pack cache, the one `pod.cmsis_pack` already owns (`$ANNEALAGE_POD_PACK_CACHE`, else `$XDG_CACHE_HOME/annealage-pod/cmsis-packs`, else `~/.cache/annealage-pod/cmsis-packs`). pyOCD's own pack index and cache (`cmsis-pack-manager`, `pyocd pack install`) are never used.

- Resolution is unchanged and stays offline by default: explicit `.pack`/`.FLM` path, then the local cache, then a vendor download only with `allow_download=True`, into that same cache.
- `PyocdBackend` asks `cmsis_pack.find_device(dut.target_family)` for the pack path and opens the session with pyOCD's `pack=<path>` option and `target_override=<family>`, so pyOCD loads the device (memory map, algorithms) from exactly the file the pod backend would use. pyOCD's built-in targets are not used when a pack device matches, so a family name means the same part on both backends.
- `dut.flash_algorithm`, when set, cannot be selected in pyOCD: it loads only a pack's default algorithm per flash range and has no by-name choice. `PyocdBackend` therefore compares the declared name with the pack default at `flash_base` and refuses to flash or erase when they differ, rather than flash with a different algorithm than declared. Boards whose flash needs a non-default algorithm (e.g. the Arch Mix QSPI part) stay on a pod.
- `pod pack` (list / fetch / path) becomes the single user-facing way to manage the cache; no pyOCD pack commands appear in docs or tool output.
- Results record the pack file and algorithm name used, alongside the backend name.

## Halt and session lifetime

pyOCD sessions are per operation, but an ST-LINK resets the target when a session closes, which undoes a halt. A core halted by `dut_halt` or `dut_reset mode=halt` therefore keeps its session, and the probe lock, until it is resumed, reset, flashed or erased. That lasts for the process, so it holds across MCP calls but a one-shot `pod dut halt` cannot leave a core halted on such a probe.

## Contention

A route that bypasses the pod bypasses the pod's holder record. Every host-local route (pyocd probe, direct tty) takes a host-side `flock` keyed on the probe uid or tty path, reported through the same `PodConflictError` holder shape and honouring `force`. Pod routes keep the pod-side holder as today.

## Registration

- Pods: unchanged. `pod register` / `register_pod` discovers via mDNS and writes the entry.
- Route overrides: `pod route <label> uart tty --tty <by-id>` (and `debug pyocd --uid <uid>`, `instruments pod --pod <label>`).
- Local benches: `pod local add <label> --family .. --pyocd <uid> [--uart-tty ..] [--usb-tty ..]`, or `--from-mpy-dev <name>` to take the probe serial and ttys from the `mpy-dev` registry (it already links `nucleo-h5` to `nucleo-h5-linked`).

## Phases and gates

1. Route model in the registry + resolution (including `instruments` routed to a named pod); `DebugBackend` split with `PodBackend` only. Gate: full host suite green, pod-a and pod-b identify/flash/reset unchanged on hardware.
2. `PyocdBackend` + local bench registration + host flock. Gate on NUCLEO-H563ZI with the STM32H5 pack in the shared cache and nothing in pyOCD's cache: identify, flash + verify, reset, halt/regs/mem, gdb attach.
3. `tty` uart route, `agent-direct` `dut_exec` on local benches, and instruments borrowed from a named pod. Gate: VCP tail and `dut_exec` on the Nucleo; pod-b uart moved to its direct tty; Nucleo SPI captured with pod-b's LA while the ST-LINK owns SWD.
4. Docs sweep (host README, MCP tool descriptions, hardware-setup).

## Risks

- pyOCD target support for newer parts may lag the pack; mitigated by pack-driven targets.
- Two flash implementations can diverge in behaviour; results name the backend.
- Borrowed instruments put two benches' holders on one pod; the pod-side holder already names the caller, which covers it.
