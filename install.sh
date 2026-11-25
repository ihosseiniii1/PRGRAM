#!/usr/bin/env bash
# PR GRAM Helper - نصب‌کننده (Ubuntu/Debian/Fedora/Rocky ... روی VPS مثل Doprax)
# استفاده:  sudo bash install.sh
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/prgram}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

c_ok=$'\e[32m'; c_err=$'\e[31m'; c_inf=$'\e[36m'; c_off=$'\e[0m'
ok()  { echo "${c_ok}✔${c_off} $*"; }
inf() { echo "${c_inf}➜${c_off} $*"; }
die() { echo "${c_err}✖ $*${c_off}" >&2; exit 1; }
trap 'die "خطا در خط $LINENO"' ERR

[[ $EUID -eq 0 ]] || die "با root اجرا کن: sudo bash install.sh"

# ---------- 1) پیش‌نیازها ----------
inf "نصب پیش‌نیازها..."
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y && apt-get install -y curl ca-certificates openssl tar gzip
elif command -v dnf >/dev/null; then
  dnf install -y curl ca-certificates openssl tar gzip
elif command -v yum >/dev/null; then
  yum install -y curl ca-certificates openssl tar gzip
else
  die "پکیج‌منیجر پشتیبانی نمی‌شه"
fi

if ! command -v docker >/dev/null; then
  inf "نصب Docker..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null 2>&1 || die "docker compose v2 پیدا نشد (پکیج docker-compose-plugin رو نصب کن)"
ok "Docker آماده است"

# ---------- 2) فایل‌ها ----------
mkdir -p "$INSTALL_DIR/data"
for f in app.py panel_ui.html requirements.txt Dockerfile docker-compose.yml Caddyfile; do
  [[ -f "$SRC_DIR/$f" ]] || die "فایل $f کنار install.sh نیست"
  cp -f "$SRC_DIR/$f" "$INSTALL_DIR/$f"
done
# فایل‌های نقطه‌دار (ممکن است موقع آپلود/دانلود با پیشوند _ تغییر نام داده باشند)
for f in .dockerignore .env.example; do
  if   [[ -f "$SRC_DIR/$f" ]];  then cp -f "$SRC_DIR/$f"  "$INSTALL_DIR/$f"
  elif [[ -f "$SRC_DIR/_${f#.}" ]]; then cp -f "$SRC_DIR/_${f#.}" "$INSTALL_DIR/$f"
  else die "فایل $f کنار install.sh نیست"; fi
done
chown -R 1000:1000 "$INSTALL_DIR/data"   # کاربر داخل کانتینر uid 1000 است
ok "فایل‌ها در $INSTALL_DIR کپی شد"

