#!/usr/bin/env bash
# PR GRAM Helper - installer
# Works on Debian/Ubuntu and Fedora/RHEL/Rocky/Alma VPS images (e.g. Doprax).
#
#   Interactive:   sudo bash install.sh
#   With domain:   sudo bash install.sh --domain panel.example.com
#   Unattended:    sudo BOT_TOKEN=... OWNER_ID=... DOMAIN=panel.example.com bash install.sh --yes
#   Help:          bash install.sh --help
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/prgram}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$INSTALL_DIR/.env"
CONTAINER="prgram"
REQUIRED_FILES=(app.py panel_ui.html requirements.txt Dockerfile docker-compose.yml Caddyfile)

if [[ -t 1 ]]; then
  c_ok=$'\e[32m'; c_err=$'\e[31m'; c_inf=$'\e[36m'; c_warn=$'\e[33m'; c_off=$'\e[0m'
else
  c_ok=""; c_err=""; c_inf=""; c_warn=""; c_off=""
fi
ok()   { echo "${c_ok}✔${c_off} $*"; }
inf()  { echo "${c_inf}➜${c_off} $*"; }
warn() { echo "${c_warn}⚠${c_off} $*"; }
die()  { echo "${c_err}✖ $*${c_off}" >&2; exit 1; }

usage() {
  cat <<'USAGE'
PR GRAM Helper installer

Usage: sudo bash install.sh [options]

Options:
  --domain <name>   Enable automatic HTTPS for this domain (A record must point here)
  -y, --yes         Non-interactive: never prompt (BOT_TOKEN and OWNER_ID must be provided)
  -h, --help        Show this help

Environment variables (all optional in interactive mode):
  BOT_TOKEN, OWNER_ID, API_ID, API_HASH   first-install settings
  DOMAIN                                  same as --domain
  INSTALL_DIR                             default /opt/prgram
  FORCE=1                                 continue even if DNS does not point to this server
USAGE
}

DOMAIN="${DOMAIN:-}"
ASSUME_YES="${ASSUME_YES:-}"
while (($#)); do
  case "$1" in
    --domain)   [[ -n "${2:-}" ]] || die "--domain needs a value"; DOMAIN="$2"; shift 2 ;;
    --domain=*) DOMAIN="${1#*=}"; shift ;;
    -y|--yes)   ASSUME_YES=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *)          die "Unknown option: $1 (try --help)" ;;
  esac
done

INTERACTIVE=0
if [[ -t 0 && -z "$ASSUME_YES" ]]; then INTERACTIVE=1; fi

trap 'die "Failed at line $LINENO: $BASH_COMMAND"' ERR
trap 'die "Interrupted"' INT TERM

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash install.sh"
[[ "$(uname -s)" == "Linux" ]] || die "This installer supports Linux only"

# ---------- helpers ----------
same_dir() { [[ "$SRC_DIR" -ef "$INSTALL_DIR" ]]; }

find_dotfile() { # find_dotfile .env.example -> prints the path (the dot may have been replaced by "_" on upload)
  local f=$1
  if   [[ -f "$SRC_DIR/$f" ]];       then echo "$SRC_DIR/$f"
  elif [[ -f "$SRC_DIR/_${f#.}" ]];  then echo "$SRC_DIR/_${f#.}"
  fi
}

env_get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' || true; }

set_env() { # set_env KEY VALUE  (safe for values containing | & \)
  local k=$1 v=$2 esc
  esc="$(printf '%s' "$v" | sed -e 's/[\\|&]/\\&/g')"
  if [[ -s "$ENV_FILE" && -n "$(tail -c1 "$ENV_FILE")" ]]; then echo >> "$ENV_FILE"; fi
  if grep -qE "^${k}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${k}=.*|${k}=${esc}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$k" "$v" >> "$ENV_FILE"
  fi
}

prompt() { # prompt VAR "text"   (input is visible; whitespace, CR and quotes are stripped)
  local __var=$1 __txt=$2 __v=""
  if ! read -rp "$__txt" __v && [[ -z "$__v" ]]; then echo; die "Input ended (Ctrl+D or no terminal). Re-run interactively, or pass values as environment variables with --yes"; fi
  __v="$(printf '%s' "$__v" | tr -d '\r' | sed -E "s/^[[:space:]\"']+//; s/[[:space:]\"']+\$//")"
  printf -v "$__var" '%s' "$__v"
}

