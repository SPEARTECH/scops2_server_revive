#!/usr/bin/env bash
set -e

# ─── Configuration ───────────────────────────────────────────────
BIND_IP="0.0.0.0"
HOST_IP="3.238.21.103"
TLS_CERT_CN="gate1.us.dnas.playstation.org"
UPSTREAM_DNS="8.8.8.8:53"
GSINIT_FILE="gsinit_diag_localweb.php"

JOINWAIT_FORMAT="u32le"
KEYEX2_MODE="echo-client"
WM_KEYEX2_MODE="echo-client"
POST_KE2_PUSH="off"
WM_POST_KE2_PUSH="off"
CT34_PROFILE="ct_ps2"
UDP_45000_MODE="srp_nat"
SCCT12_EXTRA="off"

# ─── Paths ───────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
if [ ! -f "$PYTHON" ]; then
    PYTHON="$(command -v python3 || command -v python)"
fi

OPENSSL="$(command -v openssl)"

echo "Using Python: $PYTHON"
echo "Using OpenSSL: $OPENSSL"

# Check dependencies
$PYTHON -c "import rsa" 2>/dev/null || {
    echo "ERROR: Missing Python module 'rsa'. Install with: pip install rsa dnslib"
    exit 1
}

# ─── Create directories ─────────────────────────────────────────
mkdir -p logs captures/tcp/router_rx captures/tcp/router_tx tools/certs

# ─── Generate DNAS TLS cert (PS2-era compatible) ─────────────────
DNAS_KEY="tools/certs/dnas.key"
DNAS_CRT="tools/certs/dnas.crt"

if [ ! -f "$DNAS_CRT" ] || [ ! -f "$DNAS_KEY" ]; then
    echo "Generating PS2-friendly TLS cert for '$TLS_CERT_CN'..."
    CA_DIR="tools/certs/ca"
    rm -rf "$CA_DIR"
    mkdir -p "$CA_DIR/newcerts"
    touch "$CA_DIR/index.txt"
    echo "01" > "$CA_DIR/serial"

    cat > "$CA_DIR/openssl.cnf" <<CNFEOF
[ req ]
default_bits = 1024
prompt = no
distinguished_name = req_distinguished_name
req_extensions = v3_req

[ req_distinguished_name ]
CN = $TLS_CERT_CN

[ v3_req ]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[ alt_names ]
DNS.1 = $TLS_CERT_CN

[ ca ]
default_ca = CA_default

[ CA_default ]
dir = $CA_DIR
database = $CA_DIR/index.txt
new_certs_dir = $CA_DIR/newcerts
serial = $CA_DIR/serial
default_md = sha1
policy = policy_any
x509_extensions = v3_req

[ policy_any ]
commonName = supplied
CNFEOF

    $OPENSSL genrsa -out "$DNAS_KEY" 1024 2>/dev/null
    $OPENSSL req -new -key "$DNAS_KEY" -out "$CA_DIR/dnas.csr" -config "$CA_DIR/openssl.cnf" 2>/dev/null
    $OPENSSL ca -selfsign -batch -config "$CA_DIR/openssl.cnf" -extensions v3_req \
        -in "$CA_DIR/dnas.csr" -out "$DNAS_CRT" -keyfile "$DNAS_KEY" \
        -startdate 20000101000000Z -enddate 20500101000000Z -notext 2>/dev/null
    echo "  cert: $DNAS_CRT"
fi

# ─── Kill existing listeners ─────────────────────────────────────
echo "Stopping existing listeners..."
for PORT in 53 80 443 40000 40005 44001 44002 45000; do
    fuser -k $PORT/tcp 2>/dev/null || true
    fuser -k $PORT/udp 2>/dev/null || true
done
sleep 0.25

# ─── Start services ──────────────────────────────────────────────
echo "Starting server stack..."

# DNS/53
$PYTHON tools/dns_override_forwarder.py \
    --listen "$BIND_IP:53" \
    --upstream "$UPSTREAM_DNS" \
    --a-suffix "ubisoft.com=$HOST_IP" \
    --a-suffix "ubi.com=$HOST_IP" \
    --a-suffix "gamespy.com=$HOST_IP" \
    --a-suffix "gamespy.net=$HOST_IP" \
    --a-suffix "dnas.playstation.org=$HOST_IP" \
    --a "noname=$HOST_IP" \
    --log --log-file logs/dns_53.log &

# HTTP/80
$PYTHON tools/gs_http_server.py \
    --bind "$BIND_IP" --port 80 \
    --gsinit-file "$GSINIT_FILE" \
    --web-root webroot \
    --allow-any \
    --log-file logs/gs_http_80.log &

