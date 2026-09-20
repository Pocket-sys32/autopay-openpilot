#!/usr/bin/env bash
# Mirror the VM's headless Android emulator onto this workstation so a human can drive it by hand
# (adding a Google account, clearing a one-off interstitial). Run this on your own machine, not on the VM.
set -euo pipefail

instance=${PARKING_VM_INSTANCE:-instance1}
project=${PARKING_VM_PROJECT:-fieldscout-497018}
zone=${PARKING_VM_ZONE:-us-west1-b}
port=${PARKING_ADB_PORT:-5555}
serial="127.0.0.1:${port}"

for tool in gcloud adb scrcpy; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf '%s is not installed on this workstation.\n' "$tool" >&2
    exit 1
  fi
done

# The emulator binds its adb port to the VM's loopback only, so it is reachable through the SSH tunnel
# and stays off the external firewall.
gcloud compute ssh "$instance" --project "$project" --zone "$zone" -- -N -L "${port}:127.0.0.1:${port}" &
tunnel=$!
trap 'adb disconnect "$serial" >/dev/null 2>&1 || true; kill "$tunnel" 2>/dev/null || true' EXIT

connected=0
for _ in $(seq 30); do
  if adb connect "$serial" 2>/dev/null | grep -qi connected; then
    connected=1
    break
  fi
  sleep 1
done
if test "$connected" -ne 1; then
  printf 'Could not reach the emulator on %s. Is android-emulator.service running?\n' "$serial" >&2
  exit 1
fi

adb -s "$serial" wait-for-device
printf 'Mirroring %s. Stop parking-worker first if an attempt could run while you are in here.\n' "$serial"
scrcpy -s "$serial"