prompt_valid() { # prompt_valid VAR "text" REGEX "error message"   (re-asks until valid)
  local __pvar=$1 __txt=$2 __re=$3 __msg=$4 __pv=""
  while true; do
    prompt __pv "$__txt"
    [[ "$__pv" =~ $__re ]] && break
    warn "$__msg"
  done
  printf -v "$__pvar" '%s' "$__pv"
}

public_ip() { curl -4fsS --max-time 6 https://api.ipify.org 2>/dev/null || curl -4fsS --max-time 6 https://ifconfig.me 2>/dev/null || true; }

open_ports() { # open_ports 80/tcp 443/tcp ...
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    local p; for p in "$@"; do ufw allow "$p" >/dev/null; done
    ok "ufw: opened $*"
  elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    local p; for p in "$@"; do firewall-cmd --permanent --add-port="$p" >/dev/null; done
    firewall-cmd --reload >/dev/null
    ok "firewalld: opened $*"
  fi
}

wait_healthy() { # waits up to ~2 minutes for the app container
  local st i
  for ((i = 0; i < 60; i++)); do
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER" 2>/dev/null || echo missing)"
    case "$st" in
      healthy|running) return 0 ;;
      unhealthy|exited|dead) return 1 ;;
    esac
    sleep 2
  done
  return 1
}

# ---------- 0) preflight (fail fast, before changing anything) ----------
if ! same_dir; then
  for f in "${REQUIRED_FILES[@]}"; do
    [[ -f "$SRC_DIR/$f" ]] || die "File $f is missing next to install.sh"
  done
  for f in .dockerignore .env.example; do
    [[ -n "$(find_dotfile "$f")" ]] || die "File $f (or _${f#.}) is missing next to install.sh"
  done
fi

if command -v apt-get >/dev/null; then PKG_MGR=apt
elif command -v dnf >/dev/null;   then PKG_MGR=dnf
elif command -v yum >/dev/null;   then PKG_MGR=yum
else die "Unsupported package manager (need apt, dnf or yum)"; fi

mkdir -p "$INSTALL_DIR"
avail_mb="$(df --output=avail -BM "$INSTALL_DIR" | tail -1 | tr -dc '0-9')"
(( avail_mb >= 1500 )) || warn "Only ${avail_mb}MB free on the install disk; the Docker build needs about 1.5GB"

# ---------- 1) prerequisites ----------
inf "Installing prerequisites..."
case "$PKG_MGR" in
  apt) export DEBIAN_FRONTEND=noninteractive
       apt-get update -y >/dev/null
       apt-get install -y curl ca-certificates openssl tar gzip >/dev/null ;;
  dnf) dnf install -y curl ca-certificates openssl tar gzip >/dev/null ;;
  yum) yum install -y curl ca-certificates openssl tar gzip >/dev/null ;;
esac

if ! command -v docker >/dev/null; then
  inf "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
if ! docker compose version >/dev/null 2>&1; then
  inf "Installing the Docker Compose plugin..."
  case "$PKG_MGR" in
    apt) apt-get install -y docker-compose-plugin >/dev/null || true ;;
    *)   "$PKG_MGR" install -y docker-compose-plugin >/dev/null || true ;;
  esac
fi
docker compose version >/dev/null 2>&1 || die "docker compose v2 not found (install the docker-compose-plugin package)"
docker info >/dev/null 2>&1 || die "The Docker daemon is not running (try: systemctl start docker)"
ok "Docker is ready"

# ---------- 2) files ----------
mkdir -p "$INSTALL_DIR/data"
if same_dir; then
  inf "Running from the install directory; skipping file copy"
else
  for f in "${REQUIRED_FILES[@]}"; do cp -f "$SRC_DIR/$f" "$INSTALL_DIR/$f"; done
  for f in .dockerignore .env.example; do cp -f "$(find_dotfile "$f")" "$INSTALL_DIR/$f"; done
fi
chown -R 1000:1000 "$INSTALL_DIR/data"   # the user inside the container is uid 1000
ok "Files are in $INSTALL_DIR"

