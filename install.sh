#!/usr/bin/env bash
# PR GRAM Helper - installer (Ubuntu/Debian/Fedora/Rocky ... on a VPS such as Doprax)
# Usage:  sudo bash install.sh
#         sudo DOMAIN=panel.example.com bash install.sh     (non-interactive domain)
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/prgram}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

c_ok=$'\e[32m'; c_err=$'\e[31m'; c_inf=$'\e[36m'; c_off=$'\e[0m'
ok()  { echo "${c_ok}✔${c_off} $*"; }
inf() { echo "${c_inf}➜${c_off} $*"; }
die() { echo "${c_err}✖ $*${c_off}" >&2; exit 1; }
trap 'die "Error on line $LINENO"' ERR

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash install.sh"

# ---------- 1) prerequisites ----------
inf "Installing prerequisites..."
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y && apt-get install -y curl ca-certificates openssl tar gzip
elif command -v dnf >/dev/null; then
  dnf install -y curl ca-certificates openssl tar gzip
elif command -v yum >/dev/null; then
  yum install -y curl ca-certificates openssl tar gzip
else
  die "Unsupported package manager"
fi

if ! command -v docker >/dev/null; then
  inf "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null 2>&1 || die "docker compose v2 not found (install the docker-compose-plugin package)"
ok "Docker is ready"

# ---------- 2) files ----------
mkdir -p "$INSTALL_DIR/data"
for f in app.py panel_ui.html requirements.txt Dockerfile docker-compose.yml Caddyfile; do
  [[ -f "$SRC_DIR/$f" ]] || die "File $f is missing next to install.sh"
  cp -f "$SRC_DIR/$f" "$INSTALL_DIR/$f"
done
# dotfiles (may have been renamed with a "_" prefix during upload/download)
for f in .dockerignore .env.example; do
  if   [[ -f "$SRC_DIR/$f" ]];  then cp -f "$SRC_DIR/$f"  "$INSTALL_DIR/$f"
  elif [[ -f "$SRC_DIR/_${f#.}" ]]; then cp -f "$SRC_DIR/_${f#.}" "$INSTALL_DIR/$f"
  else die "File $f is missing next to install.sh"; fi
done
chown -R 1000:1000 "$INSTALL_DIR/data"   # the user inside the container is uid 1000
ok "Files copied to $INSTALL_DIR"

