#!/usr/bin/env bash
# Snapshot of how the integrated controller is exposed: InputPlumber devices,
# kernel input devices, hidraw nodes, and which ones Steam has open.
# Run on the device with sudo, e.g.:
#   sudo scripts/diagnose.sh > enabled.txt    (controller enabled)
#   sudo scripts/diagnose.sh > disabled.txt   (after disabling in the plugin)
#   diff enabled.txt disabled.txt
BUS=org.shadowblip.InputPlumber
IFACE=org.shadowblip.Input.CompositeDevice

echo "=== InputPlumber composite devices ==="
for p in $(busctl --system tree --list $BUS | grep -E '/CompositeDevice[0-9]+$'); do
  echo "--- $p"
  for prop in Name ProfileName SourceDevicePaths TargetDevices; do
    echo "  $prop: $(busctl --system get-property $BUS "$p" $IFACE $prop 2>&1)"
  done
done

echo
echo "=== All InputPlumber objects ==="
busctl --system tree --list $BUS

echo
echo "=== Kernel input devices ==="
grep -E '^(N|S|H):' /proc/bus/input/devices

echo
echo "=== hidraw devices ==="
for h in /sys/class/hidraw/hidraw*; do
  [ -e "$h" ] || continue
  echo "$(basename "$h"): $(grep -E 'HID_NAME|HID_ID' "$h/device/uevent" | tr '\n' ' ')"
done

echo
echo "=== Device nodes ==="
ls -l /dev/input/ /dev/hidraw* 2>&1
ls -lR /dev/inputplumber 2>/dev/null

echo
echo "=== Input/hidraw nodes held open by Steam ==="
for pid in $(pgrep -x steam); do
  ls -l /proc/"$pid"/fd 2>/dev/null | grep -E '/dev/(input|hidraw|uinput)' | sed "s|^|pid $pid: |"
done
