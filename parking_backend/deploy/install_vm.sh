#!/usr/bin/env bash
set -euo pipefail

if test "${EUID}" -ne 0; then
  printf 'Run this script as root.\n' >&2
  exit 1
fi
if ! test -c /dev/kvm; then
  printf '/dev/kvm is unavailable. Enable nested virtualization or use a compatible N2 VM.\n' >&2
  exit 1
fi
if test "$(uname -m)" != x86_64; then
  printf 'The pinned Android image requires an x86_64 VM.\n' >&2
  exit 1
fi

apt-get update
apt-get install -y curl nginx nodejs npm openjdk-17-jre-headless python3-venv unzip \
  libgl1 libpulse0 libnss3 libx11-6 libxcb1 libxcomposite1 libxcursor1 libxi6 libxtst6 libasound2t64
id parking-demo >/dev/null 2>&1 || useradd --system --create-home --groups kvm parking-demo
install -d -o parking-demo -g parking-demo -m 0750 /opt/parking-demo
install -d -o parking-demo -g parking-demo -m 0700 /var/lib/parking-demo
install -d -o root -g parking-demo -m 0750 /etc/parking-demo

python3 -m venv /opt/parking-demo/.venv
/opt/parking-demo/.venv/bin/pip install -r /opt/parking-demo/parking_backend/requirements.txt
install -d -o parking-demo -g parking-demo /opt/parking-demo/npm
sudo -u parking-demo npm config set prefix /opt/parking-demo/npm
sudo -u parking-demo /usr/bin/npm install --global appium@2.19.0
cd /opt/parking-demo
sudo -u parking-demo env PATH=/opt/parking-demo/npm/bin:/usr/bin:/bin \
  /opt/parking-demo/npm/bin/appium driver install uiautomator2@4.2.9

android_root=/opt/android-sdk
install -d -o parking-demo -g parking-demo "$android_root/cmdline-tools"
tools_zip=/tmp/android-command-line-tools.zip
curl --fail --location --output "$tools_zip" \
  https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
unzip -q "$tools_zip" -d "$android_root/cmdline-tools"
mv "$android_root/cmdline-tools/cmdline-tools" "$android_root/cmdline-tools/latest"
chown -R parking-demo:parking-demo "$android_root"

sdkmanager="$android_root/cmdline-tools/latest/bin/sdkmanager"
sudo -u parking-demo env ANDROID_HOME="$android_root" "$sdkmanager" --licenses
sudo -u parking-demo env ANDROID_HOME="$android_root" "$sdkmanager" \
  'platform-tools' 'emulator' 'platforms;android-35' 'system-images;android-35;google_apis_playstore;x86_64'
printf 'no\n' | sudo -u parking-demo env ANDROID_HOME="$android_root" \
  "$android_root/cmdline-tools/latest/bin/avdmanager" create avd --force --name parking-demo \
  --package 'system-images;android-35;google_apis_playstore;x86_64' --device pixel_6

install -m 0644 /opt/parking-demo/parking_backend/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable android-emulator appium parking-api parking-worker

printf 'Installation complete. Configure /etc/parking-demo/backend.env and TLS before starting services.\n'