# ---------- 3) .env settings ----------
ENV_FILE="$INSTALL_DIR/.env"
sed -i 's/\r$//' "$ENV_FILE" 2>/dev/null || true   # fixes a .env created on Windows (CRLF)
env_get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }
set_env() { # set_env KEY VALUE
  [[ -s "$ENV_FILE" && -n "$(tail -c1 "$ENV_FILE")" ]] && echo >> "$ENV_FILE"
  if grep -qE "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
if [[ -f "$ENV_FILE" ]]; then
  ok ".env already exists; leaving it untouched"
else
  cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
  read -rp "BOT_TOKEN (from BotFather): " BOT_TOKEN
  read -rp "OWNER_ID (your numeric Telegram ID): " OWNER_ID
  read -rp "API_ID (empty = shared default, not recommended): " API_ID || true
  API_HASH=""; [[ -n "${API_ID:-}" ]] && read -rp "API_HASH: " API_HASH
  [[ -n "$BOT_TOKEN" && "$OWNER_ID" =~ ^[0-9]+$ ]] || die "BOT_TOKEN or OWNER_ID is invalid"
  SESSION_KEY="$(head -c32 /dev/urandom | base64 | tr '+/' '-_')"   # Fernet key format
  sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$BOT_TOKEN|;s|^OWNER_ID=.*|OWNER_ID=$OWNER_ID|;s|^SESSION_KEY=.*|SESSION_KEY=$SESSION_KEY|;s|^API_ID=.*|API_ID=${API_ID:-}|;s|^API_HASH=.*|API_HASH=${API_HASH:-}|" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok ".env created"
  echo "${c_err}⚠ SESSION_KEY is stored in $ENV_FILE. Keep a copy somewhere safe (off the server); without it the saved sessions cannot be recovered.${c_off}"
fi

# ---------- 3.5) domain + automatic HTTPS ----------
DOMAIN="${DOMAIN:-$(env_get DOMAIN)}"
if [[ -z "$DOMAIN" ]]; then
  read -rp "Panel domain (e.g. panel.example.com; its A record must point to this server; empty = no domain): " DOMAIN || true
fi
DOMAIN="$(echo "${DOMAIN:-}" | tr 'A-Z' 'a-z' | sed -E 's#^https?://##; s#/.*$##; s#\s##g')"

if [[ -n "$DOMAIN" ]]; then
  [[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "Invalid domain: $DOMAIN"

  SERVER_IP="$(curl -4fsS --max-time 6 https://api.ipify.org 2>/dev/null || curl -4fsS --max-time 6 https://ifconfig.me 2>/dev/null || true)"
  DNS_IP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)"
  inf "Server IP: ${SERVER_IP:-unknown}   |   Domain A record: ${DNS_IP:-not found}"
  if [[ -z "$DNS_IP" || ( -n "$SERVER_IP" && "$DNS_IP" != "$SERVER_IP" ) ]]; then
    echo "${c_err}⚠ The domain's A record does not point to this server (or has not propagated yet).${c_off}"
    echo "  Create an A record for $DOMAIN with value ${SERVER_IP:-the server IP} (on Cloudflare, set the cloud to gray / DNS only)."
    if [[ -t 0 && -z "${FORCE:-}" ]]; then
      read -rp "Continue anyway? (the SSL certificate cannot be issued until DNS is correct) [y/N] " a
      [[ "$a" =~ ^[Yy]$ ]] || die "Fix DNS first and run again (or use FORCE=1)"
    fi
  fi

  # ports 80/443 must not be used by another service (nginx/apache)
  busy="$(ss -tlnp 2>/dev/null | awk '$4 ~ /:(80|443)$/ && $0 !~ /docker-proxy/ {print $4" "$NF}' | head -3 || true)"
  [[ -z "$busy" ]] || die "Port 80/443 is used by another service: $busy  (stop it, e.g. systemctl disable --now nginx apache2)"

  # OS firewall (if ufw is active)
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
    ok "ufw: ports 80/443 opened"
  fi

  set_env DOMAIN "$DOMAIN"
  set_env WEB_BASE_URL "https://$DOMAIN"
  set_env TRUST_PROXY 1
  set_env BIND_IP 127.0.0.1
  set_env COMPOSE_PROFILES domain
  ok "Domain configured: https://$DOMAIN"
  echo "  (If you use the Doprax firewall, open ports 80 and 443 there too)"
else
  inf "No domain: the panel is available at http://IP:$(env_get HOST_PORT) (add one later with: selfpanel domain)"
fi

# ---------- 4) selfpanel ----------
if [[ -f "$SRC_DIR/selfpanel" ]]; then
  install -m 0755 "$SRC_DIR/selfpanel" /usr/local/bin/selfpanel
  ok "selfpanel command installed"
fi

# ---------- 5) swap for low-RAM servers ----------
mem_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
if (( mem_mb < 1500 )) && ! swapon --show | grep -q .; then
  inf "Low RAM (${mem_mb}MB); creating 1GB swap..."
  fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ok "Swap enabled"
fi

# ---------- 6) build and run ----------
inf "Building and starting..."
cd "$INSTALL_DIR"
docker compose up -d --build --remove-orphans
sleep 8
docker compose ps
if [[ -n "${DOMAIN:-}" ]]; then
  inf "Requesting the SSL certificate (up to ~60 seconds)..."
  for _ in $(seq 1 15); do
    curl -fsS --max-time 5 -o /dev/null "https://$DOMAIN/healthz" 2>/dev/null && { ok "HTTPS is active: https://$DOMAIN"; break; }; sleep 4
  done
  curl -fsS --max-time 5 -o /dev/null "https://$DOMAIN/healthz" 2>/dev/null || echo "${c_err}⚠ HTTPS is not responding yet; check the log: docker logs prgram-caddy${c_off}"
fi
echo
ok "Installation finished. To manage the service type:  ${c_inf}selfpanel${c_off}"
echo "Admin link: selfpanel link   (or menu option 6)"
