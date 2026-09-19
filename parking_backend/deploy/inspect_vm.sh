#!/usr/bin/env bash
set -euo pipefail

printf 'OS: '
. /etc/os-release
printf '%s %s\n' "$NAME" "$VERSION_ID"
printf 'Architecture: '
uname -m
printf 'vCPUs: '
nproc
printf 'Memory: '
free -h | awk '/Mem:/ {print $2}'
printf 'Root disk: '
df -h / | awk 'NR==2 {print $2 " total, " $4 " available"}'
printf 'CPU virtualization flags: '
if grep -Eqm1 '(vmx|svm)' /proc/cpuinfo; then printf 'present\n'; else printf 'missing\n'; fi
printf '/dev/kvm: '
if test -r /dev/kvm -a -w /dev/kvm; then printf 'usable\n'; else printf 'unavailable\n'; fi
printf 'Listening TCP ports:\n'
ss -ltn

