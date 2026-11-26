#!/bin/bash
# Vorot - Camera & Gate Control: встановлення
#
# Рекомендовано: спершу завантажити й переглянути, потім запустити
#   wget https://tehnodron.in.ua/camera-control/install.txt -O install.sh
#   sudo bash install.sh
#
# На платах з малою пам'яттю (NanoPi NEO, 256 МБ) запускайте у фоні,
# щоб обрив SSH не перервав установку пакетів:
#   sudo systemd-run --unit=vorot-install bash "$PWD/install.sh"
#   journalctl -u vorot-install -f
#
# Змінні середовища (необов'язкові):
#   LISTEN_PORT=5000   порт веб-інтерфейсу (за замовчуванням 8080)
#   FORCE=1            перевстановити поверх наявної програми (БД зберігається)

set -euo pipefail

REPO_URL="${UPDATE_REPO_URL:-https://tehnodron.in.ua/camera-control}"
INSTALL_DIR="${INSTALL_DIR:-/opt/camera-control}"
SERVICE_NAME="${SERVICE_NAME:-camera-control}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
EARLYOOM_CONF="${EARLYOOM_CONF:-/etc/default/earlyoom}"
LISTEN_PORT="${LISTEN_PORT:-8080}"
APP="camera_control.py"

die()  { echo "✗ $*" >&2; exit 1; }
info() { echo "→ $*"; }
ok()   { echo "✓ $*"; }
warn() { echo "⚠ $*" >&2; }

[ "$(id -u)" -eq 0 ] || die "Запустіть від root: sudo bash install.sh"
[[ "$LISTEN_PORT" =~ ^[0-9]+$ ]] || die "LISTEN_PORT має бути числом"

if [ -f /etc/os-release ]; then
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
        *" debian "*|*" ubuntu "*) ;;
        *) die "Підтримуються Debian / Ubuntu / Armbian, виявлено: ${ID:-?}" ;;
    esac
fi

if [ -f "$INSTALL_DIR/$APP" ] && [ "${FORCE:-0}" != "1" ]; then
    die "Програма вже встановлена в $INSTALL_DIR. Для оновлення: update.sh (або FORCE=1 для перевстановлення)"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "=================================="
echo " Vorot - встановлення"
echo "=================================="

# ---------- пам'ять ----------
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
SWAP_MB=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
info "Пам'ять: ${MEM_MB} МБ, своп: ${SWAP_MB} МБ"
if [ "$MEM_MB" -lt 512 ] && [ "$SWAP_MB" -lt 256 ]; then
    warn "Мало пам'яті і майже немає свопу - установка ffmpeg може підвісити плату."
    warn "Увімкніть zram (на Armbian є за замовчуванням) і запустіть знову."
    [ "${FORCE_LOWMEM:-0}" = "1" ] || die "Перервано. Щоб продовжити попри це: FORCE_LOWMEM=1"
fi

# ---------- пакети ----------
export DEBIAN_FRONTEND=noninteractive
info "Встановлення пакетів (без необов'язкових залежностей)..."
apt-get update
apt-get install -y --no-install-recommends python3 python3-flask python3-pip ca-certificates curl earlyoom

pip3 install OPi.GPIO --break-system-packages \
    || warn "OPi.GPIO не встановлено - кнопки GPIO не працюватимуть (це не Orange/NanoPi?)"

# earlyoom: при нестачі пам'яті першими вбиває ffmpeg і apt, програму береже
echo 'EARLYOOM_ARGS="-m 5 -s 10 -r 3600 --prefer ^(ffmpeg|apt|apt-get|dpkg)$ --avoid ^python3$"' > "$EARLYOOM_CONF"
systemctl restart earlyoom || warn "earlyoom не запустився"

info "Встановлення ffmpeg (найважчий пакет, може тривати 10-30 хв на слабкій платі)..."
apt-get install -y --no-install-recommends ffmpeg \
    || warn "ffmpeg не встановлено - відео з камер не працюватиме, кнопки працюють"

# ---------- програма ----------
info "Перевірка опублікованої версії..."
curl -fsSL "$REPO_URL/version.json" -o "$TMP/version.json" \
    || die "Не вдалося завантажити $REPO_URL/version.json"

read -r URL SHA VERSION < <(python3 -I - "$TMP/version.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    m = d.get("files", {}).get("main", {})
    print(m.get("url", "-"), str(m.get("sha256", "-")).lower(), d.get("version", "-"))
except Exception as e:
    print("version.json:", type(e).__name__, e, file=sys.stderr)
    print("- - -")
PY
)
[ -n "${VERSION:-}" ] && [ "$VERSION" != "-" ] || die "Некоректний version.json на сервері"
[[ "$URL" == https://* ]] || die "URL програми має бути https://, отримано: $URL"
[[ "$SHA" =~ ^[0-9a-f]{64}$ ]] || die "У version.json немає коректного sha256"

info "Завантаження версії $VERSION..."
curl -fsSL "$URL" -o "$TMP/$APP" || die "Не вдалося завантажити $URL"
echo "$SHA  $TMP/$APP" | sha256sum -c --quiet - || die "SHA-256 не збігається - файл пошкоджений або підмінений"
ok "SHA-256 перевірено"

[ "$(head -c 2 "$TMP/$APP")" = "#!" ] || die "Завантажений файл не є Python-скриптом"
FILE_VERSION=$(grep -m1 '^CURRENT_VERSION' "$TMP/$APP" | cut -d'"' -f2)
[ "$FILE_VERSION" = "$VERSION" ] || die "Версія у файлі ($FILE_VERSION) не збігається з version.json ($VERSION)"
python3 -m py_compile "$TMP/$APP" || die "Синтаксична помилка в програмі"

mkdir -p "$INSTALL_DIR"
install -m 755 "$TMP/$APP" "$INSTALL_DIR/$APP.new"
mv -f "$INSTALL_DIR/$APP.new" "$INSTALL_DIR/$APP"
ok "Програма: $INSTALL_DIR/$APP"

# ---------- сервіс ----------
cat > "$SYSTEMD_DIR/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Vorot - Camera & Gate Control
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/$APP
Environment=LISTEN_PORT=$LISTEN_PORT
OOMScoreAdjust=-500
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 3
systemctl is-active --quiet "$SERVICE_NAME" \
    || die "Сервіс не запустився. Журнал: journalctl -u $SERVICE_NAME -n 50"

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "=================================="
ok "Встановлено версію $VERSION"
echo "=================================="
echo " Веб-інтерфейс: http://${IP:-<ip>}:$LISTEN_PORT"
echo " Перший вхід:   admin / admin  (одразу попросить змінити пароль)"
echo " Журнал:        journalctl -u $SERVICE_NAME -f"
echo
echo " Не відкривайте порт $LISTEN_PORT в інтернет без VPN."
echo "=================================="
