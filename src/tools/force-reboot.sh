#!/usr/bin/env bash
# force-reboot.sh
#
# Reboot the host using the SysRq REISUB-style sequence. Use when the
# normal reboot path would hang because a kernel module is wedged or
# userspace has D-state processes that systemd cannot terminate.
#
# Triggered for the R26/R27 vhci_hcd corruption scenario where
# `usb_poison_urb` puts mpremote and modprobe into uninterruptible sleep
# and `sudo reboot` blocks on the systemd shutdown sequence.
#
# Sequence:
#   s: sync filesystems
#   u: remount read-only
#   b: immediate reboot via the kernel reboot syscall
#
# This bypasses systemd entirely so stuck userspace cannot block it.

set -uo pipefail

if [[ $EUID -ne 0 ]] && ! sudo -n true 2>/dev/null; then
    echo "Need sudo. Re-run with sudo or have a cached credential." >&2
    exit 1
fi

echo "Forceful host reboot via SysRq trigger in 5 seconds. Ctrl-C to cancel."
for i in 5 4 3 2 1; do
    echo "  $i ..."
    sleep 1
done

echo "Enabling all SysRq functions ..."
echo 1 | sudo tee /proc/sys/kernel/sysrq >/dev/null

echo "Sync filesystems ..."
echo s | sudo tee /proc/sysrq-trigger >/dev/null
sleep 2

echo "Remount read-only ..."
echo u | sudo tee /proc/sysrq-trigger >/dev/null
sleep 2

echo "Reboot now."
echo b | sudo tee /proc/sysrq-trigger >/dev/null

# If we get here something went wrong; fall back to the regular forceful reboot.
sleep 5
echo "SysRq did not trigger reboot. Falling back to reboot -f." >&2
sudo reboot -f
