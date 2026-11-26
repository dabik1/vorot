#!/usr/bin/env python3
# Camera Control System

from flask import Flask, Response, render_template_string, request, redirect, url_for, session, jsonify
from functools import wraps
from datetime import timedelta, datetime
import subprocess
import threading
import time
import sqlite3
import hashlib
import socket
import json
import os
import urllib.request
import urllib.parse
import sys
import re
import signal
import shutil
import secrets
import ssl
import hmac
from werkzeug.security import generate_password_hash, check_password_hash

# ========== VERSION & UPDATE CONFIG ==========
CURRENT_VERSION = "4.4.9"
# Default repository URL - can be overridden with environment variable
REPO_URL = os.environ.get('UPDATE_REPO_URL', 'https://tehnodron.in.ua/camera-control')

# Робота за nginx у підтеці, наприклад http://ip:5000/vorot/
#   URL_PREFIX=/vorot      - підтека (порожньо = корінь сайту)
#   LISTEN_HOST/PORT       - де слухати (за nginx: 127.0.0.1)
#   TRUST_PROXY=1          - брати IP клієнта з X-Forwarded-For (лише від localhost)
URL_PREFIX = os.environ.get('URL_PREFIX', '').strip().rstrip('/')
if URL_PREFIX and not URL_PREFIX.startswith('/'):
    URL_PREFIX = '/' + URL_PREFIX
if not re.fullmatch(r'(/[A-Za-z0-9_.-]+)*', URL_PREFIX):
    sys.exit(f"URL_PREFIX містить недопустимі символи: {URL_PREFIX!r}")
LISTEN_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '8080'))
TRUST_PROXY = os.environ.get('TRUST_PROXY', '') == '1'

# Усі шляхи - від теки програми, а не від робочої теки процесу
SCRIPT_PATH = os.path.abspath(__file__)
BASE_DIR = os.path.dirname(SCRIPT_PATH)
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
BACKUPS_KEEP = 5
MAX_UPDATE_SIZE = 5 * 1024 * 1024
PROCESS_STARTED = time.time()   # мітка запуску - за нею видно, що перезапуск відбувся

# TLS з повною перевіркою сертифіката. НЕ замінювати на unverified -
# інакше будь-хто посередині підсуне свій код під виглядом оновлення.
SSL_CONTEXT = ssl.create_default_context()

# GPIO
GPIO_ERROR = None
try:
    import OPi.GPIO as GPIO
    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)
    GPIO_AVAILABLE = True
except ImportError:
    # нормально на ПК без плати
    GPIO_AVAILABLE = False
    GPIO_ERROR = 'модуль OPi.GPIO не встановлено'
except Exception as e:
    # модуль є, але не працює: немає прав, не та плата тощо - це треба бачити
    GPIO_AVAILABLE = False
    GPIO_ERROR = f'{type(e).__name__}: {e}'
    print(f"⚠️ GPIO не ініціалізовано: {GPIO_ERROR}")

# FFmpeg
try:
    subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True, timeout=15)
    FFMPEG_AVAILABLE = True
except (OSError, subprocess.SubprocessError):
    FFMPEG_AVAILABLE = False

app = Flask(__name__)

# ========== SECRET KEY ==========
# Ключ підпису сесій. Генерується один раз при першому запуску
# і зберігається поруч із програмою. Кожна інсталяція - свій ключ.
SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'secret.key')

def load_secret_key():
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, 'r') as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    key = secrets.token_hex(32)
    fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(key)
    print("🔑 Згенеровано новий secret.key")
    return key

app.secret_key = load_secret_key()

class _PrefixMiddleware:
    """/vorot/api/x -> маршрут /api/x. Запити без підтеки теж обслуговуються
    (прямий доступ на LISTEN_PORT для діагностики)."""
    def __init__(self, wsgi_app, prefix):
        self.wsgi_app, self.prefix = wsgi_app, prefix
    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == self.prefix or path.startswith(self.prefix + '/'):
            environ['SCRIPT_NAME'] = self.prefix
            environ['PATH_INFO'] = path[len(self.prefix):] or '/'
        return self.wsgi_app(environ, start_response)

if URL_PREFIX:
    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, URL_PREFIX)
app.jinja_env.globals['prefix'] = URL_PREFIX

def client_ip():
    """IP клієнта. За nginx усі запити йдуть з 127.0.0.1 - тоді беремо
    X-Forwarded-For, але лише від localhost і лише при TRUST_PROXY=1,
    щоб його не можна було підробити, звернувшись напряму."""
    addr = request.remote_addr or '?'
    if TRUST_PROXY and addr in ('127.0.0.1', '::1'):
        fwd = request.headers.get('X-Forwarded-For', '').split(',')[-1].strip()
        if fwd:
            return fwd
    return addr

