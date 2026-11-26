#!/bin/bash
# Vorot - Camera & Gate Control: оновлення з командного рядка
#
# Зазвичай оновлюються з веб-інтерфейсу (Налаштування -> Оновлення).
# Цей скрипт - для пристроїв, де кнопка оновлення не працює
# (версії 4.4.1-4.4.5: зламаний JavaScript сторінки) або немає доступу до інтерфейсу.
#
#   wget https://tehnodron.in.ua/camera-control/update.txt -O update.sh
#   sudo bash update.sh
#
# Змінні середовища (необов'язкові):
#   YES=1    не питати підтвердження
#   FORCE=1  встановити навіть якщо версія на сервері не новіша

set -euo pipefail

REPO_URL="${UPDATE_REPO_URL:-https://tehnodron.in.ua/camera-control}"
INSTALL_DIR="${INSTALL_DIR:-/opt/camera-control}"
SERVICE_NAME="${SERVICE_NAME:-camera-control}"
APP="camera_control.py"
BACKUP_DIR="$INSTALL_DIR/backups"
KEEP=5

die()  { echo "✗ $*" >&2; exit 1; }
info() { echo "→ $*"; }
ok()   { echo "✓ $*"; }

[ "$(id -u)" -eq 0 ] || die "Запустіть від root: sudo bash update.sh"
[ -f "$INSTALL_DIR/$APP" ] || die "Програму не знайдено: $INSTALL_DIR/$APP (спершу install.sh)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

CURRENT=$(grep -m1 '^CURRENT_VERSION' "$INSTALL_DIR/$APP" | cut -d'"' -f2)
[ -n "$CURRENT" ] || CURRENT="0"
info "Встановлена версія: $CURRENT"

curl -fsSL "$REPO_URL/version.json" -o "$TMP/version.json" \
    || die "Не вдалося завантажити $REPO_URL/version.json"

read -r URL SHA VERSION NEWER < <(python3 -I - "$TMP/version.json" "$CURRENT" <<'PY'
import json, re, sys
ver = lambda v: tuple(int(x) for x in re.findall(r"\d+", str(v))) or (0,)
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    m = d.get("files", {}).get("main", {})
    new = str(d.get("version", "-"))
    print(m.get("url", "-"), str(m.get("sha256", "-")).lower(), new,
          1 if ver(new) > ver(sys.argv[2]) else 0)
except Exception as e:
    print("version.json:", type(e).__name__, e, file=sys.stderr)
    print("- - - 0")
PY
)
[ -n "${VERSION:-}" ] && [ "$VERSION" != "-" ] || die "Некоректний version.json на сервері"
info "Версія на сервері: $VERSION"

if [ "$NEWER" != "1" ] && [ "${FORCE:-0}" != "1" ]; then
    ok "Оновлення не потрібне"
    exit 0
fi

[[ "$URL" == https://* ]] || die "URL програми має бути https://, отримано: $URL"
[[ "$SHA" =~ ^[0-9a-f]{64}$ ]] || die "У version.json немає коректного sha256 - оновлення відхилено"

# Питаємо лише в інтерактивному терміналі (при curl | bash stdin - не термінал)
if [ -t 0 ] && [ "${YES:-0}" != "1" ]; then
    read -r -p "Оновити $CURRENT -> $VERSION? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 0
fi

# ---------- завантаження і перевірки ----------
info "Завантаження..."
curl -fsSL "$URL" -o "$TMP/$APP" || die "Не вдалося завантажити $URL"
echo "$SHA  $TMP/$APP" | sha256sum -c --quiet - || die "SHA-256 не збігається - файл пошкоджений або підмінений"
ok "SHA-256 перевірено"

[ "$(head -c 2 "$TMP/$APP")" = "#!" ] || die "Завантажений файл не є Python-скриптом"
FILE_VERSION=$(grep -m1 '^CURRENT_VERSION' "$TMP/$APP" | cut -d'"' -f2)
[ "$FILE_VERSION" = "$VERSION" ] || die "Версія у файлі ($FILE_VERSION) не збігається з version.json ($VERSION)"
python3 -m py_compile "$TMP/$APP" || die "Синтаксична помилка в новій версії"

# Пробний запуск без старту сервера: відсутні модулі, помилки при імпорті
info "Пробний запуск нової версії..."
( cd "$INSTALL_DIR" && CAMERA_CONTROL_PREFLIGHT=1 timeout 120 python3 -c \
    "import runpy, sys; runpy.run_path(sys.argv[1], run_name='__preflight__')" "$TMP/$APP" ) \
    > "$TMP/preflight.log" 2>&1 \
    || die "Нова версія не запускається: $(grep -v '^\s*$' "$TMP/preflight.log" | tail -1)"
ok "Пробний запуск пройдено"

# ---------- резервні копії ----------
# Назва як у програми - кнопка "Відкотити" в інтерфейсі побачить цю копію
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/camera_control-$CURRENT-$STAMP.py"
cp -p "$INSTALL_DIR/$APP" "$BACKUP"
if [ -f "$INSTALL_DIR/cameras.db" ]; then
    python3 -I - "$INSTALL_DIR/cameras.db" "$BACKUP_DIR/cameras-$STAMP.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
    chmod 600 "$BACKUP_DIR/cameras-$STAMP.db"
fi
ok "Резервна копія: $BACKUP"

# ---------- заміна і перезапуск ----------
install -m 755 "$TMP/$APP" "$INSTALL_DIR/$APP.new"
mv -f "$INSTALL_DIR/$APP.new" "$INSTALL_DIR/$APP"
rm -f "$INSTALL_DIR/version.txt"          # застарілий файл версій до 4.4.5

info "Перезапуск сервісу $SERVICE_NAME..."
systemctl restart "$SERVICE_NAME"
sleep 5
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "✗ Сервіс не запустився - повертаю версію $CURRENT" >&2
    cp -p "$BACKUP" "$INSTALL_DIR/$APP"
    systemctl restart "$SERVICE_NAME" || true
    die "Оновлення скасовано. Журнал: journalctl -u $SERVICE_NAME -n 50"
fi

# Лишаємо останні $KEEP копій програми і бази
ls -1t "$BACKUP_DIR"/camera_control-*.py 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f || true
ls -1t "$BACKUP_DIR"/cameras-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f || true

echo "=================================="
ok "Оновлено: $CURRENT -> $VERSION"
echo "=================================="
python3 -I - "$TMP/version.json" <<'PY' || true
import json, sys
for c in json.load(open(sys.argv[1], encoding="utf-8")).get("changelog", []):
    print("  -", c)
PY
