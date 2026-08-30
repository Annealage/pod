# Annealage Pod - RP2350 firmware build.
#
# Pod boards are defined out-of-tree under src/boards/ (they are not in the
# MicroPython submodule's ports/rp2/boards/), so the build passes BOARD_DIR to
# the rp2 port and lets it derive BOARD from the directory name. The pod's C user
# modules (usbhost, usbip) are pulled in by the board's frozen manifest via the
# c_module() directive (the manifest_c_module branch), so no USER_C_MODULES is
# needed.
#
# Two pod boards exist, differing only in hardware:
#   ANNEALAGE_POD_RP2350  - Raspberry Pi Pico 2 W (RP2350A, 4 MB flash). Flashed
#                           over SWD by a wired pico-probe.
#   ANNEALAGE_POD_RP2350B - Waveshare RP2350B-Plus-W (RP2350B, 16 MB flash,
#                           optional QSPI PSRAM). No probe is wired to it, so it
#                           is flashed over USB in BOOTSEL mode.
# Select one with BOARD=; ANNEALAGE_POD_RP2350 is the default.
#
# The MicroPython integration branch (tessera) is composed by mbm from the
# feature branches listed in mbm.toml; the src/micropython submodule must be
# checked out on tessera before building (see mbm.toml).
#
# Usage:
#   make                       # build the default board (.uf2 + .elf)
#   make BOARD=ANNEALAGE_POD_RP2350B         # build the Waveshare pod
#   make flash                 # build, then flash over SWD via the pico-probe
#   make BOARD=ANNEALAGE_POD_RP2350B flash-usb  # build, then flash over BOOTSEL
#   make reset                 # reset the target
#   make clean                 # remove the board build directory
#   make help                  # list targets

MPY_DIR   := src/micropython
RP2_PORT  := $(MPY_DIR)/ports/rp2
BOARD     ?= ANNEALAGE_POD_RP2350
BOARD_DIR := $(abspath src/boards/$(BOARD))
BUILD     := $(RP2_PORT)/build-$(BOARD)
JOBS      ?= $(shell nproc)

# Bare-metal ARM toolchain. Override ARM_TOOLCHAIN_BIN if installed elsewhere;
# if arm-none-eabi-gcc is already on PATH this prepend is harmless.
ARM_TOOLCHAIN_BIN ?= /opt/arm-gnu-toolchain-14.3.rel1-x86_64-arm-none-eabi/bin
export PATH := $(ARM_TOOLCHAIN_BIN):$(PATH)

# Programming probe (the wired pico-probe). Address by VID:PID:Serial; override
# on the command line for a different probe.
PROBE ?= 2e8a:000c:0501083219160908
CHIP  ?= RP235x

# Safe flashing uses OpenOCD: its rp2350 reset-init halts BOTH cores before any
# flash access, so a running netboot on core1 cannot drive XIP during the erase
# and wedge the QSPI flash. probe-rs halts only core0, which bricked the flash
# once (interrupted erase while core1 ran XIP); see docs/rp2350/dev-notes.md. The
# RPi OpenOCD fork carries the rp2350 flash driver. PROBE_SERIAL is the bare
# serial from PROBE (OpenOCD wants the serial, not VID:PID:Serial).
OPENOCD      ?= /home/corona/openocd_rpi/src/openocd
OPENOCD_TCL  ?= /home/corona/openocd_rpi/tcl
PROBE_SERIAL ?= $(lastword $(subst :, ,$(PROBE)))

# BOOTSEL-mode USB flashing, for a pod with no probe wired to it (the Waveshare
# board). picotool addresses the target by its RP2350 chip serial so that a
# second RP-series board sitting in BOOTSEL cannot be flashed by accident; read
# the serial off the intended board with `picotool info -a` (it is the "chipid",
# uppercased and without the 0x).
POD_USB_SERIAL ?= 8E495826EE18B97A

# picotool cache for the pico-sdk fetch-from-git, so a fresh cmake configure does
# not fail on the host picotool version gate.
PTCACHE ?= $(HOME)/.cache/picotool-sdk

.DEFAULT_GOAL := firmware

.PHONY: firmware
firmware: mpy-cross ## Build the pod firmware (.uf2 + .elf)
	@# Configure with the picotool fetch flag if the build dir is not yet
	@# configured. The rp2 port Makefile's auto-configure omits it, so a fresh
	@# build (e.g. after `make clean`) dies at the pico-sdk picotool version gate
	@# before the qstr collection runs; pre-configuring here makes the port build
	@# reuse it. See docs/rp2350/dev-notes.md.
	[ -e $(BUILD)/Makefile ] || cmake -S $(RP2_PORT) -B $(BUILD) -DPICO_BUILD_DOCS=0 \
		-DMICROPY_BOARD=$(BOARD) -DMICROPY_BOARD_DIR=$(BOARD_DIR) \
		-DPICOTOOL_FORCE_FETCH_FROM_GIT=1 -DPICOTOOL_FETCH_FROM_GIT_PATH=$(PTCACHE)
	$(MAKE) -C $(RP2_PORT) BOARD_DIR=$(BOARD_DIR) -j$(JOBS)
	@echo "built: $(BUILD)/firmware.uf2"

.PHONY: mpy-cross
mpy-cross: ## Build the mpy-cross compiler (needed to freeze the manifest)
	$(MAKE) -C $(MPY_DIR)/mpy-cross -j$(JOBS)

.PHONY: submodules
submodules: ## Initialise the MicroPython submodules this board needs
	$(MAKE) -C $(RP2_PORT) BOARD_DIR=$(BOARD_DIR) submodules

.PHONY: flash
flash: firmware ## Build, then flash over SWD with BOTH cores halted (OpenOCD; safe to run while firmware is live)
	$(OPENOCD) -s $(OPENOCD_TCL) -f interface/cmsis-dap.cfg \
		-c "adapter serial $(PROBE_SERIAL)" -c "adapter speed 5000" \
		-f target/rp2350.cfg \
		-c "program $(BUILD)/firmware.elf verify reset exit"

.PHONY: flash-probe-rs
flash-probe-rs: firmware ## Fallback flash via probe-rs (flattens the multi-section UF2). UNSAFE while firmware runs - probe-rs halts only core0, so core1 XIP during the erase can wedge the QSPI flash; use only from a clean/bootrom state.
	python3 tools/uf2_to_bin.py $(BUILD)/firmware.uf2 $(BUILD)/prog.bin
	probe-rs download --probe $(PROBE) --chip $(CHIP) \
		--binary-format bin --base-address 0x10000000 $(BUILD)/prog.bin
	probe-rs reset --probe $(PROBE) --chip $(CHIP)

.PHONY: flash-usb
flash-usb: firmware ## Build, then flash over USB with the board in BOOTSEL mode (picotool; for boards with no wired probe)
	picotool load -x --ser $(POD_USB_SERIAL) $(BUILD)/firmware.uf2

.PHONY: reset
reset: ## Reset the target over SWD
	probe-rs reset --probe $(PROBE) --chip $(CHIP)

.PHONY: pinout
pinout: ## Regenerate docs/pod/pinout.{svg,png} from the pinmap (needs python3; PNG needs cairosvg)
	python3 tools/pinout_diagram.py

.PHONY: clean
clean: ## Remove the board build directory (forces a fresh cmake configure)
	rm -rf $(BUILD)

.PHONY: help
help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