# TLS/443 (DNAS gate)
$OPENSSL s_server \
    -accept "$BIND_IP:443" \
    -cert "$DNAS_CRT" -key "$DNAS_KEY" \
    -cipher "DES-CBC3-SHA:@SECLEVEL=0" \
    -bugs -legacy_renegotiation -ign_eof \
    -state -tlsextdebug -msg \
    > logs/tls_443.log 2> logs/tls_443_err.log &

# TCP logger (other ports)
$PYTHON tools/tcp_log_server.py \
    --bind "$BIND_IP" \
    --ports "44000,6667,6668,27900,27901,28910,29900,29901,29920" \
    --max-bytes 1024 --idle-timeout 2 \
    --out-dir captures/tcp \
    --log-file logs/tcp_ports.log &

# UDP loggers
$PYTHON tools/udp_log_server.py \
    --bind "$BIND_IP" --ports "19341" \
    --max-bytes 512 --log-file logs/udp_19341.log &

$PYTHON tools/udp_log_server.py \
    --bind "$BIND_IP" --ports "3658,4400,41006,10070,10071,10072,10073,10074,10075,10076,10077,10078,10079,10080" \
    --max-bytes 512 --log-file logs/udp_manual_ports.log &

# UDP reply (NAT probe)
$PYTHON tools/udp_reply_server.py \
    --bind "$BIND_IP" --ports "45000,45001" \
    --max-bytes 512 --reply-mode "$UDP_45000_MODE" \
    --scct12-extra "$SCCT12_EXTRA" \
    --log-file logs/udp_45000_reply.log &

# UDP GameSpy echo
$PYTHON tools/udp_reply_server.py \
    --bind "$BIND_IP" --ports "27900,27901,28910,29900,29901,29920" \
    --max-bytes 512 --reply-mode echo \
    --log-file logs/udp_gamespy_echo.log &

# GS Router/40000
$PYTHON tools/ubigs_router_server.py \
    --bind "$BIND_IP" --port 40000 \
    --wm-ip "$HOST_IP" --wm-port 40005 \
    --joinwait-format "$JOINWAIT_FORMAT" \
    --keyex2-mode "$KEYEX2_MODE" \
    --post-ke2-push "$POST_KE2_PUSH" \
    --log-file logs/router_40000.log \
    --save-rx-dir captures/tcp/router_rx \
    --save-tx-dir captures/tcp/router_tx \
    --ct34-profile "$CT34_PROFILE" \
    --fixed-rsa-key-file state/shared_router_rsa.json &

# GS Router WM/40005
$PYTHON tools/ubigs_router_wm_server.py \
    --bind "$BIND_IP" --port 40005 \
    --proxy-ip "$HOST_IP" --proxy-port 44002 \
    --keyex2-mode "$WM_KEYEX2_MODE" \
    --post-ke2-push "$WM_POST_KE2_PUSH" \
    --log-file logs/router_wm_40005.log \
    --ct34-profile "$CT34_PROFILE" \
    --user-db state/users.json \
    --login-boot-delay 0.5 \
    --idle-timeout 30 \
    --fixed-rsa-key-file state/shared_router_rsa.json &

# Pers Proxy/44001
$PYTHON tools/ubigs_pers_proxy_server.py \
    --bind "$BIND_IP" --port 44001 \
    --wm-ip "$HOST_IP" --wm-port 44002 \
    --log-file logs/pers_proxy_44001.log &

# Pers Proxy WM/44002
$PYTHON tools/ubigs_pers_proxy_wm_server.py \
    --bind "$BIND_IP" --port 44002 \
    --log-file logs/pers_proxy_wm_44002.log &

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Server stack started!"
echo "═══════════════════════════════════════════════════"
echo "  Host IP:        $HOST_IP"
echo "  KE2 mode:       $KEYEX2_MODE (router) / $WM_KEYEX2_MODE (wm)"
echo "  CT34 profile:   $CT34_PROFILE"
echo ""
echo "  DNS:            $BIND_IP:53"
echo "  HTTP:           $BIND_IP:80"
echo "  TLS/DNAS:       $BIND_IP:443"
echo "  Router:         $BIND_IP:40000"
echo "  Router WM:      $BIND_IP:40005"
echo "  Pers Proxy:     $BIND_IP:44001"
echo "  Pers Proxy WM:  $BIND_IP:44002"
echo "  NAT Probe:      $BIND_IP:45000 (UDP)"
echo ""
echo "  Logs in: $ROOT/logs/"
echo "═══════════════════════════════════════════════════"
echo ""
echo "Press Ctrl+C to stop all services."

# Wait for all background processes; Ctrl+C kills them all
trap "echo 'Stopping...'; kill 0; exit 0" SIGINT SIGTERM
wait