# ---------- 3) .env settings ----------
[[ -f "$ENV_FILE" ]] && sed -i 's/\r$//' "$ENV_FILE"   # fixes a .env created on Windows (CRLF)
if [[ -f "$ENV_FILE" ]]; then
  ok ".env already exists; leaving your settings untouched"
  [[ -n "$(env_get BOT_TOKEN)" && -n "$(env_get OWNER_ID)" ]] || warn "BOT_TOKEN or OWNER_ID is empty in $ENV_FILE; edit it with: selfpanel config"
else
  cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  BOT_TOKEN="${BOT_TOKEN:-}"; OWNER_ID="${OWNER_ID:-}"; API_ID="${API_ID:-}"; API_HASH="${API_HASH:-}"
  BOT_TOKEN="${BOT_TOKEN//[[:space:]]/}"; OWNER_ID="${OWNER_ID//[[:space:]]/}"; API_ID="${API_ID//[[:space:]]/}"; API_HASH="${API_HASH//[[:space:]]/}"

  if (( INTERACTIVE )); then
    [[ "$BOT_TOKEN" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{20,}$ ]] || \
      prompt_valid BOT_TOKEN "BOT_TOKEN (from BotFather): " '^[0-9]{5,}:[A-Za-z0-9_-]{20,}$' "That does not look like a bot token (expected something like 123456789:AAH...; paste only the token, no bot prefix)"
    [[ "$OWNER_ID" =~ ^[0-9]+$ ]] || \
      prompt_valid OWNER_ID "OWNER_ID (your numeric Telegram ID): " '^[0-9]+$' "OWNER_ID must be a number"
    if [[ -z "$API_ID" ]]; then
      prompt_valid API_ID "API_ID from my.telegram.org (empty = shared default, not recommended): " '^[0-9]*$' "API_ID must be a number or empty"
    fi
    if [[ -n "$API_ID" && -z "$API_HASH" ]]; then
      prompt_valid API_HASH "API_HASH: " '^[0-9a-fA-F]{32}$' "API_HASH must be 32 hex characters"
    fi
  fi

  [[ "$BOT_TOKEN" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{20,}$ ]] || die "BOT_TOKEN is missing or invalid (provide it as an environment variable with --yes)"
  [[ "$OWNER_ID" =~ ^[0-9]+$ ]]                         || die "OWNER_ID is missing or invalid"
  [[ -z "$API_ID" || "$API_ID" =~ ^[0-9]+$ ]]           || die "API_ID must be a number"
  [[ -z "$API_ID" || "$API_HASH" =~ ^[0-9a-fA-F]{32}$ ]] || die "API_HASH is required with API_ID and must be 32 hex characters"

  SESSION_KEY="$(openssl rand -base64 32 | tr '+/' '-_')"   # Fernet key format (urlsafe base64, 32 bytes)
  set_env BOT_TOKEN   "$BOT_TOKEN"
  set_env OWNER_ID    "$OWNER_ID"
  set_env SESSION_KEY "$SESSION_KEY"
  set_env API_ID      "$API_ID"
  set_env API_HASH    "$API_HASH"
  ok ".env created"
  warn "SESSION_KEY is stored in $ENV_FILE. Keep a copy somewhere safe (off the server); without it the saved sessions cannot be recovered."
fi
chmod 600 "$ENV_FILE"

# ---------- 3.2) cap CPU_LIMIT to the cores this server really has (Docker rejects more) ----------
cores="$(nproc 2>/dev/null || echo 1)"
cpu_limit="$(env_get CPU_LIMIT)"; cpu_limit="${cpu_limit:-1.5}"
if awk -v l="$cpu_limit" -v c="$cores" 'BEGIN{exit !(l+0 > c+0)}'; then
  set_env CPU_LIMIT "$cores"
  warn "CPU_LIMIT=$cpu_limit is more than the $cores core(s) on this server; set to $cores"
fi

# ---------- 3.5) domain + automatic HTTPS ----------
DOMAIN="${DOMAIN:-$(env_get DOMAIN)}"
if [[ -z "$DOMAIN" ]] && (( INTERACTIVE )); then
  prompt DOMAIN "Panel domain (e.g. panel.example.com; its A record must point to this server; empty = no domain): "
fi
DOMAIN="$(echo "${DOMAIN:-}" | tr '[:upper:]' '[:lower:]' | sed -E 's#^https?://##; s#/.*$##' | tr -d '[:space:]')"

HOST_PORT="$(env_get HOST_PORT)"; HOST_PORT="${HOST_PORT:-8787}"
SERVER_IP="$(public_ip)"