# ---------- 3) تنظیمات .env ----------
ENV_FILE="$INSTALL_DIR/.env"
sed -i 's/\r$//' "$ENV_FILE" 2>/dev/null || true   # اگر .env با ویندوز ساخته شده (CRLF) درست می‌شود
env_get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }
set_env() { # set_env KEY VALUE
  [[ -s "$ENV_FILE" && -n "$(tail -c1 "$ENV_FILE")" ]] && echo >> "$ENV_FILE"
  if grep -qE "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
if [[ -f "$ENV_FILE" ]]; then
  ok ".env موجود است؛ دست نمی‌خورد"
else
  cp "$INSTALL_DIR/.env.example" "$ENV_FILE"
  read -rp "BOT_TOKEN (از BotFather): " BOT_TOKEN
  read -rp "OWNER_ID (آیدی عددی تلگرام خودت): " OWNER_ID
  read -rp "API_ID (خالی = پیش‌فرض عمومی، پیشنهاد نمی‌شه): " API_ID || true
  API_HASH=""; [[ -n "${API_ID:-}" ]] && read -rp "API_HASH: " API_HASH
  [[ -n "$BOT_TOKEN" && "$OWNER_ID" =~ ^[0-9]+$ ]] || die "BOT_TOKEN یا OWNER_ID نامعتبر است"
  SESSION_KEY="$(head -c32 /dev/urandom | base64 | tr '+/' '-_')"   # فرمت کلید Fernet
  sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$BOT_TOKEN|;s|^OWNER_ID=.*|OWNER_ID=$OWNER_ID|;s|^SESSION_KEY=.*|SESSION_KEY=$SESSION_KEY|;s|^API_ID=.*|API_ID=${API_ID:-}|;s|^API_HASH=.*|API_HASH=${API_HASH:-}|" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok ".env ساخته شد"
  echo "${c_err}⚠ SESSION_KEY داخل $ENV_FILE است. یک نسخه از آن را جای امن (بیرون از سرور) نگه دار؛ بدون آن نشست‌ها قابل بازیابی نیستند.${c_off}"
fi

# ---------- 3.5) دامنه + HTTPS خودکار ----------
# استفاده غیرتعاملی:  sudo DOMAIN=panel.example.com bash install.sh
DOMAIN="${DOMAIN:-$(env_get DOMAIN)}"
if [[ -z "$DOMAIN" ]]; then
  read -rp "دامنه‌ی پنل (مثلا panel.example.com — رکورد A باید به IP این سرور اشاره کند؛ خالی = بدون دامنه): " DOMAIN || true
fi
DOMAIN="$(echo "${DOMAIN:-}" | tr 'A-Z' 'a-z' | sed -E 's#^https?://##; s#/.*$##; s#\s##g')"

if [[ -n "$DOMAIN" ]]; then
  [[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "دامنه نامعتبر است: $DOMAIN"

  SERVER_IP="$(curl -4fsS --max-time 6 https://api.ipify.org 2>/dev/null || curl -4fsS --max-time 6 https://ifconfig.me 2>/dev/null || true)"
  DNS_IP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')"
  inf "IP سرور: ${SERVER_IP:-نامشخص}   |   رکورد A دامنه: ${DNS_IP:-پیدا نشد}"
  if [[ -z "$DNS_IP" || ( -n "$SERVER_IP" && "$DNS_IP" != "$SERVER_IP" ) ]]; then
    echo "${c_err}⚠ رکورد A دامنه هنوز به این سرور اشاره نمی‌کند (یا هنوز پخش نشده).${c_off}"
    echo "  در DNS یک رکورد  A  برای $DOMAIN با مقدار ${SERVER_IP:-IP سرور}  بساز (اگر Cloudflare داری ابر را خاکستری/DNS only بگذار)."
    if [[ -t 0 && -z "${FORCE:-}" ]]; then
      read -rp "با این حال ادامه بدهم؟ (SSL تا درست شدن DNS گرفته نمی‌شود) [y/N] " a
      [[ "$a" =~ ^[Yy]$ ]] || die "اول DNS را درست کن و دوباره اجرا کن (یا FORCE=1)"
    fi
  fi

  # پورت‌های 80/443 نباید توسط سرویس دیگری (nginx/apache) گرفته شده باشند
  busy="$(ss -tlnp 2>/dev/null | awk '$4 ~ /:(80|443)$/ && $0 !~ /docker-proxy/ {print $4" "$NF}' | head -3 || true)"
  [[ -z "$busy" ]] || die "پورت 80/443 توسط سرویس دیگری استفاده می‌شود: $busy  (آن را متوقف کن؛ مثلا systemctl disable --now nginx apache2)"

  # فایروال سیستم‌عامل (اگر ufw فعال است)
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
    ok "ufw: پورت‌های 80/443 باز شد"
  fi

  set_env DOMAIN "$DOMAIN"
  set_env WEB_BASE_URL "https://$DOMAIN"
  set_env TRUST_PROXY 1
  set_env BIND_IP 127.0.0.1
  set_env COMPOSE_PROFILES domain
  ok "دامنه تنظیم شد: https://$DOMAIN"
  echo "  (اگر فایروال پنل Doprax داری، پورت‌های 80 و 443 را هم آنجا باز کن)"
else
  inf "بدون دامنه: پنل روی http://IP:$(env_get HOST_PORT) در دسترس است (بعدا: selfpanel domain)"
fi

# ---------- 4) selfpanel ----------
if [[ -f "$SRC_DIR/selfpanel" ]]; then
  install -m 0755 "$SRC_DIR/selfpanel" /usr/local/bin/selfpanel
  ok "دستور selfpanel نصب شد"
fi

# ---------- 5) swap برای سرور کم‌رم ----------
mem_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
if (( mem_mb < 1500 )) && ! swapon --show | grep -q .; then
  inf "RAM کم است (${mem_mb}MB)؛ ساخت 1GB swap..."
  fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ok "swap فعال شد"
fi

# ---------- 6) بیلد و اجرا ----------
inf "بیلد و اجرا..."
cd "$INSTALL_DIR"
docker compose up -d --build --remove-orphans
sleep 8
docker compose ps
if [[ -n "${DOMAIN:-}" ]]; then
  inf "در حال گرفتن گواهی SSL (تا ~۳۰ ثانیه)..."
  for _ in $(seq 1 15); do
    curl -fsS --max-time 5 -o /dev/null "https://$DOMAIN/healthz" 2>/dev/null && { ok "HTTPS فعال است: https://$DOMAIN"; break; }; sleep 4
  done
  curl -fsS --max-time 5 -o /dev/null "https://$DOMAIN/healthz" 2>/dev/null || echo "${c_err}⚠ هنوز HTTPS جواب نمی‌دهد؛ لاگ: docker logs prgram-caddy${c_off}"
fi
echo
ok "نصب تمام شد. برای مدیریت تایپ کن:  ${c_inf}selfpanel${c_off}"
echo "لینک ادمین: selfpanel  ←  گزینه Admin link"
