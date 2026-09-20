#!/usr/bin/env bash
# Install what remote_emulator.sh needs on a workstation: the Google Cloud CLI and a scrcpy new enough for
# Android 15. Everything lands under $HOME, so this needs no root and changes nothing system-wide.
# Run it on your own machine, not on the VM. Re-running it is safe.
set -euo pipefail

scrcpy_version=v4.1
scrcpy_archive="scrcpy-linux-x86_64-${scrcpy_version}.tar.gz"
scrcpy_root="$HOME/.local/opt/scrcpy-linux-x86_64-${scrcpy_version}"
gcloud_root="$HOME/google-cloud-sdk"
bin_dir="$HOME/.local/bin"

if test "$(uname -m)" != x86_64; then
  printf 'These prebuilt archives are x86-64 only.\n' >&2
  exit 1
fi
mkdir -p "$bin_dir" "$HOME/.local/opt"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

if test -x "$gcloud_root/bin/gcloud"; then
  printf 'Google Cloud CLI already present in %s.\n' "$gcloud_root"
else
  printf 'Installing the Google Cloud CLI into %s\n' "$gcloud_root"
  curl -fsSL -o "$work/gcloud.tar.gz" \
    https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz
  tar -xzf "$work/gcloud.tar.gz" -C "$HOME"
fi
ln -sf "$gcloud_root/bin/gcloud" "$bin_dir/gcloud"
ln -sf "$gcloud_root/bin/gsutil" "$bin_dir/gsutil"

if test -x "$scrcpy_root/scrcpy"; then
  printf 'scrcpy %s already present in %s.\n' "$scrcpy_version" "$scrcpy_root"
else
  printf 'Installing scrcpy %s into %s\n' "$scrcpy_version" "$scrcpy_root"
  base="https://github.com/Genymobile/scrcpy/releases/download/${scrcpy_version}"
  curl -fsSL -o "$work/$scrcpy_archive" "$base/$scrcpy_archive"
  curl -fsSL -o "$work/SHA256SUMS.txt" "$base/SHA256SUMS.txt"
  # Never unpack a release archive that does not match the checksum published beside it.
  (cd "$work" && grep " $scrcpy_archive\$" SHA256SUMS.txt | sha256sum -c -)
  tar -xzf "$work/$scrcpy_archive" -C "$HOME/.local/opt"
fi

# A wrapper, not a symlink: the prebuilt needs its own scrcpy-server and ships an adb newer than most
# distros package. The distro's scrcpy stays installed and is used again if this file is deleted.
cat >"$bin_dir/scrcpy" <<EOF
#!/usr/bin/env bash
set -euo pipefail
root=$scrcpy_root
export SCRCPY_SERVER_PATH="\$root/scrcpy-server"
export ADB="\${ADB:-\$root/adb}"
exec "\$root/scrcpy" "\$@"
EOF
chmod +x "$bin_dir/scrcpy"

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) printf '\nAdd %s to your PATH, ahead of /usr/bin.\n' "$bin_dir" ;;
esac

printf '\ngcloud: %s\n' "$("$bin_dir/gcloud" --version 2>/dev/null | head -1)"
printf 'scrcpy: %s\n' "$("$bin_dir/scrcpy" --version 2>/dev/null | head -1)"
printf '\nIf gcloud is not signed in yet, run: gcloud auth login\n'
printf 'Then check the VM with: ./remote_emulator.sh --status\n'
