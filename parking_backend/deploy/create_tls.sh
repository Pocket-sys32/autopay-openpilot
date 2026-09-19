#!/usr/bin/env bash
set -euo pipefail

external_ip="${1:?usage: create_tls.sh EXTERNAL_IP}"
output_dir="${2:-./tls-output}"
mkdir -p "$output_dir"
chmod 700 "$output_dir"

openssl genrsa -out "$output_dir/parking-ca.key" 4096
openssl req -x509 -new -key "$output_dir/parking-ca.key" -sha256 -days 3650 \
  -subj '/CN=Comma Parking Demo CA' -out "$output_dir/parking-ca.crt"
openssl genrsa -out "$output_dir/parking-server.key" 3072
openssl req -new -key "$output_dir/parking-server.key" -subj '/CN=parking-demo' -out "$output_dir/parking-server.csr"
printf 'subjectAltName=IP:%s\nextendedKeyUsage=serverAuth\n' "$external_ip" > "$output_dir/server.ext"
openssl x509 -req -in "$output_dir/parking-server.csr" -CA "$output_dir/parking-ca.crt" \
  -CAkey "$output_dir/parking-ca.key" -CAcreateserial -out "$output_dir/parking-server.crt" \
  -days 825 -sha256 -extfile "$output_dir/server.ext"
chmod 600 "$output_dir"/*.key
printf 'Install parking-server.crt/key on the VM and parking-ca.crt on comma four. Keep parking-ca.key offline.\n'