# Cookie сесії: недоступна для JS і не надсилається з чужих сайтів (захист від CSRF)
app.config.update(
    # Cookie не розрізняють порти: з назвою 'session' вхід у сусідню
    # Flask-програму на тому ж IP викидав би з цієї, і навпаки
    SESSION_COOKIE_NAME='cc_session',
    SESSION_COOKIE_PATH=URL_PREFIX or '/',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

def _resolve_db_path():
    """БД поруч із програмою. Якщо її там нема, а є в робочій теці -
    беремо ту (старі інсталяції), щоб не втратити налаштування."""
    here = os.path.join(BASE_DIR, 'cameras.db')
    cwd = os.path.abspath('cameras.db')
    if not os.path.exists(here) and cwd != here and os.path.exists(cwd):
        print(f"⚠️ БД використовується з робочої теки: {cwd}")
        return cwd
    return here

DB_PATH = _resolve_db_path()
gpio_states = {}
initialized_pins = set()
scheduler_thread = None
scheduler_running = False

# ========== DATABASE ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, is_admin INTEGER DEFAULT 0)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS cameras (
        id INTEGER PRIMARY KEY, name TEXT, host TEXT, port INTEGER DEFAULT 554,
        username TEXT DEFAULT 'admin', password TEXT DEFAULT 'admin',
        brand TEXT DEFAULT 'hikvision', channel INTEGER DEFAULT 1, substream INTEGER DEFAULT 1,
        fps INTEGER DEFAULT 10, gpio_pin INTEGER, gpio_mode TEXT DEFAULT 'pulse',
        gpio_duration REAL DEFAULT 2.0, enabled INTEGER DEFAULT 1)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS buttons (
        id INTEGER PRIMARY KEY, name TEXT, gpio_pin INTEGER,
        gpio_mode TEXT DEFAULT 'pulse', gpio_duration REAL DEFAULT 2.0, enabled INTEGER DEFAULT 1)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_order (
        id INTEGER PRIMARY KEY, user_id INTEGER, item_type TEXT, item_id INTEGER, position INTEGER,
        UNIQUE(user_id, item_type, item_id))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY, view_mode TEXT DEFAULT 'grid', columns INTEGER DEFAULT 2)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY, button_id INTEGER, name TEXT,
        action TEXT DEFAULT 'pulse', time TEXT, days TEXT DEFAULT '1111111',
        enabled INTEGER DEFAULT 1,
        FOREIGN KEY (button_id) REFERENCES buttons(id) ON DELETE CASCADE)''')
    
    c.execute('SELECT COUNT(*) FROM users WHERE is_admin=1')  # noqa
    if c.fetchone()[0] == 0:
        # admin/admin - при першому вході буде примусова зміна пароля
        c.execute('INSERT INTO users (username, password, is_admin) VALUES (?,?,1)',
                  ('admin', hash_password('admin')))
    
    conn.commit()
    conn.close()
    # У БД паролі камер - читати її може лише власник процесу
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ========== UPDATE SYSTEM ==========
_update_lock = threading.Lock()
_restart_lock = threading.Lock()
_restart_pending = False

def parse_version(v):
    """'4.4.10' -> (4, 4, 10). Порівнюємо числа, а не рядки."""
    parts = re.findall(r'\d+', str(v))
    return tuple(int(x) for x in parts) if parts else (0,)

def get_current_version():
    return CURRENT_VERSION

def version_in_source(text):
    m = re.search(r'^CURRENT_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None

def check_for_updates():
    """Check if updates are available"""
    current = get_current_version()
    try:
        req = urllib.request.Request(
            f"{REPO_URL}/version.json",
            headers={'User-Agent': f'Camera-Control/{current}'}
        )
        with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as response:
            data = json.loads(response.read(64 * 1024).decode())
        
        latest = str(data.get('version', 'unknown'))
        main = data.get('files', {}).get('main', {})
        newer = latest != 'unknown' and parse_version(latest) > parse_version(current)
        older = latest != 'unknown' and parse_version(latest) < parse_version(current)
        return {
            'success': True,
            'current': current,
            'latest': latest,
            'available': newer,
            'server_older': older,
            'changelog': data.get('changelog', []),
            'date': data.get('date', ''),
            'url': main.get('url', ''),
            'sha256': str(main.get('sha256', '')).strip().lower()
        }
    except urllib.error.HTTPError as e:
        return {'success': False, 'current': current,
                'error': f'HTTP Error {e.code}: {e.reason}. Check if files are uploaded to {REPO_URL}'}
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            msg = f'SSL certificate error: {e.reason}. Update refused for security.'
        else:
            msg = f'Connection failed: {e.reason}. Check internet connection and URL'
        return {'success': False, 'current': current, 'error': msg}
    except json.JSONDecodeError:
        return {'success': False, 'current': current, 'error': 'Invalid JSON in version.json file'}
    except Exception as e:
        return {'success': False, 'current': current, 'error': f'Unexpected error: {str(e)}'}

def download_update():
    """Завантажує і перевіряє файл оновлення. Повертає (True, (шлях, версія)) або (False, помилка)."""
    try:
        info = check_for_updates()
        if not info.get('success'):
            return False, f"Cannot check updates: {info.get('error', 'Unknown error')}"
        if not info.get('available'):
            return False, "No update available"
        
        url = info.get('url')
        if not url:
            return False, "Update URL not found in version.json"
        if urllib.parse.urlparse(url).scheme != 'https':
            return False, "Update URL must use https://"
        
        expected_hash = info.get('sha256', '')
        if len(expected_hash) != 64:
            return False, "version.json has no sha256 for main file - update refused"
        
        print(f"📥 Downloading from: {url}")
        req = urllib.request.Request(url, headers={'User-Agent': f'Camera-Control/{CURRENT_VERSION}'})
        with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as response:
            payload = response.read(MAX_UPDATE_SIZE + 1)
        if len(payload) > MAX_UPDATE_SIZE:
            return False, "Update file is too large"
        
        # 1. Контрольна сума - файл саме той, що опубліковано
        actual_hash = hashlib.sha256(payload).hexdigest()
        if actual_hash != expected_hash:
            return False, f"SHA-256 mismatch: expected {expected_hash[:12]}..., got {actual_hash[:12]}..."
        print("✓ SHA-256 verified")
        
        # 2. Це Python-скрипт у UTF-8
        if not payload.startswith(b'#!'):
            return False, "Downloaded file is not a valid Python script"
        try:
            text = payload.decode('utf-8')
        except UnicodeDecodeError:
            return False, "Update file is not valid UTF-8"
        
        # 3. Версія всередині файлу збігається з version.json
        #    (інакше - вічний цикл "є оновлення" після встановлення)
        file_version = version_in_source(text)
        if file_version != info['latest']:
            return False, (f"Версія у файлі ({file_version}) не збігається з "
                           f"version.json ({info['latest']}) - перевірте опубліковані файли")
        
        # 4. Синтаксис
        try:
            compile(text, 'camera_control_update', 'exec')
        except SyntaxError as e:
            return False, f"Python syntax error: line {e.lineno}: {e.msg}"
        
        temp_file = SCRIPT_PATH + '.new'
        with open(temp_file, 'wb') as f:
            f.write(payload)
        print(f"✓ Download completed: {len(payload)} bytes")
        return True, (temp_file, info['latest'])
        
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: Cannot download update file"
    except urllib.error.URLError as e:
        return False, f"Connection error: {e.reason}"
    except Exception as e:
        return False, f"Download failed: {str(e)}"

def preflight_check(path):
    """Пробний запуск нової версії в окремому процесі - без старту сервера.
    Ловить відсутні модулі, помилки при імпорті, NameError на верхньому рівні."""
    code = "import runpy, sys; runpy.run_path(sys.argv[1], run_name='__preflight__')"
    env = dict(os.environ, CAMERA_CONTROL_PREFLIGHT='1')
    try:
        r = subprocess.run([sys.executable, '-c', code, path], cwd=BASE_DIR, env=env,
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "перевірка запуску перевищила 120 с"
    if r.returncode != 0:
        lines = [l for l in (r.stderr or r.stdout or '').strip().splitlines() if l.strip()]
        return False, (lines[-1] if lines else f"код виходу {r.returncode}")[:300]
    return True, ''

def list_backups():
    """Резервні копії, найновіші першими: [(шлях, версія)]."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = []
    for name in os.listdir(BACKUP_DIR):
        if not name.endswith('.py'):
            continue
        path = os.path.join(BACKUP_DIR, name)
        m = re.match(r'camera_control-(.+)-\d{8}-\d{6}\.py$', name)
        if m:
            ver = m.group(1)
        else:
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    ver = version_in_source(f.read()) or '?'
            except OSError:
                continue
        items.append((os.path.getmtime(path), path, ver))
    items.sort(reverse=True)
    return [(p, v) for _, p, v in items]

def prune_backups():
    for path, _ in list_backups()[BACKUPS_KEEP:]:
        try:
            os.remove(path)
        except OSError:
            pass

def _backup_current():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = f"camera_control-{CURRENT_VERSION}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.py"
    path = os.path.join(BACKUP_DIR, name)
    shutil.copy2(SCRIPT_PATH, path)
    return path

def _check_writable():
    if not os.access(BASE_DIR, os.W_OK) or not os.access(SCRIPT_PATH, os.W_OK):
        return (f"Немає прав на запис у {BASE_DIR}. Сервіс запущено від користувача, "
                f"якому не належать файли програми")
    return None

def apply_update(temp_file, new_version):
    """Атомарно підміняє файл програми. Попередня версія - у backups/."""
    try:
        backup_file = _backup_current()
        shutil.copymode(SCRIPT_PATH, temp_file)
        os.replace(temp_file, SCRIPT_PATH)      # атомарно: або старий файл, або новий
        prune_backups()
        # застарілий файл версії від попередніх релізів
        legacy = os.path.join(BASE_DIR, 'version.txt')
        if os.path.exists(legacy):
            os.remove(legacy)
        print(f"✓ Оновлено {CURRENT_VERSION} -> {new_version}, копія: {backup_file}")
        return True, f"Встановлено версію {new_version}"
    except Exception as e:
        try:
            os.remove(temp_file)
        except OSError:
            pass
        return False, f"Не вдалося замінити файл: {e}"

def install_update():
    """Повний цикл: завантаження -> перевірки -> пробний запуск -> заміна -> перезапуск."""
    if not _update_lock.acquire(blocking=False):
        return False, "Оновлення вже виконується", None
    try:
        err = _check_writable()
        if err:
            return False, err, None
        ok, res = download_update()
        if not ok:
            return False, res, None
        temp_file, new_version = res
        ok, err = preflight_check(temp_file)
        if not ok:
            os.remove(temp_file)
            return False, f"Нова версія не запускається: {err}", None
        print("✓ Preflight passed")
        ok, msg = apply_update(temp_file, new_version)
        if not ok:
            return False, msg, None
        schedule_restart()
        return True, msg, new_version
    finally:
        _update_lock.release()

def rollback_update():
    """Повертає найсвіжішу резервну копію. Поточна версія теж зберігається."""
    if not _update_lock.acquire(blocking=False):
        return False, "Оновлення вже виконується", None
    try:
        err = _check_writable()
        if err:
            return False, err, None
        backups = list_backups()
        if not backups:
            return False, "Немає резервних копій", None
        src, ver = backups[0]
        with open(src, 'r', encoding='utf-8') as f:
            text = f.read()
        try:
            compile(text, src, 'exec')
        except SyntaxError as e:
            return False, f"Резервна копія пошкоджена: {e.msg}", None
        ok, err = preflight_check(src)
        if not ok:
            return False, f"Резервна копія не запускається: {err}", None
        temp_file = SCRIPT_PATH + '.new'
        shutil.copyfile(src, temp_file)
        _backup_current()
        shutil.copymode(SCRIPT_PATH, temp_file)
        os.replace(temp_file, SCRIPT_PATH)
        os.remove(src)          # відновлена копія тепер робочий файл
        prune_backups()
        print(f"↩ Відкат {CURRENT_VERSION} -> {ver}")
        schedule_restart()
        return True, f"Відновлено версію {ver}", ver
    except Exception as e:
        return False, f"Помилка відкату: {e}", None
    finally:
        _update_lock.release()

def _mark_fds_cloexec():
    """Усі дескриптори, крім stdin/stdout/stderr, закриються при exec."""
    try:
        fds = [int(x) for x in os.listdir('/proc/self/fd')]
    except OSError:
        fds = range(3, 1024)
    for fd in fds:
        if fd > 2:
            try:
                os.set_inheritable(fd, False)
            except OSError:
                pass

def schedule_restart(delay=1.5):
    """Перезапуск самого процесу (os.execv): не залежить від назви systemd-сервісу
    і не потребує прав на systemctl. PID лишається тим самим."""
    global _restart_pending
    with _restart_lock:
        if _restart_pending:
            return
        _restart_pending = True
    
    def _do_restart():
        time.sleep(delay)                        # дати відповіді HTTP дійти до браузера
        deadline = time.time() + 20
        while active_pulse_pins() and time.time() < deadline:
            time.sleep(0.2)                      # дочекатися кінця імпульсу воріт
        release_pulse_pins()                     # і гарантовано відпустити реле
        reset_streams()                          # ffmpeg не лишаються сиротами
        print("🔄 Перезапуск процесу...", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        # werkzeug робить слухаючий сокет успадковуваним - без цього новий
        # процес отримав би його в спадок і не зміг би зайняти порт
        _mark_fds_cloexec()
        argv = list(getattr(sys, 'orig_argv', None) or [sys.executable, SCRIPT_PATH] + sys.argv[1:])
        os.execv(sys.executable, argv)
    
    threading.Thread(target=_do_restart, daemon=True).start()

# ========== PASSWORDS ==========
# pbkdf2 із сіллю. 60 000 ітерацій ~ 0.5 с на NanoPi NEO - відчутно для перебору,
# непомітно для людини. Кількість ітерацій зберігається в самому хеші,
# тож її можна збільшити пізніше без втрати старих паролів.
PASSWORD_HASH_METHOD = 'pbkdf2:sha256:60000'
PASSWORD_MIN_LENGTH = 8
WEAK_PASSWORDS = {
    'admin', 'admin123', 'administrator', 'password', 'password1', 'qwerty123',
    '12345678', '123456789', '1234567890', '11111111', '00000000', '87654321',
    'camera', 'camera123', 'user1234', 'changeme',
}

def hash_password(password):
    return generate_password_hash(password, method=PASSWORD_HASH_METHOD)

def is_legacy_hash(stored):
    """Старий формат: голий SHA-256 без солі (64 hex-символи)."""
    return (isinstance(stored, str) and len(stored) == 64
            and all(c in '0123456789abcdef' for c in stored))

def verify_password(stored, password):
    if not stored:
        return False
    if is_legacy_hash(stored):
        return hmac.compare_digest(stored, hashlib.sha256(password.encode()).hexdigest())
    try:
        return check_password_hash(stored, password)
    except Exception:
        return False

def password_problem(password, username=''):
    """Повертає текст проблеми або None, якщо пароль прийнятний."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f'Пароль має містити щонайменше {PASSWORD_MIN_LENGTH} символів'
    if password.lower() == (username or '').lower():
        return 'Пароль не може збігатися з логіном'
    if password.lower() in WEAK_PASSWORDS:
        return 'Надто простий пароль'
    return None

def pw_stamp(stored):
    """Відбиток хешу пароля. Зміна пароля робить старі сесії недійсними."""
    return hashlib.sha256(stored.encode()).hexdigest()[:16]

# Хеш-заглушка: для неіснуючого логіну час відповіді той самий,
# тож перебором не з'ясувати, які логіни існують
_DUMMY_HASH = hash_password(secrets.token_hex(16))

# ========== LOGIN RATE LIMIT ==========
LOGIN_MAX_FAILS = 5          # спроб
LOGIN_WINDOW = 15 * 60       # за 15 хвилин
LOGIN_LOCK = 5 * 60          # блокування на 5 хвилин
_login_fails = {}            # ip -> [кількість, час_першої, заблоковано_до]
_login_fails_lock = threading.Lock()

def login_locked(ip):
    """Скільки секунд лишилось блокування (0 - не заблоковано)."""
    now = time.time()
    with _login_fails_lock:
        e = _login_fails.get(ip)
        if e and e[2] > now:
            return int(e[2] - now)
    return 0

def login_failed(ip):
    now = time.time()
    with _login_fails_lock:
        for k in [k for k, v in _login_fails.items()
                  if v[2] < now and now - v[1] > LOGIN_WINDOW]:
            del _login_fails[k]
        e = _login_fails.get(ip)
        if not e or now - e[1] > LOGIN_WINDOW:
            e = [0, now, 0]
        e[0] += 1
        if e[0] >= LOGIN_MAX_FAILS:
            e[2] = now + LOGIN_LOCK
            e[0], e[1] = 0, now
            print(f"🚫 Вхід заблоковано на {LOGIN_LOCK // 60} хв для {ip}")
        _login_fails[ip] = e

def login_succeeded(ip):
    with _login_fails_lock:
        _login_fails.pop(ip, None)

# ========== CSRF ==========
# Кожна сесія має свій токен. Усі запити, що щось змінюють (POST/PUT/DELETE),
# мусять його передати - заголовком X-CSRF-Token або полем форми csrf_token.
# Чужий сайт токена не знає, тож не зможе відкрити ворота від імені користувача.
CSRF_EXEMPT = {'/login'}

def get_csrf_token():
    tok = session.get('csrf')
    if not tok:
        tok = secrets.token_urlsafe(32)
        session['csrf'] = tok
    return tok

app.jinja_env.globals['csrf_token'] = get_csrf_token

@app.before_request
def csrf_protect():
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    if request.path in CSRF_EXEMPT:
        return None
    sent = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token') or ''
    expected = session.get('csrf') or ''
    if not expected or not hmac.compare_digest(sent, expected):
        print(f"🛡️ CSRF відхилено: {request.method} {request.path} з {client_ip()}")
        if request.path.startswith('/api/'):
            return jsonify({'success': False,
                            'message': 'Сесія застаріла - оновіть сторінку'}), 403
        return 'Сесія застаріла - оновіть сторінку', 403
    return None

@app.after_request
def _init_new_pins(resp):
    if (request.method in ('POST', 'PUT') and resp.status_code == 200
            and request.path.startswith(('/api/buttons', '/api/cameras', '/api/restore'))):
        try:
            init_all_gpio()
        except Exception as e:
            print(f"⚠️ Ініціалізація GPIO після зміни налаштувань: {type(e).__name__}: {e}")
    return resp

# ========== AUTH ==========
def current_user():
    """Перевіряє сесію проти БД. Видалений користувач або змінений пароль -
    сесія анулюється одразу, а не через 30 днів."""
    if not session.get('logged_in'):
        return None
    conn = get_db()
    user = conn.execute('SELECT id, username, password, is_admin FROM users WHERE id=?',
                        (session.get('user_id'),)).fetchone()
    conn.close()
    if not user or not hmac.compare_digest(pw_stamp(user['password']), session.get('pw_stamp', '')):
        session.clear()
        return None
    # права беруться з БД - зняття адмінки діє одразу
    if session.get('is_admin') != bool(user['is_admin']):
        session['is_admin'] = bool(user['is_admin'])
    if session.get('username') != user['username']:
        session['username'] = user['username']
    return user

def _deny(message, status, redirect_to):
    if request.path.startswith(('/api/', '/snapshot/', '/stream/')):
        return jsonify({'success': False, 'message': message}), status
    return redirect(URL_PREFIX + redirect_to)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return _deny('Not logged in', 401, '/login')
        if session.get('must_change'):
            return _deny('Password change required', 403, '/change-password')
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user:
            return _deny('Not logged in', 401, '/login')
        if session.get('must_change'):
            return _deny('Password change required', 403, '/change-password')
        if not user['is_admin']:
            return jsonify({'success': False, 'message': 'Access denied'}), 403
        return f(*args, **kwargs)
    return decorated

# ========== GPIO ==========
def init_gpio(pin):
    if not GPIO_AVAILABLE or pin in initialized_pins:
        return GPIO_AVAILABLE
    try:
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.HIGH)
        initialized_pins.add(pin)
        gpio_states[pin] = False
        return True
    except Exception as e:
        print(f"❌ GPIO пін {pin}: не вдалося налаштувати - {type(e).__name__}: {e}")
        return False

def configured_pins():
    """Усі піни, вказані в кнопках і камерах."""
    conn = get_db()
    rows = conn.execute('SELECT gpio_pin FROM buttons WHERE gpio_pin IS NOT NULL '
                        'UNION SELECT gpio_pin FROM cameras WHERE gpio_pin IS NOT NULL').fetchall()
    conn.close()
    pins = set()
    for (pin,) in rows:
        try:
            pins.add(int(pin))
        except (TypeError, ValueError):
            pass
    return sorted(pins)

def init_all_gpio():
    """Одразу виставити всі налаштовані реле у "вимкнено" (HIGH).
    Без цього після перезавантаження пін лишається у невизначеному стані
    до першого натискання - реле з керуванням низьким рівнем може спрацювати.
    Уже налаштовані піни не чіпаються: увімкнений перемикач чи імпульс тривають."""
    if not GPIO_AVAILABLE:
        return []
    done = []
    for pin in configured_pins():
        if init_gpio(pin):
            done.append(pin)
    return done

_active_pulses = {}              # pin -> кількість імпульсів, що зараз тривають
_active_pulses_lock = threading.Lock()

def active_pulse_pins():
    with _active_pulses_lock:
        return list(_active_pulses)

def release_pulse_pins():
    """Відпустити реле всіх незавершених імпульсів (перед перезапуском/зупинкою).
    Інакше процес, убитий посеред імпульсу, лишає реле увімкненим назавжди."""
    if not GPIO_AVAILABLE:
        return
    for pin in active_pulse_pins():
        try:
            GPIO.output(pin, GPIO.HIGH)
            print(f"⚡ Реле на піні {pin} примусово вимкнено")
        except Exception:
            pass

def gpio_pulse(pin, duration):
    if not init_gpio(pin):
        return False
    with _active_pulses_lock:
        _active_pulses[pin] = _active_pulses.get(pin, 0) + 1
    try:
        GPIO.output(pin, GPIO.LOW)
        time.sleep(duration)
        GPIO.output(pin, GPIO.HIGH)
        return True
    except Exception as e:
        print(f"❌ GPIO пін {pin}: збій імпульсу - {type(e).__name__}: {e}")
        # збій міг статися після увімкнення - реле не має лишитись увімкненим
        try:
            GPIO.output(pin, GPIO.HIGH)
        except Exception as e2:
            print(f"🚨 GPIO пін {pin}: НЕ ВДАЛОСЯ ВИМКНУТИ РЕЛЕ - {type(e2).__name__}: {e2}")
        return False
    finally:
        with _active_pulses_lock:
            _active_pulses[pin] -= 1
            if _active_pulses[pin] <= 0:
                del _active_pulses[pin]

def gpio_toggle(pin):
    if not init_gpio(pin):
        return False, False
    old_state = gpio_states.get(pin, False)
    new_state = not old_state
    try:
        GPIO.output(pin, GPIO.LOW if new_state else GPIO.HIGH)
    except Exception as e:
        print(f"❌ GPIO пін {pin}: збій перемикання - {type(e).__name__}: {e}")
        return False, old_state
    gpio_states[pin] = new_state
    return True, new_state

# ========== SCHEDULER ==========
_schedule_last_run = {}    # schedule_id -> 'YYYY-MM-DD HH:MM' останнього виконання

def scheduler_worker():
    """Background thread that checks and executes schedules"""
    global scheduler_running
    print("📅 Scheduler started")
    
    while scheduler_running:
        try:
            now = datetime.now()
            current_time = now.strftime('%H:%M')
            current_day = now.weekday()  # 0=Monday, 6=Sunday
            
            conn = get_db()
            schedules = conn.execute('''
                SELECT s.*, b.gpio_pin, b.gpio_duration 
                FROM schedules s 
                JOIN buttons b ON s.button_id = b.id 
                WHERE s.enabled = 1 AND b.enabled = 1
            ''').fetchall()
            conn.close()
            
            for schedule in schedules:
                # Check if schedule should run now
                if schedule['time'] == current_time:
                    # Перевірка йде кожні 5 с, а хвилина триває 60 -
                    # без цього розклад спрацював би ~12 разів поспіль
                    run_key = now.strftime('%Y-%m-%d %H:%M')
                    if _schedule_last_run.get(schedule['id']) == run_key:
                        continue
                    # Check if today is enabled (days format: "1111111" for Mon-Sun)
                    days = schedule['days'] if schedule['days'] else '1111111'
                    if len(days) > current_day and days[current_day] == '1':
                        _schedule_last_run[schedule['id']] = run_key
                        # Execute action
                        action = schedule['action']
                        pin = schedule['gpio_pin']
                        duration = schedule['gpio_duration'] if schedule['gpio_duration'] else 2.0
                        
                        print(f"📅 Executing schedule: {schedule['name']} - {action} on pin {pin}")
                        
                        if action == 'pulse':
                            threading.Thread(target=gpio_pulse, args=(pin, duration), daemon=True).start()
                        elif action == 'on':
                            if init_gpio(pin):
                                GPIO.output(pin, GPIO.LOW)
                                gpio_states[pin] = True
                        elif action == 'off':
                            if init_gpio(pin):
                                GPIO.output(pin, GPIO.HIGH)
                                gpio_states[pin] = False
            
        except Exception as e:
            print(f"❌ Scheduler error: {e}")
        
        # Check every 5 seconds
        time.sleep(5)
    
    print("📅 Scheduler stopped")

def start_scheduler():
    """Start the scheduler background thread"""
    global scheduler_thread, scheduler_running
    
    if scheduler_thread and scheduler_thread.is_alive():
        return
    
    scheduler_running = True
    scheduler_thread = threading.Thread(target=scheduler_worker, daemon=True)
    scheduler_thread.start()

def stop_scheduler():
    """Stop the scheduler background thread"""
    global scheduler_running
    scheduler_running = False

# ========== USER ORDER & SETTINGS ==========
def get_user_items(user_id):
    conn = get_db()
    
    cameras = conn.execute('SELECT * FROM cameras WHERE enabled=1').fetchall()
    cameras = [dict(c) for c in cameras]
    for c in cameras:
        c['type'] = 'camera'
    
    buttons = conn.execute('SELECT * FROM buttons WHERE enabled=1').fetchall()
    buttons = [dict(b) for b in buttons]
    for b in buttons:
        b['type'] = 'button'
    
    orders = conn.execute('SELECT item_type, item_id, position FROM user_order WHERE user_id=?', (user_id,)).fetchall()
    order_map = {}
    for o in orders:
        order_map[(o['item_type'], o['item_id'])] = o['position']
    
    conn.close()
    
    all_items = cameras + buttons
    
    def get_position(item):
        key = (item['type'], item['id'])
        if key in order_map:
            return order_map[key]
        if item['type'] == 'camera':
            return 1000 + item['id']
        return 2000 + item['id']
    
    all_items.sort(key=get_position)
    return all_items

def get_user_settings(user_id):
    conn = get_db()
    s = conn.execute('SELECT * FROM user_settings WHERE user_id=?', (user_id,)).fetchone()
    conn.close()
    if s:
        return {'view_mode': s['view_mode'], 'columns': s['columns']}
    return {'view_mode': 'grid', 'columns': 2}

def save_user_settings(user_id, view_mode, columns):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO user_settings (user_id, view_mode, columns) VALUES (?,?,?)',
                (user_id, view_mode, columns))
    conn.commit()
    conn.close()

def save_user_order(user_id, items_order):
    conn = get_db()
    conn.execute('DELETE FROM user_order WHERE user_id=?', (user_id,))
    for pos, item in enumerate(items_order):
        conn.execute('INSERT INTO user_order (user_id, item_type, item_id, position) VALUES (?,?,?,?)',
                    (user_id, item['type'], item['id'], pos))
    conn.commit()
    conn.close()

# ========== RTSP STREAM ==========
def get_rtsp_url(cam):
    brand = cam.get('brand', 'hikvision')
    ch = cam.get('channel', 1)
    sub = cam.get('substream', 1)
    
    if brand == 'dahua':
        stream = 1 if sub else 0
        path = "/cam/realmonitor?channel=" + str(ch) + "&subtype=" + str(stream)
    else:
        stream_num = str(ch) + "0" + ("2" if sub else "1")
        path = "/Streaming/Channels/" + stream_num
    
    return "rtsp://" + cam['username'] + ":" + cam['password'] + "@" + cam['host'] + ":" + str(cam['port']) + path

# Один ffmpeg на камеру, скільки б людей не дивилось.
# Запускається з першим глядачем, зупиняється, коли всі пішли.
STREAM_IDLE_TIMEOUT = 20     # с без жодного запиту -> ffmpeg зупиняється
STREAM_STALL_TIMEOUT = 15    # с без кадрів -> ffmpeg перезапускається
STREAM_MAX_FPS = 25
SNAPSHOT_WAIT = 8            # с - скільки запит кадру чекає новий кадр
POLL_MAX_FPS = 5             # стеля для режиму окремих кадрів (iPhone)
MAX_JPEG_BUFFER = 4 * 1024 * 1024

def _load_camera(cam_id):
    conn = get_db()
    cam = conn.execute('SELECT * FROM cameras WHERE id=? AND enabled=1', (cam_id,)).fetchone()
    conn.close()
    return dict(cam) if cam else None

def build_ffmpeg_cmd(cam):
    fps = max(1, min(int(cam.get('fps') or 10), STREAM_MAX_FPS))
    return ['ffmpeg', '-nostdin', '-loglevel', 'error',
            '-rtsp_transport', 'tcp', '-i', get_rtsp_url(cam),
            '-an', '-f', 'mjpeg', '-q:v', '5', '-r', str(fps), '-']

def _mask_secrets(text, cam):
    """ffmpeg пише URL з логіном і паролем у повідомленнях про помилки."""
    text = re.sub(r'(rtsp://)[^@/\s]+@', r'\1***@', text)
    if cam.get('password'):
        text = text.replace(cam['password'], '***')
    return text

class CameraStream:
    def __init__(self, cam_id):
        self.cam_id = cam_id
        self.cond = threading.Condition()
        self.frame = None          # останній JPEG
        self.frame_time = 0.0
        self.seq = 0               # номер кадру - клієнт просить "новіший за N"
        self.last_request = 0.0
        self.error = None
        self.proc = None
        self.thread = None         # None = супервізор не працює
        self.stopped = False
        self.waiters = 0           # запити, що зараз чекають кадр - теж глядачі
        self.failures = 0          # невдалі запуски поспіль (для паузи між спробами)

    def _idle(self, now=None):
        """Нікого нема: ні запитів, що чекають, ні свіжих звернень."""
        now = now or time.time()
        return self.waiters == 0 and now - self.last_request > STREAM_IDLE_TIMEOUT

    # ----- для маршрутів -----
    def touch(self):
        with self.cond:
            self.last_request = time.time()
            if self.stopped or self.thread is not None:
                return
            self.thread = threading.Thread(target=self._supervise, daemon=True)
            self.thread.start()

    def wait_frame(self, after_seq=0, timeout=8.0):
        """Чекає кадр новіший за after_seq. Повертає (jpeg, seq, вік, помилка)."""
        self.touch()
        deadline = time.time() + timeout
        with self.cond:
            self.waiters += 1
            try:
                while not (self.frame is not None and self.seq > after_seq) and not self.stopped:
                    rest = deadline - time.time()
                    if rest <= 0:
                        break
                    self.cond.wait(rest)
            finally:
                self.waiters -= 1
                self.last_request = time.time()
            age = time.time() - self.frame_time if self.frame is not None else None
            return self.frame, self.seq, age, self.error

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()
        self._kill()

    # ----- всередині -----
    def _kill(self):
        """Вбиває всю групу процесів: якщо ffmpeg запущено через обгортку,
        дочірні процеси інакше тримали б трубу і читання зависло б."""
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass

    def _set_error(self, msg):
        with self.cond:
            self.error = msg
            self.cond.notify_all()

    def _supervise(self):
        try:
            while True:
                with self.cond:
                    if self.stopped or self._idle():
                        self.frame = None           # застарілий кадр не показуємо як живий
                        self.thread = None
                        return
                cam = _load_camera(self.cam_id)
                if not cam:
                    self._set_error('Камеру не знайдено або вимкнено')
                    with self.cond:
                        self.thread = None
                    return
                if not FFMPEG_AVAILABLE:
                    self._set_error('ffmpeg не встановлено')
                    with self.cond:
                        self.thread = None
                    return
                
                started = time.time()
                self._run_ffmpeg(cam, started)
                
                self.failures = 0 if self.frame_time > started else self.failures + 1
                pause = min(30, 2 * self.failures)  # 0, 2, 4 ... 30 с між спробами
                if pause:
                    end = time.time() + pause
                    with self.cond:
                        while not self.stopped and time.time() < end:
                            self.cond.wait(end - time.time())
        except Exception as e:
            print(f"❌ Stream {self.cam_id}: {e}")
            with self.cond:
                self.thread = None
        finally:
            self._kill()

    def _run_ffmpeg(self, cam, started):
        try:
            proc = subprocess.Popen(build_ffmpeg_cmd(cam), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                    start_new_session=True)
        except Exception as e:
            self._set_error(f'Не вдалося запустити ffmpeg: {e}')
            return
        self.proc = proc
        
        def read_stderr():
            for raw in iter(proc.stderr.readline, b''):
                line = raw.decode('utf-8', 'replace').strip()
                if line:
                    self._set_error(_mask_secrets(line, cam)[:200])
        
        def watchdog():
            while proc.poll() is None:
                time.sleep(1)
                now = time.time()
                with self.cond:
                    idle = self._idle(now)
                    stalled = now - max(self.frame_time, started) > STREAM_STALL_TIMEOUT
                    stop = self.stopped
                if stalled and not idle and not stop:
                    self._set_error('Камера не передає кадри')
                if idle or stalled or stop:
                    self._kill()
                    return
        
        threading.Thread(target=read_stderr, daemon=True).start()
        threading.Thread(target=watchdog, daemon=True).start()
        
        buf = bytearray()
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b'\xff\xd8')
                if start < 0:
                    del buf[:-1]                     # 0xFF може бути початком маркера
                    break
                end = buf.find(b'\xff\xd9', start + 2)
                if end < 0:
                    if start:
                        del buf[:start]
                    if len(buf) > MAX_JPEG_BUFFER:   # щось пішло не так - не їмо пам'ять
                        buf.clear()
                    break
                jpg = bytes(buf[start:end + 2])
                del buf[:end + 2]
                if len(jpg) > 1000:
                    with self.cond:
                        self.frame = jpg
                        self.frame_time = time.time()
                        self.seq += 1
                        self.error = None
                        self.cond.notify_all()
        self._kill()


_streams = {}
_streams_lock = threading.Lock()

def get_stream(cam_id):
    with _streams_lock:
        s = _streams.get(cam_id)
        if s is None or s.stopped:
            if not _load_camera(cam_id):
                return None
            s = CameraStream(cam_id)
            _streams[cam_id] = s
        return s

def reset_streams(cam_id=None):
    """Після зміни налаштувань камери - наступний запит відкриє потік заново."""
    with _streams_lock:
        ids = [cam_id] if cam_id is not None else list(_streams)
        victims = [_streams.pop(i) for i in ids if i in _streams]
    for v in victims:
        v.stop()

def mjpeg_generator(stream):
    seq = 0
    while not stream.stopped:
        frame, new_seq, age, err = stream.wait_frame(after_seq=seq, timeout=10)
        if frame is None or new_seq == seq:
            continue
        seq = new_seq
        yield (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
               + str(len(frame)).encode() + b'\r\n\r\n' + frame + b'\r\n')

# ========== NETWORK SCAN ==========
def scan_network():
    results = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
    except OSError:
        local_ip = "192.168.1.1"
    
    subnet = '.'.join(local_ip.split('.')[:-1])
    
    def check_port(ip, port, timeout=0.5):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                return sock.connect_ex((ip, port)) == 0
        except OSError:
            return False
    
    threads = []
    results_lock = threading.Lock()
    
    def worker(ip):
        ports = []
        for port in [554, 80, 8080]:
            if check_port(ip, port):
                ports.append(port)
        if ports:
            with results_lock:
                brand = 'hikvision' if 554 in ports else 'unknown'
                results.append({'ip': ip, 'ports': ports, 'brand': brand})
    
    for i in range(1, 255):
        ip = subnet + "." + str(i)
        t = threading.Thread(target=worker, args=(ip,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join(timeout=0.1)
    
    return sorted(results, key=lambda x: int(x['ip'].split('.')[-1]))

# ========== BACKUP ==========
def create_backup():
    conn = get_db()
    backup = {
        'version': '4.2',
        'date': datetime.now().isoformat(),
        'users': [dict(u) for u in conn.execute('SELECT * FROM users').fetchall()],
        'cameras': [dict(c) for c in conn.execute('SELECT * FROM cameras').fetchall()],
        'buttons': [dict(b) for b in conn.execute('SELECT * FROM buttons').fetchall()],
        'user_order': [dict(o) for o in conn.execute('SELECT * FROM user_order').fetchall()],
        'user_settings': [dict(s) for s in conn.execute('SELECT * FROM user_settings').fetchall()]
    }
    conn.close()
    return backup

def restore_backup(backup_data):
    conn = get_db()
    try:
        conn.execute('DELETE FROM user_settings')
        conn.execute('DELETE FROM user_order')
        conn.execute('DELETE FROM buttons')
        conn.execute('DELETE FROM cameras')
        conn.execute('DELETE FROM users')
        
        for u in backup_data.get('users', []):
            conn.execute('INSERT INTO users VALUES (?,?,?,?)',
                        (u['id'], u['username'], u['password'], u['is_admin']))
        
        for c in backup_data.get('cameras', []):
            conn.execute('INSERT INTO cameras VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (c['id'], c['name'], c['host'], c['port'], c['username'], c['password'],
                         c['brand'], c['channel'], c['substream'], c['fps'],
                         c.get('gpio_pin'), c.get('gpio_mode','pulse'), c.get('gpio_duration',2), c.get('enabled',1)))
        
        for b in backup_data.get('buttons', []):
            conn.execute('INSERT INTO buttons VALUES (?,?,?,?,?,?)',
                        (b['id'], b['name'], b['gpio_pin'], b.get('gpio_mode','pulse'), b.get('gpio_duration',2), b.get('enabled',1)))
        
        for o in backup_data.get('user_order', []):
            conn.execute('INSERT INTO user_order VALUES (?,?,?,?,?)',
                        (o['id'], o['user_id'], o['item_type'], o['item_id'], o['position']))
        
        for s in backup_data.get('user_settings', []):
            conn.execute('INSERT INTO user_settings VALUES (?,?,?)',
                        (s['user_id'], s.get('view_mode','grid'), s.get('columns',2)))
        
        conn.commit()
        return True, "OK"
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        conn.close()

# ========== HTML ==========
HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="csrf-token" content="{{ csrf_token() }}">
    <title>Camera Control</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:system-ui,-apple-system,sans-serif;background:#111;color:#eee;min-height:100vh}
        
        .header{background:#1a1a1a;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #333;flex-wrap:wrap;gap:10px}
        .logo{font-size:18px;font-weight:600;color:#0f0}
        .nav{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
        .nav a,.nav button,.nav select{background:#222;border:1px solid #333;color:#888;text-decoration:none;font-size:13px;cursor:pointer;padding:6px 12px;border-radius:4px}
        .nav a:hover,.nav button:hover{color:#fff;background:#333}
        .nav select{color:#fff}
        .nav .user{color:#0f0;border:1px solid #0f0;font-size:12px;background:none}
        .nav .edit-mode{background:#f90;color:#000;border-color:#f90}
        
        .content{padding:20px;max-width:1600px;margin:0 auto}
        
        .view-controls{display:flex;gap:10px;margin-bottom:15px;align-items:center;flex-wrap:wrap}
        .view-controls label{color:#666;font-size:12px}
        .view-controls select{background:#222;border:1px solid #333;color:#fff;padding:6px 10px;border-radius:4px;font-size:13px}
        
        .settings{display:none;background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:20px;margin-bottom:20px}
        .settings.open{display:block}
        .settings h3{color:#0f0;margin-bottom:15px;font-size:14px;text-transform:uppercase}
        
        .tabs{display:flex;gap:5px;margin-bottom:15px;flex-wrap:wrap}
        .tab{background:#222;border:none;color:#888;padding:8px 16px;border-radius:4px;cursor:pointer;font-size:13px}
        .tab:hover{color:#fff}
        .tab.active{background:#0f0;color:#000}
        
        .panel{display:none}
        .panel.active{display:block}
        
        .form-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:10px}
        .form-group{position:relative}
        .form-group label{display:block;color:#666;font-size:11px;margin-bottom:4px;text-transform:uppercase}
        .form-group input,.form-group select{width:100%;padding:8px 10px;background:#222;border:1px solid #333;border-radius:4px;color:#fff;font-size:13px}
        .form-group input:focus,.form-group select:focus{outline:none;border-color:#0f0}
        .form-group.hidden{display:none}
        
        .btn{padding:8px 16px;border:none;border-radius:4px;font-size:13px;cursor:pointer;font-weight:500}
        .btn-green{background:#0f0;color:#000}
        .btn-red{background:#f44;color:#fff}
        .btn-gray{background:#333;color:#fff}
        .btn-blue{background:#08f;color:#fff}
        .btn:hover{opacity:0.9}
        
        table{width:100%;border-collapse:collapse;font-size:13px}
        th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #222}
        th{color:#666;font-size:11px;text-transform:uppercase}
        tr:hover td{background:#1a1a1a}
        
        .badge{font-size:10px;padding:2px 8px;border-radius:10px;text-transform:uppercase}
        .badge-hik{background:#1a3a1a;color:#0f0}
        .badge-dah{background:#1a1a3a;color:#66f}
        
        .items{display:grid;gap:15px}
        .items.grid-1{grid-template-columns:1fr}
        .items.grid-2{grid-template-columns:repeat(2,1fr)}
        .items.grid-3{grid-template-columns:repeat(3,1fr)}
        .items.grid-4{grid-template-columns:repeat(4,1fr)}
        .items.list{grid-template-columns:1fr}
        .items.list .item{display:flex;align-items:stretch}
        .items.list .item .cam-view{width:200px;flex-shrink:0}
        .items.list .item-content{flex:1;display:flex;flex-direction:column}
        .items.list .item-head{flex:1}
        .items.list .item-btn{width:auto;padding:12px 24px}
        
        @media(max-width:900px){
            .items.grid-3,.items.grid-4{grid-template-columns:repeat(2,1fr)}
        }
        @media(max-width:600px){
            .items.grid-2,.items.grid-3,.items.grid-4{grid-template-columns:1fr}
            .items.list .item{flex-direction:column}
            .items.list .item .cam-view{width:100%}
        }
        
        .item{background:#1a1a1a;border-radius:8px;overflow:hidden;border:1px solid #333;position:relative}
        .item:hover{border-color:#0f0}
        .item-head{padding:10px 12px;background:#222;display:flex;justify-content:space-between;align-items:center}
        .item-head h4{font-size:13px;font-weight:500}
        .item-head small{color:#666;font-size:11px}
        .item img{width:100%;display:block;background:#000;aspect-ratio:16/9;object-fit:cover}
        .cam-view{position:relative;background:#000}
        .cam-badge{position:absolute;top:6px;left:6px;padding:3px 8px;border-radius:4px;font-size:11px;background:rgba(0,0,0,.7);color:#ccc;display:none;pointer-events:none}
        .cam-view.loading .cam-badge{display:block}
        .cam-view.stale .cam-badge{display:block;color:#fc6}
        .cam-view.stale img{opacity:.55}
        
        .item-btn{width:100%;padding:12px;background:#0a0;border:none;color:#fff;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;font-weight:500}
        .item-btn:hover{background:#0c0}
        .item-btn:disabled{opacity:0.5}
        .item-btn.on{background:#0f0;color:#000}
        
        .gpio-btn .item-btn{background:#0a0}
        
        .move-btn{position:absolute;top:5px;left:5px;background:#f90;color:#000;border:none;padding:5px 10px;border-radius:4px;font-size:11px;cursor:pointer;display:none;z-index:10}
        .edit-mode-active .move-btn{display:block}
        
        .scan-results{max-height:200px;overflow-y:auto;margin-top:15px}
        .scan-item{display:flex;justify-content:space-between;align-items:center;padding:8px;background:#222;border-radius:4px;margin-bottom:5px}
        
        .empty{text-align:center;padding:60px;color:#666}
        
        .log{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:15px;margin-top:20px}
        .log h4{color:#666;font-size:11px;text-transform:uppercase;margin-bottom:10px}
        #log{font-family:monospace;font-size:12px;max-height:80px;overflow-y:auto;color:#666}
        .log-ok{color:#0f0}
        .log-err{color:#f44}
        
        .modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.8);z-index:100;justify-content:center;align-items:center}
        .modal.open{display:flex}
        .modal-box{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:20px;width:100%;max-width:500px;max-height:90vh;overflow-y:auto}
        .modal-box h3{color:#0f0;margin-bottom:15px}
        .modal-buttons{display:flex;gap:10px;margin-top:15px}
    </style>
</head>
<body class="{% if edit_mode %}edit-mode-active{% endif %}">
    <div class="header">
        <div class="logo">Camera Control</div>
        <div class="nav">
            <button onclick="toggleEditMode()" class="{% if edit_mode %}edit-mode{% endif %}">
                {% if edit_mode %}Zберегти{% else %}Редагувати{% endif %}
            </button>
            {% if is_admin %}<button onclick="toggleSettings()">Налаштування</button>{% endif %}
            <span class="user">{{ username }}</span>
            <a href="{{ prefix }}/change-password">Пароль</a>
            <a href="{{ prefix }}/logout">Вийти</a>
        </div>
    </div>
    
    <div class="content">
        <div class="view-controls">
            <label>Вигляд:</label>
            <select id="viewMode" onchange="changeView()">
                <option value="grid" {% if settings.view_mode == 'grid' %}selected{% endif %}>Плитка</option>
                <option value="list" {% if settings.view_mode == 'list' %}selected{% endif %}>Список</option>
            </select>
            <label>Колонок:</label>
            <select id="columns" onchange="changeView()">
                <option value="1" {% if settings.columns == 1 %}selected{% endif %}>1</option>
                <option value="2" {% if settings.columns == 2 %}selected{% endif %}>2</option>
                <option value="3" {% if settings.columns == 3 %}selected{% endif %}>3</option>
                <option value="4" {% if settings.columns == 4 %}selected{% endif %}>4</option>
            </select>
        </div>
        
        {% if is_admin %}
        <div class="settings" id="settings">
            <div class="tabs">
                <button class="tab active" onclick="showPanel(this,'cameras')">Камери</button>
                <button class="tab" onclick="showPanel(this,'buttons')">GPIO Кнопки</button>
                <button class="tab" onclick="showPanel(this,'schedules')">Розклади</button>
                <button class="tab" onclick="showPanel(this,'users')">Користувачі</button>
                <button class="tab" onclick="showPanel(this,'scan')">Пошук</button>
                <button class="tab" onclick="showPanel(this,'backup')">Бекап</button>
                <button class="tab" onclick="showPanel(this,'update')">Оновлення</button>
            </div>
            
            <div class="panel active" id="p-cameras">
                <h3>Додати камеру</h3>
                <form id="camForm" onsubmit="saveCam(event)">
                    <input type="hidden" name="id" value="">
                    <div class="form-row">
                        <div class="form-group"><label>Назва</label><input name="name" required></div>
                        <div class="form-group"><label>IP</label><input name="host" required></div>
                        <div class="form-group"><label>Порт</label><input name="port" type="number" value="554"></div>
                        <div class="form-group"><label>Бренд</label>
                            <select name="brand"><option value="hikvision">Hikvision</option><option value="dahua">Dahua</option></select>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group"><label>Логін</label><input name="username" value="admin"></div>
                        <div class="form-group"><label>Пароль</label><input name="password" value="admin"></div>
                        <div class="form-group"><label>Канал</label><input name="channel" type="number" value="1" min="1"></div>
                        <div class="form-group"><label>Субстрім</label>
                            <select name="substream"><option value="1">Так</option><option value="0">Ні</option></select>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group"><label>FPS</label>
                            <select name="fps"><option value="5">5</option><option value="10" selected>10</option><option value="15">15</option><option value="25">25</option></select>
                        </div>
                        <div class="form-group"><label>GPIO пін</label><input name="gpio_pin" type="number"></div>
                        <div class="form-group"><label>Режим</label>
                            <select name="gpio_mode" onchange="toggleDuration(this,'camDuration')"><option value="pulse">Імпульс</option><option value="toggle">Перемикач</option></select>
                        </div>
                        <div class="form-group" id="camDuration"><label>Тривалість</label><input name="gpio_duration" type="number" value="2" step="0.5"></div>
                    </div>
                    <button type="submit" class="btn btn-green" id="camSubmit">+ Додати</button>
                    <button type="button" class="btn btn-gray" onclick="resetCamForm()" style="display:none" id="camCancel">Скасувати</button>
                </form>
                
                {% if cameras %}
                <table style="margin-top:20px">
                    <tr><th>Назва</th><th>IP</th><th>Бренд</th><th>GPIO</th><th></th></tr>
                    {% for c in cameras %}
                    <tr>
                        <td>{{c.name}}</td>
                        <td>{{c.host}}</td>
                        <td><span class="badge {% if c.brand=='dahua' %}badge-dah{% else %}badge-hik{% endif %}">{{c.brand or 'hik'}}</span></td>
                        <td>{{c.gpio_pin or '-'}}</td>
                        <td style="text-align:right">
                            <button class="btn btn-gray" onclick='editCam({{c.id}},{{c.name|tojson}},{{c.host|tojson}},{{c.port}},{{c.brand|tojson}},{{c.username|tojson}},{{c.password|tojson}},{{c.channel}},{{c.substream}},{{c.fps}},{{c.gpio_pin or "null"}},{{c.gpio_mode|tojson}},{{c.gpio_duration}})'>Edit</button>
                            <button class="btn btn-red" onclick="delCam({{c.id}})">Del</button>
                        </td>
                    </tr>
                    {% endfor %}
                </table>
                {% endif %}
            </div>
            
            <div class="panel" id="p-buttons">
                <h3>Додати GPIO кнопку</h3>
                <form id="btnForm" onsubmit="saveBtn(event)">
                    <input type="hidden" name="id" value="">
                    <div class="form-row">
                        <div class="form-group"><label>Назва</label><input name="name" required></div>
                        <div class="form-group"><label>GPIO пін</label><input name="gpio_pin" type="number" required></div>
                        <div class="form-group"><label>Режим</label>
                            <select name="gpio_mode" onchange="toggleDuration(this,'btnDuration')"><option value="pulse">Імпульс</option><option value="toggle">Перемикач</option></select>
                        </div>
                        <div class="form-group" id="btnDuration"><label>Тривалість</label><input name="gpio_duration" type="number" value="2" step="0.5"></div>
                    </div>
                    <button type="submit" class="btn btn-green" id="btnSubmit">+ Додати</button>
                    <button type="button" class="btn btn-gray" onclick="resetBtnForm()" style="display:none" id="btnCancel">Скасувати</button>
                </form>
                
                {% if buttons %}
                <table style="margin-top:20px">
                    <tr><th>Назва</th><th>GPIO</th><th>Режим</th><th>Тривалість</th><th></th></tr>
                    {% for b in buttons %}
                    <tr>
                        <td>{{b.name}}</td>
                        <td>{{b.gpio_pin}}</td>
                        <td>{{b.gpio_mode}}</td>
                        <td>{% if b.gpio_mode == 'pulse' %}{{b.gpio_duration}}s{% else %}-{% endif %}</td>
                        <td style="text-align:right">
                            <button class="btn btn-gray" onclick="editBtn({{b.id}},'{{b.name}}',{{b.gpio_pin}},'{{b.gpio_mode}}',{{b.gpio_duration}})">Edit</button>
                            <button class="btn btn-red" onclick="delBtn({{b.id}})">Del</button>
                        </td>
                    </tr>
                    {% endfor %}
                </table>
                {% endif %}
            </div>
            
            <div class="panel" id="p-schedules">
                <h3>Розклади для GPIO кнопок</h3>
                
                <form id="schedForm" onsubmit="saveSched(event)">
                    <input type="hidden" name="id" value="">
                    <div class="form-row">
                        <div class="form-group">
                            <label>Кнопка</label>
                            <select name="button_id" required>
                                <option value="">Виберіть кнопку</option>
                                {% for b in buttons %}
                                <option value="{{b.id}}">{{b.name}} (GPIO {{b.gpio_pin}})</option>
                                {% endfor %}
                            </select>
                        </div>
                        <div class="form-group"><label>Назва розкладу</label><input name="name" required placeholder="Ввімкнути вранці"></div>
                        <div class="form-group">
                            <label>Дія</label>
                            <select name="action">
                                <option value="pulse">Імпульс</option>
                                <option value="on">Увімкнути</option>
                                <option value="off">Вимкнути</option>
                            </select>
                        </div>
                        <div class="form-group"><label>Час (HH:MM)</label><input name="time" type="time" required></div>
                    </div>
                    
                    <div class="form-group" style="margin-bottom:15px">
                        <label style="margin-bottom:8px">Дні тижня:</label>
                        <div style="display:flex;gap:10px;flex-wrap:wrap">
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_0" value="1" checked style="width:auto;margin:0">
                                <span>Пн</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_1" value="1" checked style="width:auto;margin:0">
                                <span>Вт</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_2" value="1" checked style="width:auto;margin:0">
                                <span>Ср</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_3" value="1" checked style="width:auto;margin:0">
                                <span>Чт</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_4" value="1" checked style="width:auto;margin:0">
                                <span>Пт</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_5" value="1" checked style="width:auto;margin:0">
                                <span>Сб</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_6" value="1" checked style="width:auto;margin:0">
                                <span>Нд</span>
                            </label>
                        </div>
                    </div>
                    
                    <button type="submit" class="btn btn-green" id="schedSubmit">+ Додати розклад</button>
                    <button type="button" class="btn btn-gray" onclick="resetSchedForm()" style="display:none" id="schedCancel">Скасувати</button>
                </form>
                
                {% if schedules %}
                <table style="margin-top:20px">
                    <tr><th>Кнопка</th><th>Назва</th><th>Час</th><th>Дні</th><th>Дія</th><th>Статус</th><th></th></tr>
                    {% for s in schedules %}
                    <tr>
                        <td>{{s.button_name}}</td>
                        <td>{{s.name}}</td>
                        <td style="font-family:monospace">{{s.time}}</td>
                        <td style="font-size:11px">
                            {% if s.days == '1111111' %}Щодня
                            {% elif s.days == '1111100' %}Пн-Пт
                            {% elif s.days == '0000011' %}Сб-Нд
                            {% else %}
                                <span title="Пн Вт Ср Чт Пт Сб Нд">
                                    {%if s.days[0]=='1'%}Пн {%endif%}
                                    {%if s.days[1]=='1'%}Вт {%endif%}
                                    {%if s.days[2]=='1'%}Ср {%endif%}
                                    {%if s.days[3]=='1'%}Чт {%endif%}
                                    {%if s.days[4]=='1'%}Пт {%endif%}
                                    {%if s.days[5]=='1'%}Сб {%endif%}
                                    {%if s.days[6]=='1'%}Нд {%endif%}
                                </span>
                            {% endif %}
                        </td>
                        <td>
                            {% if s.action == 'pulse' %}Імпульс
                            {% elif s.action == 'on' %}ON
                            {% else %}OFF{% endif %}
                        </td>
                        <td>
                            <button class="btn btn-gray" onclick="toggleSched({{s.id}},{{s.enabled}})" style="padding:4px 8px;font-size:11px">
                                {% if s.enabled %}✓{% else %}✗{% endif %}
                            </button>
                        </td>
                        <td style="text-align:right">
                            <button class="btn btn-gray" onclick="editSched({{s.id}},{{s.button_id}},'{{s.name}}','{{s.time}}','{{s.days}}','{{s.action}}')">Edit</button>
                            <button class="btn btn-red" onclick="delSched({{s.id}})">Del</button>
                        </td>
                    </tr>
                    {% endfor %}
                </table>
                {% else %}
                <p style="color:#666;margin-top:20px;text-align:center">Розкладів поки немає</p>
                {% endif %}
            </div>
            
            <div class="panel" id="p-users">
                <h3>Додати користувача</h3>
                <form onsubmit="addUser(event)">
                    <div class="form-row">
                        <div class="form-group"><label>Логін</label><input name="username" required></div>
                        <div class="form-group"><label>Пароль</label><input name="password" type="password" required></div>
                        <div class="form-group"><label>Роль</label>
                            <select name="is_admin"><option value="0">Користувач</option><option value="1">Адмін</option></select>
                        </div>
                    </div>
                    <button type="submit" class="btn btn-green">+ Додати</button>
                </form>
                
                <table style="margin-top:20px">
                    <tr><th>Логін</th><th>Роль</th><th></th></tr>
                    {% for u in users %}
                    <tr>
                        <td>{{u.username}}</td>
                        <td>{% if u.is_admin %}Admin{% else %}User{% endif %}</td>
                        <td style="text-align:right">
                            <button class="btn btn-gray" onclick="chgPass({{u.id}})">Pass</button>
                            {% if u.username != 'admin' %}<button class="btn btn-red" onclick="delUser({{u.id}})">Del</button>{% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </table>
            </div>
            
            <div class="panel" id="p-scan">
                <h3>Пошук камер</h3>
                <button class="btn btn-green" onclick="scanNetwork()" id="scanBtn">Сканувати</button>
                <div class="scan-results" id="scanResults"></div>
            </div>
            
            <div class="panel" id="p-backup">
                <h3>Бекап</h3>
                <div style="display:flex;gap:10px;flex-wrap:wrap">
                    <a href="{{ prefix }}/api/backup" class="btn btn-green" download>Завантажити</a>
                    <button class="btn btn-blue" onclick="document.getElementById('restoreFile').click()">Відновити</button>
                    <input type="file" id="restoreFile" accept=".json" style="display:none" onchange="restoreBackup(this)">
                    <button class="btn btn-red" onclick="resetAll()">Скинути</button>
                </div>
            </div>
            
            <div class="panel" id="p-update">
                <h3>Система оновлення</h3>
                <div id="updateInfo" style="background:#222;padding:15px;border-radius:8px;margin-bottom:15px">
                    <div style="color:#666">Перевірка оновлень...</div>
                </div>
                <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:15px">
                    <button class="btn btn-green" onclick="checkUpdates()" id="checkBtn">Перевірити оновлення</button>
                    <button class="btn btn-blue" onclick="installUpdate()" id="installBtn" style="display:none">Встановити оновлення</button>
                    <button class="btn btn-gray" onclick="restartService()" id="restartBtn">Перезапустити сервіс</button>
                    {% if rollback_to %}<button class="btn btn-gray" onclick="rollbackUpdate('{{rollback_to}}')" id="rollbackBtn">↩ Відкотити до {{rollback_to}}</button>{% endif %}
                    <button class="btn btn-gray" onclick="diagnoseUpdate()" id="diagnoseBtn">🔍 Діагностика</button>
                </div>
                <div id="diagInfo" style="display:none;background:#1a1a1a;padding:15px;border-radius:8px;margin-bottom:15px;font-family:monospace;font-size:11px"></div>
                <div style="margin-top:20px;font-size:12px;color:#666">
                    <div><b>Поточна версія:</b> {{current_version}}</div>
                    <div style="margin-top:10px"><b>Репозиторій:</b> <code>{{repo_url}}</code></div>
                    <div style="margin-top:10px"><b>Тека програми:</b> <code>{{base_dir}}</code></div>
                    <div style="margin-top:15px;padding:10px;background:#1a1a1a;border-radius:4px;line-height:1.6">
                        <b>💡 Ручне оновлення</b>, якщо пристрій без інтернету:<br>
                        1. Завантажте <code>camera_control.txt</code> з {{repo_url}}/<br>
                        2. Скопіюйте на пристрій як <code>{{base_dir}}/camera_control.py</code><br>
                        3. Натисніть «Перезапустити сервіс»
                    </div>
                </div>
            </div>
        </div>
        {% endif %}
        
        {% if items %}
        <div class="items {% if settings.view_mode == 'list' %}list{% else %}grid-{{settings.columns}}{% endif %}" id="itemsContainer">
            {% for item in items %}
            <div class="item {% if item.type == 'button' %}gpio-btn{% endif %}" data-type="{{item.type}}" data-id="{{item.id}}">
                <button class="move-btn" onclick="moveUp(this)">Вгору</button>
                {% if settings.view_mode == 'list' and item.type == 'camera' %}
                <div class="cam-view loading"><img data-cam="{{item.id}}" data-fps="{{item.fps or 10}}" alt="{{item.name}}"><span class="cam-badge">Підключення…</span></div>
                <div class="item-content">
                {% endif %}
                
                <div class="item-head">
                    <h4>{{item.name}}</h4>
                    {% if item.type == 'camera' %}
                    <small>{{item.host}}</small>
                    {% else %}
                    <small>GPIO {{item.gpio_pin}}</small>
                    {% endif %}
                </div>
                
                {% if settings.view_mode != 'list' and item.type == 'camera' %}
                <div class="cam-view loading"><img data-cam="{{item.id}}" data-fps="{{item.fps or 10}}" alt="{{item.name}}"><span class="cam-badge">Підключення…</span></div>
                {% endif %}
                
                {% if item.type == 'button' or item.gpio_pin %}
                <button class="item-btn" id="btn-{{item.type}}-{{item.id}}" onclick="triggerGpio('{{item.type}}',{{item.id}},'{{item.gpio_mode}}')">
                    {% if item.gpio_mode == 'toggle' %}Перемкнути{% else %}Відкрити{% endif %}
                </button>
                {% endif %}
                
                {% if settings.view_mode == 'list' and item.type == 'camera' %}
                </div>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="empty"><p>Немає елементів</p></div>
        {% endif %}
        
        <div class="log">
            <h4>Журнал</h4>
            <div id="log"></div>
        </div>
    </div>
    
    <script>
    // Підтека, якщо програма працює за nginx (наприклад /vorot)
    var BASE = {{ prefix|tojson }};
    
    // CSRF: токен автоматично додається до всіх запитів, що щось змінюють.
    // Заодно до шляхів від кореня (/api/...) додається BASE.
    (function(){
        var m = document.querySelector('meta[name="csrf-token"]');
        var token = m ? m.getAttribute('content') : '';
        var origFetch = window.fetch.bind(window);
        window.fetch = function(url, opts){
            opts = opts || {};
            if (typeof url === 'string' && url.charAt(0) === '/' && url.charAt(1) !== '/') url = BASE + url;
            var method = (opts.method || 'GET').toUpperCase();
            if (method !== 'GET' && method !== 'HEAD') {
                var h = new Headers(opts.headers || {});
                h.set('X-CSRF-Token', token);
                opts.headers = h;
            }
            return origFetch(url, opts);
        };
    })();
    
    var editMode = {% if edit_mode %}true{% else %}false{% endif %};
    
    // ===== ВІДЕО =====
    // iPhone/iPad: окремі кадри - Safari нестабільно тримає MJPEG-потік і не
    // відновлює його після блокування екрана. Інші: MJPEG-потік.
    // Примусово: ?video=poll або ?video=mjpeg в адресі сторінки.
    var POLL_MAX_FPS = {{ poll_max_fps }};
    var VIDEO_MODE = (function(){
        var forced = new URLSearchParams(location.search).get('video');
        if (forced === 'poll' || forced === 'mjpeg') return forced;
        var ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
                  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
        return ios ? 'poll' : 'mjpeg';
    })();
    var camState = {};
    
    function camBadge(img, cls, text){
        var box = img.parentNode;
        box.classList.remove('loading', 'stale');
        if (cls) box.classList.add(cls);
        var b = box.querySelector('.cam-badge');
        if (b && text) b.textContent = text;
    }
    
    function stopCam(img){
        var id = img.getAttribute('data-cam');
        var st = camState[id];
        if (!st) return;
        st.gen++;
        clearTimeout(st.timer);
        if (VIDEO_MODE === 'mjpeg') img.removeAttribute('src');   // закрити з’єднання
    }
    
    function startCam(img){
        var id = img.getAttribute('data-cam');
        var st = camState[id] || (camState[id] = {gen: 0, after: 0, timer: null});
        stopCam(img);
        var gen = ++st.gen;
        
        if (VIDEO_MODE === 'mjpeg') {
            img.onload = function(){ if (gen === st.gen) camBadge(img, null); };
            img.onerror = function(){
                if (gen !== st.gen) return;
                camBadge(img, 'stale', '⚠ Немає сигналу');
                st.timer = setTimeout(function(){ if (gen === st.gen) startCam(img); }, 3000);
            };
            img.src = BASE + '/stream/' + id + '?t=' + Date.now();
            return;
        }
        
        var fps = Math.max(1, Math.min(parseInt(img.getAttribute('data-fps')) || 5, POLL_MAX_FPS));
        var gap = 1000 / fps;
        function next(){
            if (gen !== st.gen || document.hidden) return;
            var t0 = Date.now();
            fetch('/snapshot/' + id + '?after=' + st.after, {cache: 'no-store'}).then(function(r){
                if (r.status === 401) { location.href = BASE + '/login'; throw 'auth'; }
                if (!r.ok) {
                    return r.json().catch(function(){ return {}; }).then(function(d){ throw (d.message || 'Немає сигналу'); });
                }
                st.after = parseInt(r.headers.get('X-Frame-Seq')) || 0;
                var age = parseFloat(r.headers.get('X-Frame-Age')) || 0;
                return r.blob().then(function(b){ return {blob: b, age: age}; });
            }).then(function(res){
                if (gen !== st.gen) return;
                var url = URL.createObjectURL(res.blob);
                var prev = img.src;
                img.onload = function(){
                    if (prev && prev.indexOf('blob:') === 0) URL.revokeObjectURL(prev);
                };
                img.src = url;
                if (res.age > 5) camBadge(img, 'stale', '⚠ Кадр ' + Math.round(res.age) + ' с тому');
                else camBadge(img, null);
                st.timer = setTimeout(next, Math.max(0, gap - (Date.now() - t0)));
            }).catch(function(e){
                if (e === 'auth' || gen !== st.gen) return;
                camBadge(img, 'stale', '⚠ ' + (typeof e === 'string' ? e : 'Немає зв’язку'));
                st.timer = setTimeout(next, 2000);
            });
        }
        next();
    }
    
    function forEachCam(fn){
        var imgs = document.querySelectorAll('img[data-cam]');
        for (var i = 0; i < imgs.length; i++) fn(imgs[i]);
    }
    function startAllCams(){ forEachCam(startCam); }
    function stopAllCams(){ forEachCam(stopCam); }
    
    // Згорнули вкладку / заблокували екран - зупиняємось (економія трафіку і
    // навантаження на пристрій). Повернулись - відновлюємось.
    document.addEventListener('visibilitychange', function(){
        if (document.hidden) stopAllCams(); else startAllCams();
    });
    window.addEventListener('pageshow', function(e){ if (e.persisted) startAllCams(); });
    window.addEventListener('online', startAllCams);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startAllCams);
    else startAllCams();
    
    function toggleSettings(){
        var s = document.getElementById('settings');
        s.className = s.className.indexOf('open') >= 0 ? 'settings' : 'settings open';
    }
    
    function showPanel(btn,name){
        var tabs = document.querySelectorAll('.tab');
        var panels = document.querySelectorAll('.panel');
        for(var i=0; i<tabs.length; i++) tabs[i].className = 'tab';
        for(var i=0; i<panels.length; i++) panels[i].className = 'panel';
        btn.className = 'tab active';
        document.getElementById('p-'+name).className = 'panel active';
    }
    
    function log(m,ok){
        var l=document.getElementById('log');
        l.innerHTML='<div class="'+(ok?'log-ok':'log-err')+'">['+new Date().toLocaleTimeString()+'] '+m+'</div>'+l.innerHTML;
    }
    
    function toggleDuration(sel, durId){
        var dur = document.getElementById(durId);
        dur.className = sel.value === 'toggle' ? 'form-group hidden' : 'form-group';
    }
    
    function changeView(){
        var mode = document.getElementById('viewMode').value;
        var cols = document.getElementById('columns').value;
        fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({view_mode:mode,columns:parseInt(cols)})})
        .then(function(r){return r.json();}).then(function(d){if(d.success)location.reload();});
    }
    
    function getFormData(form){
        var d = {};
        var inputs = form.querySelectorAll('input, select');
        for(var i=0; i<inputs.length; i++){
            var el = inputs[i];
            if(el.name) d[el.name] = el.value;
        }
        return d;
    }
    
    function toggleEditMode(){
        if(editMode){
            var items = [];
            var els = document.querySelectorAll('.item');
            for(var i=0; i<els.length; i++){
                items.push({type: els[i].getAttribute('data-type'), id: parseInt(els[i].getAttribute('data-id'))});
            }
            fetch('/api/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items})})
            .then(function(r){return r.json();}).then(function(d){log(d.message,d.success);location.href=BASE + '/?edit=0';});
        } else {
            location.href = BASE + '/?edit=1';
        }
    }
    
    function moveUp(btn){
        var item = btn.parentElement;
        while(item && item.className.indexOf('item') < 0) item = item.parentElement;
        var prev = item.previousElementSibling;
        if(prev) document.getElementById('itemsContainer').insertBefore(item, prev);
    }
    
    // Camera functions
    function saveCam(e){
        e.preventDefault();
        var d = getFormData(e.target);
        var id = d.id;
        delete d.id;
        var url = id ? '/api/cameras/'+id : '/api/cameras';
        var method = id ? 'PUT' : 'POST';
        fetch(url,{method:method,headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})
        .then(function(r){return r.json();}).then(function(data){log(data.message,data.success);if(data.success)location.reload();});
    }
    
    function editCam(id,name,host,port,brand,user,pass,ch,sub,fps,gpio,mode,dur){
        var f = document.getElementById('camForm');
        f.elements['id'].value = id;
        f.elements['name'].value = name;
        f.elements['host'].value = host;
        f.elements['port'].value = port;
        f.elements['brand'].value = brand;
        f.elements['username'].value = user;
        f.elements['password'].value = pass;
        f.elements['channel'].value = ch;
        f.elements['substream'].value = sub;
        f.elements['fps'].value = fps;
        f.elements['gpio_pin'].value = gpio || '';
        f.elements['gpio_mode'].value = mode;
        f.elements['gpio_duration'].value = dur;
        toggleDuration(f.elements['gpio_mode'], 'camDuration');
        document.getElementById('camSubmit').textContent = 'Зберегти';
        document.getElementById('camCancel').style.display = 'inline';
        f.scrollIntoView();
    }
    
    function resetCamForm(){
        var f = document.getElementById('camForm');
        f.reset();
        f.elements['id'].value = '';
        document.getElementById('camSubmit').textContent = '+ Додати';
        document.getElementById('camCancel').style.display = 'none';
        document.getElementById('camDuration').className = 'form-group';
    }
    
    function delCam(id){
        if(!confirm('Видалити?'))return;
        fetch('/api/cameras/'+id,{method:'DELETE'}).then(function(r){return r.json();}).then(function(d){log(d.message,d.success);if(d.success)location.reload();});
    }
    
    // Button functions
    function saveBtn(e){
        e.preventDefault();
        var d = getFormData(e.target);
        var id = d.id;
        delete d.id;
        var url = id ? '/api/buttons/'+id : '/api/buttons';
        var method = id ? 'PUT' : 'POST';
        fetch(url,{method:method,headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})
        .then(function(r){return r.json();}).then(function(data){log(data.message,data.success);if(data.success)location.reload();});
    }
    
    function editBtn(id,name,gpio,mode,dur){
        var f = document.getElementById('btnForm');
        f.elements['id'].value = id;
        f.elements['name'].value = name;
        f.elements['gpio_pin'].value = gpio;
        f.elements['gpio_mode'].value = mode;
        f.elements['gpio_duration'].value = dur;
        toggleDuration(f.elements['gpio_mode'], 'btnDuration');
        document.getElementById('btnSubmit').textContent = 'Зберегти';
        document.getElementById('btnCancel').style.display = 'inline';
        f.scrollIntoView();
    }
    
    function resetBtnForm(){
        var f = document.getElementById('btnForm');
        f.reset();
        f.elements['id'].value = '';
        document.getElementById('btnSubmit').textContent = '+ Додати';
        document.getElementById('btnCancel').style.display = 'none';
        document.getElementById('btnDuration').className = 'form-group';
    }
    
    function delBtn(id){
        if(!confirm('Видалити?'))return;
        fetch('/api/buttons/'+id,{method:'DELETE'}).then(function(r){return r.json();}).then(function(d){log(d.message,d.success);if(d.success)location.reload();});
    }
    
    // Schedule functions
    function saveSched(e){
        e.preventDefault();
        var f = e.target;
        var d = {};
        
        // Get form data
        d.button_id = f.elements['button_id'].value;
        d.name = f.elements['name'].value;
        d.action = f.elements['action'].value;
        d.time = f.elements['time'].value;
        
        // Build days string (0=Mon, 6=Sun)
        var days = '';
        for(var i=0; i<7; i++){
            days += f.elements['day_'+i].checked ? '1' : '0';
        }
        d.days = days;
        
        var id = f.elements['id'].value;
        var url = id ? '/api/schedules/'+id : '/api/schedules';
        var method = id ? 'PUT' : 'POST';
        
        fetch(url,{method:method,headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})
        .then(function(r){return r.json();}).then(function(data){
            log(data.message,data.success);
            if(data.success)location.reload();
        });
    }
    
    function editSched(id,button_id,name,time,days,action){
        var f = document.getElementById('schedForm');
        f.elements['id'].value = id;
        f.elements['button_id'].value = button_id;
        f.elements['name'].value = name;
        f.elements['time'].value = time;
        f.elements['action'].value = action;
        
        // Set days checkboxes
        for(var i=0; i<7; i++){
            f.elements['day_'+i].checked = days[i] === '1';
        }
        
        document.getElementById('schedSubmit').textContent = 'Зберегти';
        document.getElementById('schedCancel').style.display = 'inline';
        showPanel(document.querySelector('.tab'),'schedules');
        f.scrollIntoView();
    }
    
    function resetSchedForm(){
        var f = document.getElementById('schedForm');
        f.reset();
        f.elements['id'].value = '';
        // Check all days by default
        for(var i=0; i<7; i++){
            f.elements['day_'+i].checked = true;
        }
        document.getElementById('schedSubmit').textContent = '+ Додати розклад';
        document.getElementById('schedCancel').style.display = 'none';
    }
    
    function delSched(id){
        if(!confirm('Видалити розклад?'))return;
        fetch('/api/schedules/'+id,{method:'DELETE'}).then(function(r){return r.json();}).then(function(d){
            log(d.message,d.success);
            if(d.success)location.reload();
        });
    }
    
    function toggleSched(id,enabled){
        var newState = enabled ? 0 : 1;
        fetch('/api/schedules/'+id+'/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:newState})})
        .then(function(r){return r.json();}).then(function(d){
            log(d.message,d.success);
            if(d.success)location.reload();
        });
    }
    
    // User functions
    function addUser(e){
        e.preventDefault();
        fetch('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(getFormData(e.target))})
        .then(function(r){return r.json();}).then(function(d){log(d.message,d.success);if(d.success)location.reload();});
    }
    
    function delUser(id){
        if(!confirm('Видалити?'))return;
        fetch('/api/users/'+id,{method:'DELETE'}).then(function(r){return r.json();}).then(function(d){log(d.message,d.success);if(d.success)location.reload();});
    }
    
    function chgPass(id){
        var p=prompt('Новий пароль:');if(!p)return;
        fetch('/api/users/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})})
        .then(function(r){return r.json();}).then(function(d){log(d.message,d.success);});
    }
    
    function triggerGpio(type,id,mode){
        var btn=document.getElementById('btn-'+type+'-'+id);
        var txt=btn.innerHTML;
        btn.disabled=true;btn.innerHTML='...';
        fetch('/api/gpio/'+type+'/'+id,{method:'POST'}).then(function(r){return r.json();}).then(function(d){
            log(d.message,d.success);
            if(mode==='toggle' && d.success){
                btn.innerHTML = d.state ? 'ON' : 'OFF';
                btn.className = d.state ? 'item-btn on' : 'item-btn';
                btn.disabled = false;
            } else {
                setTimeout(function(){btn.innerHTML=txt;btn.disabled=false;},2000);
            }
        }).catch(function(){
            log('Немає зв’язку з сервером',false);
            btn.innerHTML=txt;btn.disabled=false;
        });
    }
    
    function scanNetwork(){
        var btn=document.getElementById('scanBtn');
        var res=document.getElementById('scanResults');
        btn.disabled=true;btn.innerHTML='...';
        res.innerHTML='<div style="padding:10px;color:#666">Пошук...</div>';
        fetch('/api/scan').then(function(r){return r.json();}).then(function(data){
            btn.disabled=false;btn.innerHTML='Сканувати';
            if(data.length===0){
                res.innerHTML='<div style="padding:10px;color:#666">Не знайдено</div>';
            } else {
                var html = '';
                for(var i=0; i<data.length; i++){
                    var d = data[i];
                    html += '<div class="scan-item"><b>'+d.ip+'</b> ('+d.ports.join(',')+')<button class="btn btn-green" data-ip="'+d.ip+'" onclick="addFromScan(this.dataset.ip)">+</button></div>';
                }
                res.innerHTML = html;
            }
        });
    }
    
    function addFromScan(ip){
        document.querySelector('#camForm input[name="host"]').value=ip;
        showPanel(document.querySelector('.tab'),'cameras');
    }
    
    function restoreBackup(input){
        if(!input.files[0] || !confirm('Відновити?'))return;
        var reader=new FileReader();
        reader.onload=function(e){
            fetch('/api/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:e.target.result})
            .then(function(r){return r.json();}).then(function(d){log(d.message,d.success);if(d.success)setTimeout(function(){location.reload();},500);});
        };
        reader.readAsText(input.files[0]);
    }
    
    function resetAll(){
        if(!confirm('Видалити ВСЕ?') || !confirm('Точно?'))return;
        fetch('/api/reset',{method:'POST'}).then(function(r){return r.json();}).then(function(d){log(d.message,d.success);if(d.success)location.reload();});
    }
    
    function esc(t){
        return String(t==null?'':t).replace(/[&<>"']/g,function(c){
            return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
        });
    }
    
    function checkUpdates(){
        var btn=document.getElementById('checkBtn');
        var info=document.getElementById('updateInfo');
        var installBtn=document.getElementById('installBtn');
        
        btn.disabled=true;btn.innerHTML='Перевірка...';
        info.innerHTML='<div style="color:#666">Перевірка оновлень...</div>';
        installBtn.style.display='none';
        
        fetch('/api/update/check').then(function(r){return r.json();}).then(function(d){
            btn.disabled=false;btn.innerHTML='Перевірити оновлення';
            
            if(!d.success){
                info.innerHTML='<div style="color:#f44">Помилка: '+esc(d.error)+'</div>';
                return;
            }
            
            var html='<div><b>Поточна версія:</b> '+esc(d.current)+'</div>';
            html+='<div style="margin-top:10px"><b>Остання версія:</b> '+esc(d.latest)+'</div>';
            
            if(d.server_older){
                html+='<div style="margin-top:10px;color:#f90">На сервері старіша версія, ніж встановлена - оновлення не потрібне</div>';
            }
            if(d.available){
                html+='<div style="margin-top:10px;color:#0f0"><b>✓ Доступне оновлення!</b></div>';
                if(d.changelog && d.changelog.length>0){
                    html+='<div style="margin-top:10px"><b>Зміни:</b><ul style="margin:5px 0 0 20px">';
                    for(var i=0;i<d.changelog.length;i++){
                        html+='<li>'+esc(d.changelog[i])+'</li>';
                    }
                    html+='</ul></div>';
                }
                installBtn.style.display='inline-block';
            } else {
                html+='<div style="margin-top:10px;color:#666">✓ Ви використовуєте останню версію</div>';
            }
            
            info.innerHTML=html;
        }).catch(function(e){
            btn.disabled=false;btn.innerHTML='Перевірити оновлення';
            info.innerHTML='<div style="color:#f44">Помилка підключення</div>';
        });
    }
    
    function installUpdate(){
        if(!confirm('Встановити оновлення? Поточна версія збережеться як резервна копія.'))return;
        
        var btn=document.getElementById('installBtn');
        var info=document.getElementById('updateInfo');
        
        btn.disabled=true;btn.innerHTML='Встановлення...';
        info.innerHTML='<div style="color:#888">Завантаження, перевірка підпису і пробний запуск нової версії...<br>На NanoPi це може тривати до хвилини.</div>';
        
        fetch('/api/update/install',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
            if(d.success){
                info.innerHTML='<div style="color:#0f0">✓ '+esc(d.message)+'</div><div id="restartStatus" style="margin-top:10px;color:#888">Перезапуск...</div>';
                log(d.message,true);
                waitForRestart(d.started, d.version, document.getElementById('restartStatus'));
            } else {
                btn.disabled=false;btn.innerHTML='Встановити оновлення';
                info.innerHTML='<div style="color:#f44">✗ '+esc(d.message)+'</div><div style="margin-top:8px;color:#888">Поточна версія не змінена.</div>';
                log(d.message,false);
            }
        }).catch(function(e){
            btn.disabled=false;btn.innerHTML='Встановити оновлення';
            info.innerHTML='<div style="color:#f44">Помилка зв’язку з сервером</div>';
        });
    }
    
    // Чекаємо, поки процес перезапуститься: змінюється мітка started.
    // Якщо очікуємо конкретну версію - перевіряємо, що піднялась саме вона.
    function waitForRestart(oldStarted, expectVersion, el){
        var t0=Date.now();
        function tick(){
            var sec=Math.round((Date.now()-t0)/1000);
            if(sec>150){
                el.innerHTML='<span style="color:#f44">Сервер не відповідає понад 2 хв. Перевірте журнал: journalctl -u &lt;сервіс&gt; -n 50</span>';
                return;
            }
            fetch('/api/version',{cache:'no-store'}).then(function(r){
                if(r.status===401){location.href=BASE + '/login';throw 'auth';}
                if(!r.ok)throw 'down';
                return r.json();
            }).then(function(d){
                if(d.started===oldStarted){
                    el.innerHTML='Перезапуск... '+sec+' с';
                    setTimeout(tick,1500);
                } else if(expectVersion && d.version!==expectVersion){
                    el.innerHTML='<span style="color:#f44">✗ Сервер піднявся з версією '+esc(d.version)+' замість '+esc(expectVersion)+'</span>';
                } else {
                    el.innerHTML='<span style="color:#0f0">✓ Працює версія '+esc(d.version)+'</span>';
                    setTimeout(function(){location.reload();},1500);
                }
            }).catch(function(e){
                if(e==='auth')return;
                el.innerHTML='Перезапуск... '+sec+' с';
                setTimeout(tick,1500);
            });
        }
        setTimeout(tick,2000);
    }
    
    function rollbackUpdate(ver){
        if(!confirm('Відкотити програму до версії '+ver+'? Поточна версія збережеться як резервна копія.'))return;
        var btn=document.getElementById('rollbackBtn');
        var info=document.getElementById('updateInfo');
        btn.disabled=true;btn.innerHTML='Відкат...';
        info.innerHTML='<div style="color:#888">Перевірка резервної копії...</div>';
        fetch('/api/update/rollback',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
            if(d.success){
                info.innerHTML='<div style="color:#0f0">✓ '+esc(d.message)+'</div><div id="restartStatus" style="margin-top:10px;color:#888">Перезапуск...</div>';
                log(d.message,true);
                waitForRestart(d.started, d.version, document.getElementById('restartStatus'));
            } else {
                btn.disabled=false;btn.innerHTML='↩ Відкотити до '+esc(ver);
                info.innerHTML='<div style="color:#f44">✗ '+esc(d.message)+'</div>';
                log(d.message,false);
            }
        });
    }
    
    function restartService(){
        if(!confirm('Перезапустити сервіс?'))return;
        var info=document.getElementById('updateInfo');
        fetch('/api/restart',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
            log(d.message,d.success);
            if(d.success){
                info.innerHTML='<div id="restartStatus" style="color:#888">Перезапуск...</div>';
                waitForRestart(d.started, null, document.getElementById('restartStatus'));
            }
        });
    }
    
    function diagnoseUpdate(){
        var btn=document.getElementById('diagnoseBtn');
        var info=document.getElementById('diagInfo');
        
        btn.disabled=true;btn.innerHTML='🔍 Діагностика...';
        info.style.display='block';
        info.innerHTML='<div style="color:#666">Перевірка з’єднання...</div>';
        
        fetch('/api/update/diagnose').then(function(r){return r.json();}).then(function(d){
            btn.disabled=false;btn.innerHTML='🔍 Діагностика';
            
            var html='<div style="color:#0f0"><b>Діагностика системи оновлення:</b></div><br>';
            html+='<table style="width:100%;border:none">';
            html+='<tr><td>Поточна версія:</td><td>'+d.current_version+'</td></tr>';
            html+='<tr><td>Репозиторій:</td><td>'+d.repo_url+'</td></tr>';
            html+='<tr><td>Інтернет:</td><td>'+(d.internet?'<span style="color:#0f0">✓ OK</span>':'<span style="color:#f44">✗ НЕТ</span>')+'</td></tr>';
            html+='<tr><td>DNS:</td><td>'+(d.dns?'<span style="color:#0f0">✓ OK</span>':'<span style="color:#f44">✗ НЕТ</span>')+'</td></tr>';
            html+='<tr><td>Репозиторій доступний:</td><td>'+(d.repo_reachable?'<span style="color:#0f0">✓ OK</span>':'<span style="color:#f44">✗ НЕТ</span>')+'</td></tr>';
            html+='<tr><td>version.json існує:</td><td>'+(d.version_file_exists?'<span style="color:#0f0">✓ OK</span>':'<span style="color:#f44">✗ НЕТ</span>')+'</td></tr>';
            html+='<tr><td>SSL:</td><td>'+(d.ssl_ok?'<span style="color:#0f0">✓ OK</span>':'<span style="color:#f44">✗ ПОМИЛКА</span>')+'</td></tr>';
            html+='</table>';
            
            if(d.error){
                html+='<br><div style="color:#f44"><b>Помилка:</b> '+esc(d.error)+'</div>';
            }
            
            if(d.version_content){
                html+='<br><details style="margin-top:10px"><summary style="cursor:pointer;color:#888">version.json (перші 500 символів)</summary>';
                html+='<pre style="margin-top:5px;padding:10px;background:#000;border-radius:4px;overflow-x:auto">'+esc(d.version_content)+'</pre></details>';
            }
            
            if(!d.repo_reachable || !d.version_file_exists){
                html+='<br><div style="padding:10px;background:#400;border-radius:4px;margin-top:10px">';
                html+='<b>⚠️ Проблема:</b> Репозиторій недоступний.<br><br>';
                html+='<b>Можливі причини:</b><br>';
                html+='1. Файли ще не завантажені на '+d.repo_url+'<br>';
                html+='2. Неправильний URL репозиторію<br>';
                html+='3. Сервер недоступний<br><br>';
                html+='<b>Рішення:</b><br>';
                html+='• Завантажте файли на сервер<br>';
                html+='• Або використайте ручне оновлення (див. інструкцію нижче)<br>';
                html+='• Або встановіть змінну UPDATE_REPO_URL для локального тестування';
                html+='</div>';
            }
            
            info.innerHTML=html;
        }).catch(function(e){
            btn.disabled=false;btn.innerHTML='🔍 Діагностика';
            info.innerHTML='<div style="color:#f44">Помилка діагностики: '+e+'</div>';
        });
    }
    </script>
</body>
</html>
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:system-ui;background:#111;color:#eee;min-height:100vh;display:flex;justify-content:center;align-items:center}
        .box{background:#1a1a1a;padding:40px;border-radius:12px;width:100%;max-width:320px;border:1px solid #333}
        h1{text-align:center;color:#0f0;margin-bottom:25px;font-size:20px}
        .form-group{margin-bottom:15px}
        label{display:block;color:#666;font-size:12px;margin-bottom:5px}
        input{width:100%;padding:12px;background:#222;border:1px solid #333;border-radius:6px;color:#fff;font-size:14px}
        input:focus{outline:none;border-color:#0f0}
        button{width:100%;padding:12px;background:#0f0;border:none;border-radius:6px;color:#000;font-weight:600;cursor:pointer}
        .error{background:#400;border:1px solid #f44;padding:10px;border-radius:6px;margin-bottom:15px;text-align:center}
        .check{display:flex;align-items:center;gap:8px;margin:15px 0}
        .check input{width:auto}
    </style>
</head>
<body>
    <div class="box">
        <h1>Camera Control</h1>
        {% if error %}<div class="error">{{error}}</div>{% endif %}
        <form method="POST">
            <div class="form-group"><label>Login</label><input name="username" required autofocus></div>
            <div class="form-group"><label>Password</label><input name="password" type="password" required></div>
            <div class="check"><input type="checkbox" name="remember" id="r"><label for="r">Remember</label></div>
            <button type="submit">Login</button>
        </form>
    </div>
</body>
</html>
"""

CHANGE_PASSWORD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Зміна пароля</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:system-ui;background:#111;color:#eee;min-height:100vh;display:flex;justify-content:center;align-items:center;padding:15px}
        .box{background:#1a1a1a;padding:35px;border-radius:12px;width:100%;max-width:340px;border:1px solid #333}
        h1{text-align:center;color:#0f0;margin-bottom:8px;font-size:20px}
        .who{text-align:center;color:#666;font-size:13px;margin-bottom:20px}
        .form-group{margin-bottom:15px}
        label{display:block;color:#888;font-size:12px;margin-bottom:5px}
        input{width:100%;padding:12px;background:#222;border:1px solid #333;border-radius:6px;color:#fff;font-size:16px}
        input:focus{outline:none;border-color:#0f0}
        button{width:100%;padding:12px;background:#0f0;border:none;border-radius:6px;color:#000;font-weight:600;cursor:pointer;font-size:15px}
        .error{background:#400;border:1px solid #f44;padding:10px;border-radius:6px;margin-bottom:15px;text-align:center;font-size:14px}
        .warn{background:#431;border:1px solid #f90;padding:10px;border-radius:6px;margin-bottom:15px;font-size:13px;line-height:1.4}
        .hint{color:#666;font-size:12px;margin-top:5px}
        .links{text-align:center;margin-top:18px;font-size:13px}
        .links a{color:#888}
    </style>
</head>
<body>
    <div class="box">
        <h1>Зміна пароля</h1>
        <div class="who">{{ username }}</div>
        {% if forced %}<div class="warn">Поточний пароль надто простий. Щоб продовжити роботу, встановіть новий.</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST" autocomplete="off">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <div class="form-group"><label>Поточний пароль</label>
                <input name="current" type="password" required autofocus autocomplete="current-password"></div>
            <div class="form-group"><label>Новий пароль</label>
                <input name="new" type="password" required minlength="{{ min_len }}" autocomplete="new-password">
                <div class="hint">Щонайменше {{ min_len }} символів, не збігається з логіном</div></div>
            <div class="form-group"><label>Повторіть новий пароль</label>
                <input name="confirm" type="password" required minlength="{{ min_len }}" autocomplete="new-password"></div>
            <button type="submit">Зберегти</button>
        </form>
        <div class="links">{% if forced %}<a href="{{ prefix }}/logout">Вийти</a>{% else %}<a href="{{ prefix }}/">← Назад</a>{% endif %}</div>
    </div>
</body>
</html>
"""

# ========== ROUTES ==========
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = client_ip()
        wait = login_locked(ip)
        if wait:
            return render_template_string(LOGIN_HTML,
                error=f'Забагато невдалих спроб. Спробуйте через {wait // 60 + 1} хв'), 429
        
        u = (request.form.get('username') or '').strip()
        p = request.form.get('password') or ''
        
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username=?', (u,)).fetchone()
        if user:
            ok = verify_password(user['password'], p)
        else:
            check_password_hash(_DUMMY_HASH, p)
            ok = False
        
        if not ok:
            conn.close()
            login_failed(ip)
            print(f"⚠️ Невдалий вхід {u[:32]!r} з {ip}")
            time.sleep(1)
            return render_template_string(LOGIN_HTML, error='Невірний логін або пароль'), 401
        
        # Прозора міграція зі старого SHA-256 на pbkdf2
        stored = user['password']
        if is_legacy_hash(stored):
            stored = hash_password(p)
            conn.execute('UPDATE users SET password=? WHERE id=?', (stored, user['id']))
            conn.commit()
            print(f"🔐 Пароль {user['username']!r} переведено на новий формат")
        conn.close()
        
        login_succeeded(ip)
        session.clear()
        session['logged_in'] = True
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['is_admin'] = bool(user['is_admin'])
        session['pw_stamp'] = pw_stamp(stored)
        # Слабкий пароль (у т.ч. admin/admin) - спершу зміна, потім усе інше
        session['must_change'] = password_problem(p, user['username']) is not None
        session.permanent = bool(request.form.get('remember'))
        
        return redirect(URL_PREFIX + ('/change-password' if session['must_change'] else '/'))
    return render_template_string(LOGIN_HTML, error=None)

@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    user = current_user()
    if not user:
        return redirect(URL_PREFIX + '/login')
    
    forced = bool(session.get('must_change'))
    error = None
    
    if request.method == 'POST':
        ip = client_ip()
        wait = login_locked(ip)
        cur = request.form.get('current') or ''
        new = request.form.get('new') or ''
        confirm = request.form.get('confirm') or ''
        
        if wait:
            error = f'Забагато невдалих спроб. Спробуйте через {wait // 60 + 1} хв'
        elif not verify_password(user['password'], cur):
            login_failed(ip)
            error = 'Невірний поточний пароль'
        elif new != confirm:
            error = 'Нові паролі не збігаються'
        elif new == cur:
            error = 'Новий пароль має відрізнятися від поточного'
        else:
            error = password_problem(new, user['username'])
        
        if not error:
            h = hash_password(new)
            conn = get_db()
            conn.execute('UPDATE users SET password=? WHERE id=?', (h, user['id']))
            conn.commit()
            conn.close()
            session['pw_stamp'] = pw_stamp(h)
            session['must_change'] = False
            print(f"🔐 {user['username']!r} змінив пароль")
            return redirect(URL_PREFIX + '/')
    
    return render_template_string(CHANGE_PASSWORD_HTML, error=error, forced=forced,
                                  username=user['username'], min_len=PASSWORD_MIN_LENGTH)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(URL_PREFIX + '/login')

@app.route('/')
@login_required
def index():
    edit_mode = request.args.get('edit') == '1'
    user_id = session.get('user_id', 0)
    
    conn = get_db()
    cameras = conn.execute('SELECT * FROM cameras WHERE enabled=1').fetchall()
    buttons = conn.execute('SELECT * FROM buttons WHERE enabled=1').fetchall()
    users = conn.execute('SELECT * FROM users').fetchall()
    
    # Get schedules with button names
    schedules = conn.execute('''
        SELECT s.*, b.name as button_name 
        FROM schedules s 
        JOIN buttons b ON s.button_id = b.id 
        ORDER BY s.time
    ''').fetchall()
    
    conn.close()
    
    items = get_user_items(user_id)
    settings = get_user_settings(user_id)
    
    return render_template_string(HTML, 
        items=items, cameras=cameras, buttons=buttons, users=users,
        schedules=schedules,
        username=session.get('username'), is_admin=session.get('is_admin'),
        edit_mode=edit_mode, settings=settings,
        current_version=get_current_version(), repo_url=REPO_URL,
        rollback_to=(list_backups()[0][1] if session.get('is_admin') and list_backups() else None),
        poll_max_fps=POLL_MAX_FPS,
        base_dir=BASE_DIR)

@app.route('/stream/<int:id>')
@login_required
def stream(id):
    s = get_stream(id)
    if not s:
        return "Camera not found", 404
    resp = Response(mjpeg_generator(s), mimetype='multipart/x-mixed-replace; boundary=frame')
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route('/snapshot/<int:id>')
@login_required
def snapshot(id):
    """Один кадр. ?after=N - дочекатися кадру новішого за N (довге опитування)."""
    s = get_stream(id)
    if not s:
        return jsonify({'success': False, 'message': 'Camera not found'}), 404
    after = request.args.get('after', default=0, type=int)
    frame, seq, age, err = s.wait_frame(after_seq=after, timeout=SNAPSHOT_WAIT)
    if frame is None:
        resp = jsonify({'success': False, 'message': err or 'Немає сигналу'})
        resp.status_code = 503
    else:
        resp = Response(frame, mimetype='image/jpeg')
        resp.headers['X-Frame-Seq'] = str(seq)
        resp.headers['X-Frame-Age'] = f"{age:.1f}"
        if err:
            resp.headers['X-Stream-Error'] = urllib.parse.quote(err)
    resp.headers['Cache-Control'] = 'no-store'
    return resp

# ========== API ==========
@app.route('/api/cameras', methods=['POST'])
@admin_required
def api_add_cam():
    d = request.json
    conn = get_db()
    try:
        gpio = int(d['gpio_pin']) if d.get('gpio_pin') else None
        conn.execute('INSERT INTO cameras (name,host,port,username,password,brand,channel,substream,fps,gpio_pin,gpio_mode,gpio_duration) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (d['name'], d['host'], int(d.get('port',554)), d.get('username','admin'), d.get('password','admin'),
             d.get('brand','hikvision'), int(d.get('channel',1)), int(d.get('substream',1)), int(d.get('fps',10)),
             gpio, d.get('gpio_mode','pulse'), float(d.get('gpio_duration',2))))
        conn.commit()
        return jsonify({'success': True, 'message': 'OK'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/cameras/<int:id>', methods=['PUT', 'DELETE'])
@admin_required
def api_cam(id):
    conn = get_db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM cameras WHERE id=?', (id,))
        conn.execute('DELETE FROM user_order WHERE item_type="camera" AND item_id=?', (id,))
        conn.commit()
        conn.close()
        reset_streams(id)
        return jsonify({'success': True, 'message': 'Deleted'})
    d = request.json
    try:
        gpio = int(d['gpio_pin']) if d.get('gpio_pin') else None
        conn.execute('''UPDATE cameras SET name=?,host=?,port=?,username=?,password=?,brand=?,
            channel=?,substream=?,fps=?,gpio_pin=?,gpio_mode=?,gpio_duration=? WHERE id=?''',
            (d['name'], d['host'], int(d.get('port',554)), d.get('username','admin'), d.get('password','admin'),
             d.get('brand','hikvision'), int(d.get('channel',1)), int(d.get('substream',1)), int(d.get('fps',10)),
             gpio, d.get('gpio_mode','pulse'), float(d.get('gpio_duration',2)), id))
        conn.commit()
        reset_streams(id)
        return jsonify({'success': True, 'message': 'Updated'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/buttons', methods=['POST'])
@admin_required
def api_add_button():
    d = request.json
    conn = get_db()
    try:
        conn.execute('INSERT INTO buttons (name,gpio_pin,gpio_mode,gpio_duration) VALUES (?,?,?,?)',
            (d['name'], int(d['gpio_pin']), d.get('gpio_mode','pulse'), float(d.get('gpio_duration',2))))
        conn.commit()
        return jsonify({'success': True, 'message': 'OK'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/buttons/<int:id>', methods=['PUT', 'DELETE'])
@admin_required
def api_button(id):
    conn = get_db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM buttons WHERE id=?', (id,))
        conn.execute('DELETE FROM user_order WHERE item_type="button" AND item_id=?', (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Deleted'})
    d = request.json
    try:
        conn.execute('UPDATE buttons SET name=?,gpio_pin=?,gpio_mode=?,gpio_duration=? WHERE id=?',
            (d['name'], int(d['gpio_pin']), d.get('gpio_mode','pulse'), float(d.get('gpio_duration',2)), id))
        conn.commit()
        return jsonify({'success': True, 'message': 'Updated'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/schedules', methods=['POST'])
@admin_required
def api_add_schedule():
    d = request.json
    conn = get_db()
    try:
        conn.execute('INSERT INTO schedules (button_id, name, action, time, days) VALUES (?,?,?,?,?)',
            (int(d['button_id']), d['name'], d.get('action','pulse'), d['time'], d.get('days','1111111')))
        conn.commit()
        return jsonify({'success': True, 'message': 'Schedule added'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/schedules/<int:id>', methods=['PUT', 'DELETE'])
@admin_required
def api_schedule(id):
    conn = get_db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM schedules WHERE id=?', (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Deleted'})
    d = request.json
    try:
        conn.execute('UPDATE schedules SET button_id=?,name=?,action=?,time=?,days=? WHERE id=?',
            (int(d['button_id']), d['name'], d.get('action','pulse'), d['time'], d.get('days','1111111'), id))
        conn.commit()
        return jsonify({'success': True, 'message': 'Updated'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/schedules/<int:id>/toggle', methods=['POST'])
@admin_required
def api_schedule_toggle(id):
    d = request.json
    conn = get_db()
    try:
        conn.execute('UPDATE schedules SET enabled=? WHERE id=?', (int(d['enabled']), id))
        conn.commit()
        status = 'enabled' if d['enabled'] else 'disabled'
        return jsonify({'success': True, 'message': f'Schedule {status}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    finally:
        conn.close()

@app.route('/api/users', methods=['POST'])
@admin_required
def api_add_user():
    d = request.json or {}
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    if not username:
        return jsonify({'success': False, 'message': 'Вкажіть логін'})
    problem = password_problem(password, username)
    if problem:
        return jsonify({'success': False, 'message': problem})
    conn = get_db()
    try:
        conn.execute('INSERT INTO users (username, password, is_admin) VALUES (?,?,?)',
            (username, hash_password(password), int(d.get('is_admin', 0))))
        conn.commit()
        return jsonify({'success': True, 'message': 'OK'})
    except sqlite3.IntegrityError:
        return jsonify({'success': False, 'message': 'Такий логін уже існує'})
    finally:
        conn.close()

@app.route('/api/users/<int:id>', methods=['PUT', 'DELETE'])
@admin_required
def api_user(id):
    conn = get_db()
    if request.method == 'DELETE':
        u = conn.execute('SELECT username FROM users WHERE id=?', (id,)).fetchone()
        if u and u['username'] == 'admin':
            conn.close()
            return jsonify({'success': False, 'message': 'Cannot delete admin'})
        if id == session.get('user_id'):
            conn.close()
            return jsonify({'success': False, 'message': 'Не можна видалити самого себе'})
        conn.execute('DELETE FROM users WHERE id=?', (id,))
        conn.execute('DELETE FROM user_order WHERE user_id=?', (id,))
        conn.execute('DELETE FROM user_settings WHERE user_id=?', (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Deleted'})
    d = request.json or {}
    if 'password' in d:
        target = conn.execute('SELECT username FROM users WHERE id=?', (id,)).fetchone()
        if not target:
            conn.close()
            return jsonify({'success': False, 'message': 'Користувача не знайдено'})
        problem = password_problem(d['password'] or '', target['username'])
        if problem:
            conn.close()
            return jsonify({'success': False, 'message': problem})
        h = hash_password(d['password'])
        conn.execute('UPDATE users SET password=? WHERE id=?', (h, id))
        conn.commit()
        # Свій пароль - оновлюємо сесію; чужий - його сесії анулюються автоматично
        if id == session.get('user_id'):
            session['pw_stamp'] = pw_stamp(h)
    conn.close()
    return jsonify({'success': True, 'message': 'Пароль змінено'})

@app.route('/api/gpio/<item_type>/<int:id>', methods=['POST'])
@login_required
def api_gpio(item_type, id):
    conn = get_db()
    if item_type == 'camera':
        item = conn.execute('SELECT gpio_pin, gpio_mode, gpio_duration, name FROM cameras WHERE id=?', (id,)).fetchone()
    else:
        item = conn.execute('SELECT gpio_pin, gpio_mode, gpio_duration, name FROM buttons WHERE id=?', (id,)).fetchone()
    conn.close()
    
    if not item or not item['gpio_pin']:
        return jsonify({'success': False, 'message': 'No GPIO'})
    
    # Перевіряємо реле до відповіді: "Відкрито" не має показуватись,
    # якщо GPIO недоступний і ворота фізично не відкрились
    if not init_gpio(item['gpio_pin']):
        reason = GPIO_ERROR if not GPIO_AVAILABLE else 'помилка налаштування піна, див. журнал'
        return jsonify({'success': False, 'message': f"{item['name']}: реле недоступне ({reason})"})
    
    if item['gpio_mode'] == 'toggle':
        ok, state = gpio_toggle(item['gpio_pin'])
        if not ok:
            return jsonify({'success': False, 'state': state,
                            'message': f"{item['name']}: помилка перемикання, див. журнал"})
        return jsonify({'success': True, 'message': item['name'] + ': ' + ('ON' if state else 'OFF'), 'state': state})
    else:
        threading.Thread(target=gpio_pulse, args=(item['gpio_pin'], item['gpio_duration']), daemon=True).start()
        return jsonify({'success': True, 'message': item['name'] + ': ' + str(item['gpio_duration']) + 's'})

@app.route('/api/order', methods=['POST'])
@login_required
def api_order():
    user_id = session.get('user_id', 0)
    items = request.json.get('items', [])
    save_user_order(user_id, items)
    return jsonify({'success': True, 'message': 'OK'})

@app.route('/api/settings', methods=['POST'])
@login_required
def api_settings():
    user_id = session.get('user_id', 0)
    d = request.json
    save_user_settings(user_id, d.get('view_mode', 'grid'), int(d.get('columns', 2)))
    return jsonify({'success': True, 'message': 'OK'})

@app.route('/api/scan')
@admin_required
def api_scan():
    return jsonify(scan_network())

@app.route('/api/backup')
@admin_required
def api_backup():
    backup = create_backup()
    return Response(json.dumps(backup, indent=2, ensure_ascii=False), mimetype='application/json',
        headers={'Content-Disposition': 'attachment; filename=backup.json'})

@app.route('/api/restore', methods=['POST'])
@admin_required
def api_restore():
    try:
        ok, msg = restore_backup(request.json)
        reset_streams()
        return jsonify({'success': ok, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/reset', methods=['POST'])
@admin_required
def api_reset():
    conn = get_db()
    try:
        conn.execute('DELETE FROM user_settings')
        conn.execute('DELETE FROM user_order')
        conn.execute('DELETE FROM buttons')
        conn.execute('DELETE FROM cameras')
        conn.execute('DELETE FROM users WHERE username != "admin"')
        conn.commit()
        reset_streams()
        return jsonify({'success': True, 'message': 'OK'})
    finally:
        conn.close()

@app.route('/api/update/diagnose')
@admin_required
def api_update_diagnose():
    """Diagnose update system"""
    import socket
    
    diag = {
        'repo_url': REPO_URL,
        'version_url': f"{REPO_URL}/version.json",
        'current_version': get_current_version(),
        'internet': False,
        'dns': False,
        'repo_reachable': False,
        'version_file_exists': False,
        'ssl_ok': False
    }
    
    # Check internet
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=3):
            diag['internet'] = True
    except OSError:
        pass
    
    # Check DNS
    try:
        socket.gethostbyname('google.com')
        diag['dns'] = True
    except OSError:          # socket.gaierror - підклас OSError
        pass
    
    # Check if repo is reachable
    try:
        req = urllib.request.Request(f"{REPO_URL}/version.json")
        with urllib.request.urlopen(req, timeout=5, context=SSL_CONTEXT) as response:
            diag['repo_reachable'] = True
            diag['ssl_ok'] = True
            if response.code == 200:
                diag['version_file_exists'] = True
                data = response.read().decode()
                diag['version_content'] = data[:500]  # First 500 chars
    except urllib.error.HTTPError as e:
        diag['error'] = f'HTTP {e.code}: {e.reason}'
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            diag['error'] = f'SSL certificate error: {e.reason}'
        else:
            diag['error'] = f'URL Error: {str(e.reason)}'
    except Exception as e:
        diag['error'] = str(e)
    
    return jsonify(diag)

@app.route('/api/update/check')
@admin_required
def api_update_check():
    """Check for updates"""
    result = check_for_updates()
    return jsonify(result)

@app.route('/api/update/install', methods=['POST'])
@admin_required
def api_update_install():
    """Download, verify, install and restart"""
    success, message, new_version = install_update()
    return jsonify({'success': success, 'message': message,
                    'version': new_version, 'started': PROCESS_STARTED})

@app.route('/api/update/rollback', methods=['POST'])
@admin_required
def api_update_rollback():
    success, message, version = rollback_update()
    return jsonify({'success': success, 'message': message,
                    'version': version, 'started': PROCESS_STARTED})

@app.route('/api/version')
@login_required
def api_version():
    return jsonify({'version': CURRENT_VERSION, 'started': PROCESS_STARTED})

@app.route('/api/restart', methods=['POST'])
@admin_required
def api_restart():
    """Restart the service (self re-exec)"""
    schedule_restart()
    return jsonify({'success': True, 'message': 'Перезапуск...', 'started': PROCESS_STARTED})

if __name__ == '__main__':
    print("=" * 40)
    print("Camera Control v" + CURRENT_VERSION)
    print("=" * 40)
    init_db()
    print("GPIO:   " + ("YES" if GPIO_AVAILABLE else f"NO ({GPIO_ERROR})"))
    print("FFmpeg: " + ("YES" if FFMPEG_AVAILABLE else "NO"))
    print("Version: " + get_current_version())
    print("Dir:     " + BASE_DIR)
    
    # systemctl stop/restart: відпустити реле, якщо імпульс не завершився
    def _on_sigterm(signum, frame):
        release_pulse_pins()
        reset_streams()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)
    
    # Реле - у "вимкнено" одразу при старті, а не при першому натисканні
    pins = init_all_gpio()
    if pins:
        print("Relays:  OFF on pins " + ", ".join(map(str, pins)))
    
    # Start scheduler
    start_scheduler()
    print("Scheduler: STARTED")
    
    print("=" * 40)
    print(f"http://{LISTEN_HOST}:{LISTEN_PORT}{URL_PREFIX}/" + ("  (за проксі, IP з X-Forwarded-For)" if TRUST_PROXY else ""))
    print("Перший вхід: admin / admin (далі - обов'язкова зміна пароля)")
    print("=" * 40)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True)