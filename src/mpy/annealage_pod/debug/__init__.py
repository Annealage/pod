# On-pod debug-probe stack for the Annealage Pod RP2350 (workstream D).
#
# Layers, bottom up:
#   swd_pio  : SWD bit transport over a PIO state machine (the proven primitive).
#   swd_dap  : ADIv5 DP / AP / MEM-AP on top of the transport, plus Cortex-M
#              halt/run control. Implemented from the ARM ADIv5 / ARMv7-M specs so it stays portable.
#   flash_*  : per-target flash loaders driven through the MEM-AP (nRF52 NVMC
#              first; CMSIS-FLM general path and the RP-native fast path later).
#
# These run on the pod and are driven from the host over the REPL (USB-CDC for
# development, or the Wi-Fi socket REPL in deployment).

__version__ = "0.0.1"