if [[ -n "$DOMAIN" ]]; then
  [[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "Invalid domain: $DOMAIN"

  DNS_IP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)"
  inf "Server IP: ${SERVER_IP:-unknown}   |   Domain A record: ${DNS_IP:-not found}"
  if [[ -z "$DNS_IP" || ( -n "$SERVER_IP" && "$DNS_IP" != "$SERVER_IP" ) ]]; then
    warn "The domain's A record does not point to this server (or has not propagated yet)."
    echo "  Create an A record for $DOMAIN with value ${SERVER_IP:-the server IP} (on Cloudflare, set the cloud to gray / DNS only)."
    if (( INTERACTIVE )) && [[ -z "${FORCE:-}" ]]; then
      read -rp "Continue anyway? (the SSL certificate cannot be issued until DNS is correct) [y/N] " a || a=""
      [[ "$a" =~ ^[Yy]$ ]] || die "Fix DNS first and run again (or use FORCE=1)"
    fi
  fi

  # ports 80/443 must not be used by another service (nginx/apache); ignore our own Caddy
  if command -v ss >/dev/null && ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx prgram-caddy; then
    busy="$(ss -tlnp 2>/dev/null | awk '$4 ~ /:(80|443)$/ && $0 !~ /docker-proxy/ {print $4" "$NF}' | head -3 || true)"
    [[ -z "$busy" ]] || die "Port 80/443 is used by another service: $busy  (stop it, e.g. systemctl disable --now nginx apache2)"
  fi

  open_ports 80/tcp 443/tcp 443/udp
  set_env DOMAIN "$DOMAIN"
  set_env WEB_BASE_URL "https://$DOMAIN"
  set_env TRUST_PROXY 1
  set_env BIND_IP 127.0.0.1
  set_env COMPOSE_PROFILES domain
  ok "Domain configured: https://$DOMAIN"
  echo "  (If your VPS provider has its own firewall, open ports 80 and 443 there too)"
else
  open_ports "$HOST_PORT/tcp"
  inf "No domain: the panel will be available at http://${SERVER_IP:-SERVER_IP}:$HOST_PORT (add a domain later with: selfpanel domain)"
fi

# ---------- 4) selfpanel ----------
if [[ -f "$SRC_DIR/selfpanel" ]]; then
  install -m 0755 "$SRC_DIR/selfpanel" /usr/local/bin/selfpanel.new
  mv -f /usr/local/bin/selfpanel.new /usr/local/bin/selfpanel
  ok "selfpanel command installed"
fi

# ---------- 5) swap for low-RAM servers ----------
mem_mb="$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)"
if (( mem_mb < 1500 )) && ! swapon --show | grep -q .; then
  inf "Low RAM (${mem_mb}MB); creating 1GB swap..."
  fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ok "Swap enabled"
fi

# ---------- 6) build and run ----------
inf "Building and starting (the first build can take a few minutes)..."
cd "$INSTALL_DIR"
docker compose up -d --build --remove-orphans
inf "Waiting for the app to become healthy..."
if wait_healthy; then
  ok "App is healthy"
else
  docker compose ps || true
  docker logs --tail 40 "$CONTAINER" 2>&1 || true
  die "The app did not become healthy. Check the log above (or: selfpanel logs)"
fi
docker compose ps

if [[ -n "$DOMAIN" ]]; then
  inf "Requesting the SSL certificate (up to ~60 seconds)..."
  https_ok=0
  for _ in $(seq 1 15); do
    if curl -fsS --max-time 5 -o /dev/null "https://$DOMAIN/healthz" 2>/dev/null; then https_ok=1; break; fi
    sleep 4
  done
  if (( https_ok )); then ok "HTTPS is active: https://$DOMAIN"
  else warn "HTTPS is not responding yet; check the log: docker logs prgram-caddy"; fi
fi

# ---------- 7) summary ----------
echo
ok "Installation finished. To manage the service type:  ${c_inf}selfpanel${c_off}"
for _ in $(seq 1 15); do [[ -s "$INSTALL_DIR/data/web_token.txt" ]] && break; sleep 2; done
if [[ -x /usr/local/bin/selfpanel ]]; then INSTALL_DIR="$INSTALL_DIR" /usr/local/bin/selfpanel link || true; fi
