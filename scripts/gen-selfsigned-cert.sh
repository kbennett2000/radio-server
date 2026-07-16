#!/usr/bin/env bash
# Generate a self-signed TLS cert/key so radio-server can serve HTTPS (ADR 0039).
#
# A phone on the LAN needs an HTTPS origin: browsers gate the microphone (getUserMedia) and
# AudioWorklet — i.e. Talk and Listen — behind a "secure context" that plain http://<lan-ip> is not.
# (localhost is exempt, which is why the PC works over plain HTTP but the phone does not.)
#
# Usage:
#   scripts/gen-selfsigned-cert.sh <lan-ip> [hostname] [out-dir]
#
# Examples:
#   scripts/gen-selfsigned-cert.sh 192.168.1.62
#   scripts/gen-selfsigned-cert.sh 192.168.1.62 radio.local ./tls
#
# Then point radio.toml at the files it prints:
#   [server]
#   tls_cert = "/abs/path/radio-cert.pem"
#   tls_key  = "/abs/path/radio-key.pem"
#
# On the phone (Android/Chrome) browse https://<lan-ip>:8000 and tap through the one-time
# "Your connection is not private" warning; the origin is then secure and Listen/Talk work.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <lan-ip> [hostname] [out-dir]" >&2
  exit 2
fi

ip="$1"
host="${2:-radio-server}"
outdir="${3:-.}"
mkdir -p "$outdir"
cert="$outdir/radio-cert.pem"
key="$outdir/radio-key.pem"

# SANs must include the exact IP the phone types in the URL bar, or the cert won't match the origin.
san="subjectAltName=IP:${ip},DNS:${host},DNS:localhost,IP:127.0.0.1"

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$key" -out "$cert" \
  -days 825 \
  -subj "/CN=${host}" \
  -addext "$san"

chmod 600 "$key"

cert_abs="$(cd "$(dirname "$cert")" && pwd)/$(basename "$cert")"
key_abs="$(cd "$(dirname "$key")" && pwd)/$(basename "$key")"

cat <<EOF

Done. Add these to the [server] section of your radio.toml:

  tls_cert = "${cert_abs}"
  tls_key  = "${key_abs}"

Restart radio-server, then on the phone browse: https://${ip}:8000
(Accept the one-time self-signed warning; Listen and Talk will then work.)
EOF
