#!/bin/sh
# Generate a self-signed certificate for the personal site.
#
# Usage:
#   ./scripts/gen-self-signed-cert.sh [cert-dir] [hostnames-and-ips, comma-separated] [days]
#
# Examples:
#   ./scripts/gen-self-signed-cert.sh
#   ./scripts/gen-self-signed-cert.sh certs personal-site.local,192.168.1.10
#   ./scripts/gen-self-signed-cert.sh /etc/ssl/personal-site personal-site.local,192.168.1.10 3650
#
# Output (in the cert dir):
#   cert.pem   - public certificate
#   key.pem    - private key (chmod 600)
#
# The cert is valid for the listed SANs. Modern browsers REQUIRE a SAN entry
# (DNS or IP) for the name they connect with - a bare CN is not enough.
set -eu

CERT_DIR="${1:-certs}"
SAN_LIST="${2:-personal-site.local,127.0.0.1}"
DAYS="${3:-3650}"

command -v openssl >/dev/null 2>&1 || {
    echo "error: openssl not found (install it first: sudo apt install openssl)" >&2
    exit 1
}

mkdir -p "$CERT_DIR"

# Build the subjectAltName extension: DNS: for hostnames, IP: for IPs
SAN=""
OLDIFS="$IFS"
IFS=","
for entry in $SAN_LIST; do
    IFS="$OLDIFS"
    [ -z "$entry" ] && continue
    case "$entry" in
        *[0-9]*.*)
            # looks like an IP (heuristic: digits + dots, no letters before first dot)
            if printf '%s' "$entry" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; then
                san="IP:$entry"
            else
                san="DNS:$entry"
            fi
            ;;
        *) san="DNS:$entry" ;;
    esac
    IFS=","
    if [ -z "$SAN" ]; then
        SAN="$san"
    else
        SAN="$SAN,$san"
    fi
done
IFS="$OLDIFS"
[ -n "$SAN" ] || SAN="DNS:personal-site.local"

echo "==> Generating self-signed cert"
echo "    dir:       $CERT_DIR"
echo "    SANs:      $SAN"
echo "    validity:  $DAYS days"

# -newkey rsa:2048 - x509 -nodes -newkey requires OpenSSL 1.1.1+
openssl req -x509 -nodes -newkey rsa:2048 -days "$DAYS" \
    -keyout "$CERT_DIR/key.pem" \
    -out "$CERT_DIR/cert.pem" \
    -subj "/O=PersonalSite/CN=personal-site" \
    -addext "subjectAltName=$SAN" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth"

chmod 600 "$CERT_DIR/key.pem"
chmod 644 "$CERT_DIR/cert.pem"

echo "==> Done."
echo "    cert: $CERT_DIR/cert.pem"
echo "    key:  $CERT_DIR/key.pem"
echo ""
echo "To trust it in a browser (optional): open cert.pem -> Install Certificate"
echo "    -> Current User -> Local Machine -> Trusted Root Certification Authorities."
