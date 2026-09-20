#!/usr/bin/env bash
# Mirror the VM's headless Android emulator onto this workstation so a human can drive it by hand:
# adding the Google account, or clearing a one-off interstitial. Run this on your own machine, not on the VM.
#
#   ./remote_emulator.sh            stop the worker, mirror the emulator, restart the worker on exit
#   ./remote_emulator.sh --status   report VM services, the emulator and the signed-in accounts, then exit
#
# The Google sign-in itself stays manual on purpose: Google refuses a WebDriver-controlled session with
# "this browser or app may not be secure", so Appium must never perform it.
set -euo pipefail

instance=${PARKING_VM_INSTANCE:-parking-demo-vm}
project=${PARKING_VM_PROJECT:-fieldscout-497018}
zone=${PARKING_VM_ZONE:-us-west1-b}
port=${PARKING_ADB_PORT:-5555}
serial="127.0.0.1:${port}"

# One adb for the whole script. The scrcpy build ships a newer adb than most distros, and two versions
# take turns killing each other's server.
scrcpy_adb=$HOME/.local/opt/scrcpy-linux-x86_64-v4.1/adb
if test -x "$scrcpy_adb"; then
  adb=$scrcpy_adb
elif command -v adb >/dev/null 2>&1; then
  adb=$(command -v adb)
else
  printf 'adb is not installed on this workstation.\n' >&2
  exit 1
fi

for tool in gcloud scrcpy; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf '%s is not installed on this workstation.\n' "$tool" >&2
    exit 1
  fi
done

# The emulator runs as parking-demo, so its adb server is only reachable under that account.
remote_adb='sudo -u parking-demo env ANDROID_HOME=/opt/android-sdk HOME=/home/parking-demo /opt/android-sdk/platform-tools/adb'

on_vm() {
  gcloud compute ssh "$instance" --project "$project" --zone "$zone" --command "$1"
}

account_count() {
  on_vm "$remote_adb shell dumpsys account 2>/dev/null | sed -n 's/^ *Accounts: *//p' | head -1"
}

if test "${1:-}" = --status; then
  printf '== services ==\n'
  on_vm 'systemctl is-active android-emulator appium parking-api parking-worker' || true
  printf '== emulator ==\n'
  on_vm "$remote_adb devices" || true
  printf '== google accounts on the device ==\n'
  account_count || true
  exit 0
fi

if test -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}"; then
  printf 'No display found. scrcpy opens a window, so run this from your desktop session.\n' >&2
  exit 1
fi

# Only restart the worker on exit if it was actually running when we started.
worker_was_active=$(on_vm 'systemctl is-active parking-worker' 2>/dev/null || true)
if test "$worker_was_active" = active; then
  printf 'Stopping parking-worker so no attempt runs while you are in the emulator.\n'
  on_vm 'sudo systemctl stop parking-worker'
fi

restore() {
  "$adb" disconnect "$serial" >/dev/null 2>&1 || true
  if test -n "${tunnel:-}"; then
    kill "$tunnel" 2>/dev/null || true
  fi
  printf '\nGoogle accounts on the device now: %s\n' "$(account_count 2>/dev/null || echo unknown)"
  if test "$worker_was_active" = active; then
    printf 'Restarting parking-worker.\n'
    on_vm 'sudo systemctl start parking-worker' || printf 'Could not restart parking-worker; do it by hand.\n' >&2
  fi
}
trap restore EXIT

# The emulator binds its adb port to the VM's loopback only, so it reaches us through the SSH tunnel
# and stays off the external firewall.
gcloud compute ssh "$instance" --project "$project" --zone "$zone" -- -N -L "${port}:127.0.0.1:${port}" &
tunnel=$!

connected=0
for _ in $(seq 30); do
  if "$adb" connect "$serial" 2>/dev/null | grep -qi connected; then
    connected=1
    break
  fi
  sleep 1
done
if test "$connected" -ne 1; then
  printf 'Could not reach the emulator on %s. Run --status to see whether android-emulator is up.\n' "$serial" >&2
  exit 1
fi

"$adb" -s "$serial" wait-for-device
printf 'Mirroring %s (accounts before: %s).\n' "$serial" "$(account_count 2>/dev/null || echo unknown)"
printf 'In the device: Settings -> Passwords & accounts -> Add account -> Google, then sign in to Chrome.\n'
ADB="$adb" scrcpy -s "$serial"
