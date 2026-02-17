#!/usr/bin/env python3
# Camera Control System v4.3

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
import shutil

# ========== VERSION & UPDATE CONFIG ==========
CURRENT_VERSION = "4.4.1"
# Default repository URL - can be overridden with environment variable
REPO_URL = os.environ.get('UPDATE_REPO_URL', 'https://tehnodron.in.ua/camera-control')
VERSION_FILE = "version.txt"

# GPIO
try:
    import OPi.GPIO as GPIO
    GPIO_AVAILABLE = True
    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)
except:
    GPIO_AVAILABLE = False

# FFmpeg
try:
    subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
    FFMPEG_AVAILABLE = True
except:
    FFMPEG_AVAILABLE = False

app = Flask(__name__)
app.secret_key = 'camera-control-secret-2024'

DB_PATH = 'cameras.db'
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
    
    c.execute('SELECT COUNT(*) FROM users WHERE is_admin=1')
    if c.fetchone()[0] == 0:
        c.execute('INSERT INTO users VALUES (NULL,"admin",?,1)', (hashlib.sha256('admin'.encode()).hexdigest(),))
    
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ========== UPDATE SYSTEM ==========
def get_current_version():
    """Get current installed version"""
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, 'r') as f:
                return f.read().strip()
        except:
            pass
    return CURRENT_VERSION

def check_for_updates():
    """Check if updates are available"""
    try:
        # Add timeout and better error handling
        import ssl
        context = ssl._create_unverified_context()
        
        req = urllib.request.Request(
            f"{REPO_URL}/version.json",
            headers={'User-Agent': 'Camera-Control/4.4'}
        )
        
        with urllib.request.urlopen(req, timeout=10, context=context) as response:
            data = json.loads(response.read().decode())
            
            current = get_current_version()
            latest = data.get('version', 'unknown')
            
            return {
                'success': True,
                'current': current,
                'latest': latest,
                'available': latest != current and latest != 'unknown',
                'changelog': data.get('changelog', []),
                'date': data.get('date', ''),
                'url': data.get('files', {}).get('main', {}).get('url', '')
            }
    except urllib.error.HTTPError as e:
        return {
            'success': False,
            'error': f'HTTP Error {e.code}: {e.reason}. Check if files are uploaded to {REPO_URL}',
            'current': get_current_version()
        }
    except urllib.error.URLError as e:
        return {
            'success': False,
            'error': f'Connection failed: {e.reason}. Check internet connection and URL',
            'current': get_current_version()
        }
    except json.JSONDecodeError:
        return {
            'success': False,
            'error': 'Invalid JSON in version.json file',
            'current': get_current_version()
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Unexpected error: {str(e)}',
            'current': get_current_version()
        }

def download_update():
    """Download update file"""
    try:
        info = check_for_updates()
        if not info.get('success'):
            return False, f"Cannot check updates: {info.get('error', 'Unknown error')}"
        
        if not info.get('available'):
            return False, "No update available"
        
        url = info.get('url')
        if not url:
            return False, "Update URL not found in version.json"
        
        temp_file = __file__ + '.new'
        
        print(f"ðŸ“¥ Downloading from: {url}")
        
        # Download with progress
        import ssl
        context = ssl._create_unverified_context()
        
        req = urllib.request.Request(url, headers={'User-Agent': 'Camera-Control/4.4'})
        
        with urllib.request.urlopen(req, timeout=30, context=context) as response:
            with open(temp_file, 'wb') as f:
                f.write(response.read())
        
        # Verify it's a Python file
        with open(temp_file, 'r') as f:
            first_line = f.readline()
            if not first_line.startswith('#!'):
                os.remove(temp_file)
                return False, "Downloaded file is not a valid Python script"
        
        # Check syntax
        try:
            result = subprocess.run(['python3', '-m', 'py_compile', temp_file], 
                         check=True, capture_output=True, text=True)
            print("âœ“ Syntax check passed")
        except subprocess.CalledProcessError as e:
            os.remove(temp_file)
            return False, f"Python syntax error: {e.stderr[:200]}"
        
        print(f"âœ“ Download completed: {os.path.getsize(temp_file)} bytes")
        return True, temp_file
        
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: Cannot download file. Check if camera-control.py is uploaded"
    except urllib.error.URLError as e:
        return False, f"Connection error: {e.reason}"
    except Exception as e:
        return False, f"Download failed: {str(e)}"

def apply_update(temp_file):
    """Apply downloaded update"""
    try:
        # Create backup
        backup_dir = 'backups'
        os.makedirs(backup_dir, exist_ok=True)
        backup_file = f"{backup_dir}/backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.py"
        shutil.copy2(__file__, backup_file)
        
        # Replace file
        shutil.move(temp_file, __file__)
        os.chmod(__file__, 0o755)
        
        # Save version
        info = check_for_updates()
        with open(VERSION_FILE, 'w') as f:
            f.write(info['latest'])
        
        return True, "Update ready - restart required"
        
    except Exception as e:
        # Restore backup if failed
        if os.path.exists(backup_file):
            shutil.copy2(backup_file, __file__)
        return False, str(e)

# ========== AUTH ==========
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
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
    except:
        return False

def gpio_pulse(pin, duration):
    if not init_gpio(pin):
        return False
    try:
        GPIO.output(pin, GPIO.LOW)
        time.sleep(duration)
        GPIO.output(pin, GPIO.HIGH)
        return True
    except:
        return False

def gpio_toggle(pin):
    if not init_gpio(pin):
        return False, False
    new_state = not gpio_states.get(pin, False)
    GPIO.output(pin, GPIO.LOW if new_state else GPIO.HIGH)
    gpio_states[pin] = new_state
    return True, new_state

# ========== SCHEDULER ==========
def scheduler_worker():
    """Background thread that checks and executes schedules"""
    global scheduler_running
    print("ðŸ“… Scheduler started")
    
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
                    # Check if today is enabled (days format: "1111111" for Mon-Sun)
                    days = schedule['days'] if schedule['days'] else '1111111'
                    if len(days) > current_day and days[current_day] == '1':
                        # Execute action
                        action = schedule['action']
                        pin = schedule['gpio_pin']
                        duration = schedule['gpio_duration'] if schedule['gpio_duration'] else 2.0
                        
                        print(f"ðŸ“… Executing schedule: {schedule['name']} - {action} on pin {pin}")
                        
                        if action == 'pulse':
                            threading.Thread(target=lambda: gpio_pulse(pin, duration), daemon=True).start()
                        elif action == 'on':
                            if init_gpio(pin):
                                GPIO.output(pin, GPIO.LOW)
                                gpio_states[pin] = True
                        elif action == 'off':
                            if init_gpio(pin):
                                GPIO.output(pin, GPIO.HIGH)
                                gpio_states[pin] = False
            
        except Exception as e:
            print(f"âŒ Scheduler error: {e}")
        
        # Check every 5 seconds
        time.sleep(5)
    
    print("ðŸ“… Scheduler stopped")

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

def generate_rtsp(camera_id):
    if not FFMPEG_AVAILABLE:
        return
    
    conn = get_db()
    cam = conn.execute('SELECT * FROM cameras WHERE id=?', (camera_id,)).fetchone()
    conn.close()
    if not cam:
        return
    
    fps = cam['fps'] if cam['fps'] else 10
    rtsp_url = get_rtsp_url(dict(cam))
    
    cmd = ['ffmpeg', '-rtsp_transport', 'tcp', '-i', rtsp_url, '-f', 'mjpeg', '-q:v', '5', '-r', str(fps), '-']
    
    while True:
        process = None
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1000000)
            buffer = b''
            
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                buffer = buffer + chunk
                
                while True:
                    start = buffer.find(b'\xff\xd8')
                    if start == -1:
                        buffer = b''
                        break
                    end = buffer.find(b'\xff\xd9', start)
                    if end == -1:
                        buffer = buffer[start:]
                        break
                    jpg = buffer[start:end+2]
                    buffer = buffer[end+2:]
                    if len(jpg) > 1000:
                        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpg + b'\r\n'
        except:
            time.sleep(2)
        finally:
            if process:
                try:
                    process.kill()
                except:
                    pass
            time.sleep(1)

# ========== NETWORK SCAN ==========
def scan_network():
    results = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "192.168.1.1"
    
    subnet = '.'.join(local_ip.split('.')[:-1])
    
    def check_port(ip, port, timeout=0.5):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, port))
            sock.close()
            return result == 0
        except:
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
        .items.list .item img{width:200px;aspect-ratio:16/9}
        .items.list .item-content{flex:1;display:flex;flex-direction:column}
        .items.list .item-head{flex:1}
        .items.list .item-btn{width:auto;padding:12px 24px}
        
        @media(max-width:900px){
            .items.grid-3,.items.grid-4{grid-template-columns:repeat(2,1fr)}
        }
        @media(max-width:600px){
            .items.grid-2,.items.grid-3,.items.grid-4{grid-template-columns:1fr}
            .items.list .item{flex-direction:column}
            .items.list .item img{width:100%}
        }
        
        .item{background:#1a1a1a;border-radius:8px;overflow:hidden;border:1px solid #333;position:relative}
        .item:hover{border-color:#0f0}
        .item-head{padding:10px 12px;background:#222;display:flex;justify-content:space-between;align-items:center}
        .item-head h4{font-size:13px;font-weight:500}
        .item-head small{color:#666;font-size:11px}
        .item img{width:100%;display:block;background:#000;aspect-ratio:16/9;object-fit:cover}
        
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
                {% if edit_mode %}ZÐ±ÐµÑ€ÐµÐ³Ñ‚Ð¸{% else %}Ð ÐµÐ´Ð°Ð³ÑƒÐ²Ð°Ñ‚Ð¸{% endif %}
            </button>
            {% if is_admin %}<button onclick="toggleSettings()">ÐÐ°Ð»Ð°ÑˆÑ‚ÑƒÐ²Ð°Ð½Ð½Ñ</button>{% endif %}
            <span class="user">{{ username }}</span>
            <a href="/logout">Ð’Ð¸Ð¹Ñ‚Ð¸</a>
        </div>
    </div>
    
    <div class="content">
        <div class="view-controls">
            <label>Ð’Ð¸Ð³Ð»ÑÐ´:</label>
            <select id="viewMode" onchange="changeView()">
                <option value="grid" {% if settings.view_mode == 'grid' %}selected{% endif %}>ÐŸÐ»Ð¸Ñ‚ÐºÐ°</option>
                <option value="list" {% if settings.view_mode == 'list' %}selected{% endif %}>Ð¡Ð¿Ð¸ÑÐ¾Ðº</option>
            </select>
            <label>ÐšÐ¾Ð»Ð¾Ð½Ð¾Ðº:</label>
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
                <button class="tab active" onclick="showPanel(this,'cameras')">ÐšÐ°Ð¼ÐµÑ€Ð¸</button>
                <button class="tab" onclick="showPanel(this,'buttons')">GPIO ÐšÐ½Ð¾Ð¿ÐºÐ¸</button>
                <button class="tab" onclick="showPanel(this,'schedules')">Ð Ð¾Ð·ÐºÐ»Ð°Ð´Ð¸</button>
                <button class="tab" onclick="showPanel(this,'users')">ÐšÐ¾Ñ€Ð¸ÑÑ‚ÑƒÐ²Ð°Ñ‡Ñ–</button>
                <button class="tab" onclick="showPanel(this,'scan')">ÐŸÐ¾ÑˆÑƒÐº</button>
                <button class="tab" onclick="showPanel(this,'backup')">Ð‘ÐµÐºÐ°Ð¿</button>
                <button class="tab" onclick="showPanel(this,'update')">ÐžÐ½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ</button>
            </div>
            
            <div class="panel active" id="p-cameras">
                <h3>Ð”Ð¾Ð´Ð°Ñ‚Ð¸ ÐºÐ°Ð¼ÐµÑ€Ñƒ</h3>
                <form id="camForm" onsubmit="saveCam(event)">
                    <input type="hidden" name="id" value="">
                    <div class="form-row">
                        <div class="form-group"><label>ÐÐ°Ð·Ð²Ð°</label><input name="name" required></div>
                        <div class="form-group"><label>IP</label><input name="host" required></div>
                        <div class="form-group"><label>ÐŸÐ¾Ñ€Ñ‚</label><input name="port" type="number" value="554"></div>
                        <div class="form-group"><label>Ð‘Ñ€ÐµÐ½Ð´</label>
                            <select name="brand"><option value="hikvision">Hikvision</option><option value="dahua">Dahua</option></select>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group"><label>Ð›Ð¾Ð³Ñ–Ð½</label><input name="username" value="admin"></div>
                        <div class="form-group"><label>ÐŸÐ°Ñ€Ð¾Ð»ÑŒ</label><input name="password" value="admin"></div>
                        <div class="form-group"><label>ÐšÐ°Ð½Ð°Ð»</label><input name="channel" type="number" value="1" min="1"></div>
                        <div class="form-group"><label>Ð¡ÑƒÐ±ÑÑ‚Ñ€Ñ–Ð¼</label>
                            <select name="substream"><option value="1">Ð¢Ð°Ðº</option><option value="0">ÐÑ–</option></select>
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-group"><label>FPS</label>
                            <select name="fps"><option value="5">5</option><option value="10" selected>10</option><option value="15">15</option><option value="25">25</option></select>
                        </div>
                        <div class="form-group"><label>GPIO Ð¿Ñ–Ð½</label><input name="gpio_pin" type="number"></div>
                        <div class="form-group"><label>Ð ÐµÐ¶Ð¸Ð¼</label>
                            <select name="gpio_mode" onchange="toggleDuration(this,'camDuration')"><option value="pulse">Ð†Ð¼Ð¿ÑƒÐ»ÑŒÑ</option><option value="toggle">ÐŸÐµÑ€ÐµÐ¼Ð¸ÐºÐ°Ñ‡</option></select>
                        </div>
                        <div class="form-group" id="camDuration"><label>Ð¢Ñ€Ð¸Ð²Ð°Ð»Ñ–ÑÑ‚ÑŒ</label><input name="gpio_duration" type="number" value="2" step="0.5"></div>
                    </div>
                    <button type="submit" class="btn btn-green" id="camSubmit">+ Ð”Ð¾Ð´Ð°Ñ‚Ð¸</button>
                    <button type="button" class="btn btn-gray" onclick="resetCamForm()" style="display:none" id="camCancel">Ð¡ÐºÐ°ÑÑƒÐ²Ð°Ñ‚Ð¸</button>
                </form>
                
                {% if cameras %}
                <table style="margin-top:20px">
                    <tr><th>ÐÐ°Ð·Ð²Ð°</th><th>IP</th><th>Ð‘Ñ€ÐµÐ½Ð´</th><th>GPIO</th><th></th></tr>
                    {% for c in cameras %}
                    <tr>
                        <td>{{c.name}}</td>
                        <td>{{c.host}}</td>
                        <td><span class="badge {% if c.brand=='dahua' %}badge-dah{% else %}badge-hik{% endif %}">{{c.brand or 'hik'}}</span></td>
                        <td>{{c.gpio_pin or '-'}}</td>
                        <td style="text-align:right">
                            <button class="btn btn-gray" onclick="editCam({{c.id}},'{{c.name}}','{{c.host}}',{{c.port}},'{{c.brand}}','{{c.username}}','{{c.password}}',{{c.channel}},{{c.substream}},{{c.fps}},{{c.gpio_pin or 'null'}},'{{c.gpio_mode}}',{{c.gpio_duration}})">Edit</button>
                            <button class="btn btn-red" onclick="delCam({{c.id}})">Del</button>
                        </td>
                    </tr>
                    {% endfor %}
                </table>
                {% endif %}
            </div>
            
            <div class="panel" id="p-buttons">
                <h3>Ð”Ð¾Ð´Ð°Ñ‚Ð¸ GPIO ÐºÐ½Ð¾Ð¿ÐºÑƒ</h3>
                <form id="btnForm" onsubmit="saveBtn(event)">
                    <input type="hidden" name="id" value="">
                    <div class="form-row">
                        <div class="form-group"><label>ÐÐ°Ð·Ð²Ð°</label><input name="name" required></div>
                        <div class="form-group"><label>GPIO Ð¿Ñ–Ð½</label><input name="gpio_pin" type="number" required></div>
                        <div class="form-group"><label>Ð ÐµÐ¶Ð¸Ð¼</label>
                            <select name="gpio_mode" onchange="toggleDuration(this,'btnDuration')"><option value="pulse">Ð†Ð¼Ð¿ÑƒÐ»ÑŒÑ</option><option value="toggle">ÐŸÐµÑ€ÐµÐ¼Ð¸ÐºÐ°Ñ‡</option></select>
                        </div>
                        <div class="form-group" id="btnDuration"><label>Ð¢Ñ€Ð¸Ð²Ð°Ð»Ñ–ÑÑ‚ÑŒ</label><input name="gpio_duration" type="number" value="2" step="0.5"></div>
                    </div>
                    <button type="submit" class="btn btn-green" id="btnSubmit">+ Ð”Ð¾Ð´Ð°Ñ‚Ð¸</button>
                    <button type="button" class="btn btn-gray" onclick="resetBtnForm()" style="display:none" id="btnCancel">Ð¡ÐºÐ°ÑÑƒÐ²Ð°Ñ‚Ð¸</button>
                </form>
                
                {% if buttons %}
                <table style="margin-top:20px">
                    <tr><th>ÐÐ°Ð·Ð²Ð°</th><th>GPIO</th><th>Ð ÐµÐ¶Ð¸Ð¼</th><th>Ð¢Ñ€Ð¸Ð²Ð°Ð»Ñ–ÑÑ‚ÑŒ</th><th></th></tr>
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
                <h3>Ð Ð¾Ð·ÐºÐ»Ð°Ð´Ð¸ Ð´Ð»Ñ GPIO ÐºÐ½Ð¾Ð¿Ð¾Ðº</h3>
                
                <form id="schedForm" onsubmit="saveSched(event)">
                    <input type="hidden" name="id" value="">
                    <div class="form-row">
                        <div class="form-group">
                            <label>ÐšÐ½Ð¾Ð¿ÐºÐ°</label>
                            <select name="button_id" required>
                                <option value="">Ð’Ð¸Ð±ÐµÑ€Ñ–Ñ‚ÑŒ ÐºÐ½Ð¾Ð¿ÐºÑƒ</option>
                                {% for b in buttons %}
                                <option value="{{b.id}}">{{b.name}} (GPIO {{b.gpio_pin}})</option>
                                {% endfor %}
                            </select>
                        </div>
                        <div class="form-group"><label>ÐÐ°Ð·Ð²Ð° Ñ€Ð¾Ð·ÐºÐ»Ð°Ð´Ñƒ</label><input name="name" required placeholder="Ð’Ð²Ñ–Ð¼ÐºÐ½ÑƒÑ‚Ð¸ Ð²Ñ€Ð°Ð½Ñ†Ñ–"></div>
                        <div class="form-group">
                            <label>Ð”Ñ–Ñ</label>
                            <select name="action">
                                <option value="pulse">Ð†Ð¼Ð¿ÑƒÐ»ÑŒÑ</option>
                                <option value="on">Ð£Ð²Ñ–Ð¼ÐºÐ½ÑƒÑ‚Ð¸</option>
                                <option value="off">Ð’Ð¸Ð¼ÐºÐ½ÑƒÑ‚Ð¸</option>
                            </select>
                        </div>
                        <div class="form-group"><label>Ð§Ð°Ñ (HH:MM)</label><input name="time" type="time" required></div>
                    </div>
                    
                    <div class="form-group" style="margin-bottom:15px">
                        <label style="margin-bottom:8px">Ð”Ð½Ñ– Ñ‚Ð¸Ð¶Ð½Ñ:</label>
                        <div style="display:flex;gap:10px;flex-wrap:wrap">
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_0" value="1" checked style="width:auto;margin:0">
                                <span>ÐŸÐ½</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_1" value="1" checked style="width:auto;margin:0">
                                <span>Ð’Ñ‚</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_2" value="1" checked style="width:auto;margin:0">
                                <span>Ð¡Ñ€</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_3" value="1" checked style="width:auto;margin:0">
                                <span>Ð§Ñ‚</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_4" value="1" checked style="width:auto;margin:0">
                                <span>ÐŸÑ‚</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_5" value="1" checked style="width:auto;margin:0">
                                <span>Ð¡Ð±</span>
                            </label>
                            <label style="display:flex;align-items:center;gap:5px;margin:0;cursor:pointer">
                                <input type="checkbox" name="day_6" value="1" checked style="width:auto;margin:0">
                                <span>ÐÐ´</span>
                            </label>
                        </div>
                    </div>
                    
                    <button type="submit" class="btn btn-green" id="schedSubmit">+ Ð”Ð¾Ð´Ð°Ñ‚Ð¸ Ñ€Ð¾Ð·ÐºÐ»Ð°Ð´</button>
                    <button type="button" class="btn btn-gray" onclick="resetSchedForm()" style="display:none" id="schedCancel">Ð¡ÐºÐ°ÑÑƒÐ²Ð°Ñ‚Ð¸</button>
                </form>
                
                {% if schedules %}
                <table style="margin-top:20px">
                    <tr><th>ÐšÐ½Ð¾Ð¿ÐºÐ°</th><th>ÐÐ°Ð·Ð²Ð°</th><th>Ð§Ð°Ñ</th><th>Ð”Ð½Ñ–</th><th>Ð”Ñ–Ñ</th><th>Ð¡Ñ‚Ð°Ñ‚ÑƒÑ</th><th></th></tr>
                    {% for s in schedules %}
                    <tr>
                        <td>{{s.button_name}}</td>
                        <td>{{s.name}}</td>
                        <td style="font-family:monospace">{{s.time}}</td>
                        <td style="font-size:11px">
                            {% if s.days == '1111111' %}Ð©Ð¾Ð´Ð½Ñ
                            {% elif s.days == '1111100' %}ÐŸÐ½-ÐŸÑ‚
                            {% elif s.days == '0000011' %}Ð¡Ð±-ÐÐ´
                            {% else %}
                                <span title="ÐŸÐ½ Ð’Ñ‚ Ð¡Ñ€ Ð§Ñ‚ ÐŸÑ‚ Ð¡Ð± ÐÐ´">
                                    {%if s.days[0]=='1'%}ÐŸÐ½ {%endif%}
                                    {%if s.days[1]=='1'%}Ð’Ñ‚ {%endif%}
                                    {%if s.days[2]=='1'%}Ð¡Ñ€ {%endif%}
                                    {%if s.days[3]=='1'%}Ð§Ñ‚ {%endif%}
                                    {%if s.days[4]=='1'%}ÐŸÑ‚ {%endif%}
                                    {%if s.days[5]=='1'%}Ð¡Ð± {%endif%}
                                    {%if s.days[6]=='1'%}ÐÐ´ {%endif%}
                                </span>
                            {% endif %}
                        </td>
                        <td>
                            {% if s.action == 'pulse' %}Ð†Ð¼Ð¿ÑƒÐ»ÑŒÑ
                            {% elif s.action == 'on' %}ON
                            {% else %}OFF{% endif %}
                        </td>
                        <td>
                            <button class="btn btn-gray" onclick="toggleSched({{s.id}},{{s.enabled}})" style="padding:4px 8px;font-size:11px">
                                {% if s.enabled %}âœ“{% else %}âœ—{% endif %}
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
                <p style="color:#666;margin-top:20px;text-align:center">Ð Ð¾Ð·ÐºÐ»Ð°Ð´Ñ–Ð² Ð¿Ð¾ÐºÐ¸ Ð½ÐµÐ¼Ð°Ñ”</p>
                {% endif %}
            </div>
            
            <div class="panel" id="p-users">
                <h3>Ð”Ð¾Ð´Ð°Ñ‚Ð¸ ÐºÐ¾Ñ€Ð¸ÑÑ‚ÑƒÐ²Ð°Ñ‡Ð°</h3>
                <form onsubmit="addUser(event)">
                    <div class="form-row">
                        <div class="form-group"><label>Ð›Ð¾Ð³Ñ–Ð½</label><input name="username" required></div>
                        <div class="form-group"><label>ÐŸÐ°Ñ€Ð¾Ð»ÑŒ</label><input name="password" type="password" required></div>
                        <div class="form-group"><label>Ð Ð¾Ð»ÑŒ</label>
                            <select name="is_admin"><option value="0">ÐšÐ¾Ñ€Ð¸ÑÑ‚ÑƒÐ²Ð°Ñ‡</option><option value="1">ÐÐ´Ð¼Ñ–Ð½</option></select>
                        </div>
                    </div>
                    <button type="submit" class="btn btn-green">+ Ð”Ð¾Ð´Ð°Ñ‚Ð¸</button>
                </form>
                
                <table style="margin-top:20px">
                    <tr><th>Ð›Ð¾Ð³Ñ–Ð½</th><th>Ð Ð¾Ð»ÑŒ</th><th></th></tr>
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
                <h3>ÐŸÐ¾ÑˆÑƒÐº ÐºÐ°Ð¼ÐµÑ€</h3>
                <button class="btn btn-green" onclick="scanNetwork()" id="scanBtn">Ð¡ÐºÐ°Ð½ÑƒÐ²Ð°Ñ‚Ð¸</button>
                <div class="scan-results" id="scanResults"></div>
            </div>
            
            <div class="panel" id="p-backup">
                <h3>Ð‘ÐµÐºÐ°Ð¿</h3>
                <div style="display:flex;gap:10px;flex-wrap:wrap">
                    <a href="/api/backup" class="btn btn-green" download>Ð—Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶Ð¸Ñ‚Ð¸</a>
                    <button class="btn btn-blue" onclick="document.getElementById('restoreFile').click()">Ð’Ñ–Ð´Ð½Ð¾Ð²Ð¸Ñ‚Ð¸</button>
                    <input type="file" id="restoreFile" accept=".json" style="display:none" onchange="restoreBackup(this)">
                    <button class="btn btn-red" onclick="resetAll()">Ð¡ÐºÐ¸Ð½ÑƒÑ‚Ð¸</button>
                </div>
            </div>
            
            <div class="panel" id="p-update">
                <h3>Ð¡Ð¸ÑÑ‚ÐµÐ¼Ð° Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ</h3>
                <div id="updateInfo" style="background:#222;padding:15px;border-radius:8px;margin-bottom:15px">
                    <div style="color:#666">ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€ÐºÐ° Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½ÑŒ...</div>
                </div>
                <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:15px">
                    <button class="btn btn-green" onclick="checkUpdates()" id="checkBtn">ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€Ð¸Ñ‚Ð¸ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ</button>
                    <button class="btn btn-blue" onclick="installUpdate()" id="installBtn" style="display:none">Ð’ÑÑ‚Ð°Ð½Ð¾Ð²Ð¸Ñ‚Ð¸ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ</button>
                    <button class="btn btn-gray" onclick="restartService()" id="restartBtn">ÐŸÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÑ‚Ð¸Ñ‚Ð¸ ÑÐµÑ€Ð²Ñ–Ñ</button>
                    <button class="btn btn-gray" onclick="diagnoseUpdate()" id="diagnoseBtn">ðŸ” Ð”Ñ–Ð°Ð³Ð½Ð¾ÑÑ‚Ð¸ÐºÐ°</button>
                </div>
                <div id="diagInfo" style="display:none;background:#1a1a1a;padding:15px;border-radius:8px;margin-bottom:15px;font-family:monospace;font-size:11px"></div>
                <div style="margin-top:20px;font-size:12px;color:#666">
                    <div><b>ÐŸÐ¾Ñ‚Ð¾Ñ‡Ð½Ð° Ð²ÐµÑ€ÑÑ–Ñ:</b> {{current_version}}</div>
                    <div style="margin-top:10px"><b>Ð ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ñ–Ð¹:</b> <code>{{repo_url}}</code></div>
                    <div style="margin-top:10px"><b>ÐšÐ¾Ð¼Ð°Ð½Ð´Ð½Ð¸Ð¹ Ñ€ÑÐ´Ð¾Ðº:</b></div>
                    <div style="background:#222;padding:10px;border-radius:4px;margin-top:5px;font-family:monospace;font-size:11px">
                        curl -sSL {{repo_url}}/update.sh | bash
                    </div>
                    <div style="margin-top:15px;padding:10px;background:#1a1a1a;border-radius:4px">
                        <b>ðŸ’¡ ÐŸÐ¾Ñ€Ð°Ð´Ð°:</b> Ð¯ÐºÑ‰Ð¾ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ Ð½Ðµ Ð¿Ñ€Ð°Ñ†ÑŽÑ” Ñ‡ÐµÑ€ÐµÐ· Ð†Ð½Ñ‚ÐµÑ€Ð½ÐµÑ‚, Ð¼Ð¾Ð¶Ð½Ð° Ð¾Ð½Ð¾Ð²Ð¸Ñ‚Ð¸ Ð²Ñ€ÑƒÑ‡Ð½Ñƒ:<br>
                        1. Ð—Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶Ñ‚Ðµ camera-control.py Ð· Ñ€ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ñ–ÑŽ<br>
                        2. Ð¡ÐºÐ¾Ð¿Ñ–ÑŽÐ¹Ñ‚Ðµ Ð½Ð° RNPi Ñ‡ÐµÑ€ÐµÐ· SCP/SFTP<br>
                        3. <code>sudo cp camera-control.py /opt/camera-control/</code><br>
                        4. <code>sudo systemctl restart camera-control</code>
                    </div>
                </div>
            </div>
        </div>
        {% endif %}
        
        {% if items %}
        <div class="items {% if settings.view_mode == 'list' %}list{% else %}grid-{{settings.columns}}{% endif %}" id="itemsContainer">
            {% for item in items %}
            <div class="item {% if item.type == 'button' %}gpio-btn{% endif %}" data-type="{{item.type}}" data-id="{{item.id}}">
                <button class="move-btn" onclick="moveUp(this)">Ð’Ð³Ð¾Ñ€Ñƒ</button>
                {% if settings.view_mode == 'list' and item.type == 'camera' %}
                <img src="/stream/{{item.id}}" alt="{{item.name}}">
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
                <img src="/stream/{{item.id}}" alt="{{item.name}}">
                {% endif %}
                
                {% if item.type == 'button' or item.gpio_pin %}
                <button class="item-btn" id="btn-{{item.type}}-{{item.id}}" onclick="triggerGpio('{{item.type}}',{{item.id}},'{{item.gpio_mode}}')">
                    {% if item.gpio_mode == 'toggle' %}ÐŸÐµÑ€ÐµÐ¼ÐºÐ½ÑƒÑ‚Ð¸{% else %}Ð’Ñ–Ð´ÐºÑ€Ð¸Ñ‚Ð¸{% endif %}
                </button>
                {% endif %}
                
                {% if settings.view_mode == 'list' and item.type == 'camera' %}
                </div>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="empty"><p>ÐÐµÐ¼Ð°Ñ” ÐµÐ»ÐµÐ¼ÐµÐ½Ñ‚Ñ–Ð²</p></div>
        {% endif %}
        
        <div class="log">
            <h4>Ð–ÑƒÑ€Ð½Ð°Ð»</h4>
            <div id="log"></div>
        </div>
    </div>
    
    <script>
    var editMode = {% if edit_mode %}true{% else %}false{% endif %};
    
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
            .then(function(r){return r.json();}).then(function(d){log(d.message,d.success);location.href='/?edit=0';});
        } else {
            location.href = '/?edit=1';
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
        document.getElementById('camSubmit').textContent = 'Ð—Ð±ÐµÑ€ÐµÐ³Ñ‚Ð¸';
        document.getElementById('camCancel').style.display = 'inline';
        f.scrollIntoView();
    }
    
    function resetCamForm(){
        var f = document.getElementById('camForm');
        f.reset();
        f.elements['id'].value = '';
        document.getElementById('camSubmit').textContent = '+ Ð”Ð¾Ð´Ð°Ñ‚Ð¸';
        document.getElementById('camCancel').style.display = 'none';
        document.getElementById('camDuration').className = 'form-group';
    }
    
    function delCam(id){
        if(!confirm('Ð’Ð¸Ð´Ð°Ð»Ð¸Ñ‚Ð¸?'))return;
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
        document.getElementById('btnSubmit').textContent = 'Ð—Ð±ÐµÑ€ÐµÐ³Ñ‚Ð¸';
        document.getElementById('btnCancel').style.display = 'inline';
        f.scrollIntoView();
    }
    
    function resetBtnForm(){
        var f = document.getElementById('btnForm');
        f.reset();
        f.elements['id'].value = '';
        document.getElementById('btnSubmit').textContent = '+ Ð”Ð¾Ð´Ð°Ñ‚Ð¸';
        document.getElementById('btnCancel').style.display = 'none';
        document.getElementById('btnDuration').className = 'form-group';
    }
    
    function delBtn(id){
        if(!confirm('Ð’Ð¸Ð´Ð°Ð»Ð¸Ñ‚Ð¸?'))return;
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
        
        document.getElementById('schedSubmit').textContent = 'Ð—Ð±ÐµÑ€ÐµÐ³Ñ‚Ð¸';
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
        document.getElementById('schedSubmit').textContent = '+ Ð”Ð¾Ð´Ð°Ñ‚Ð¸ Ñ€Ð¾Ð·ÐºÐ»Ð°Ð´';
        document.getElementById('schedCancel').style.display = 'none';
    }
    
    function delSched(id){
        if(!confirm('Ð’Ð¸Ð´Ð°Ð»Ð¸Ñ‚Ð¸ Ñ€Ð¾Ð·ÐºÐ»Ð°Ð´?'))return;
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
        if(!confirm('Ð’Ð¸Ð´Ð°Ð»Ð¸Ñ‚Ð¸?'))return;
        fetch('/api/users/'+id,{method:'DELETE'}).then(function(r){return r.json();}).then(function(d){log(d.message,d.success);if(d.success)location.reload();});
    }
    
    function chgPass(id){
        var p=prompt('ÐÐ¾Ð²Ð¸Ð¹ Ð¿Ð°Ñ€Ð¾Ð»ÑŒ:');if(!p)return;
        fetch('/api/users/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})})
        .then(function(r){return r.json();}).then(function(d){log(d.message,d.success);});
    }
    
    function triggerGpio(type,id,mode){
        var btn=document.getElementById('btn-'+type+'-'+id);
        var txt=btn.innerHTML;
        btn.disabled=true;btn.innerHTML='...';
        fetch('/api/gpio/'+type+'/'+id).then(function(r){return r.json();}).then(function(d){
            log(d.message,d.success);
            if(mode==='toggle' && d.success){
                btn.innerHTML = d.state ? 'ON' : 'OFF';
                btn.className = d.state ? 'item-btn on' : 'item-btn';
                btn.disabled = false;
            } else {
                setTimeout(function(){btn.innerHTML=txt;btn.disabled=false;},2000);
            }
        });
    }
    
    function scanNetwork(){
        var btn=document.getElementById('scanBtn');
        var res=document.getElementById('scanResults');
        btn.disabled=true;btn.innerHTML='...';
        res.innerHTML='<div style="padding:10px;color:#666">ÐŸÐ¾ÑˆÑƒÐº...</div>';
        fetch('/api/scan').then(function(r){return r.json();}).then(function(data){
            btn.disabled=false;btn.innerHTML='Ð¡ÐºÐ°Ð½ÑƒÐ²Ð°Ñ‚Ð¸';
            if(data.length===0){
                res.innerHTML='<div style="padding:10px;color:#666">ÐÐµ Ð·Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾</div>';
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
        if(!input.files[0] || !confirm('Ð’Ñ–Ð´Ð½Ð¾Ð²Ð¸Ñ‚Ð¸?'))return;
        var reader=new FileReader();
        reader.onload=function(e){
            fetch('/api/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:e.target.result})
            .then(function(r){return r.json();}).then(function(d){log(d.message,d.success);if(d.success)setTimeout(function(){location.reload();},500);});
        };
        reader.readAsText(input.files[0]);
    }
    
    function resetAll(){
        if(!confirm('Ð’Ð¸Ð´Ð°Ð»Ð¸Ñ‚Ð¸ Ð’Ð¡Ð•?') || !confirm('Ð¢Ð¾Ñ‡Ð½Ð¾?'))return;
        fetch('/api/reset',{method:'POST'}).then(function(r){return r.json();}).then(function(d){log(d.message,d.success);if(d.success)location.reload();});
    }
    
    function checkUpdates(){
        var btn=document.getElementById('checkBtn');
        var info=document.getElementById('updateInfo');
        var installBtn=document.getElementById('installBtn');
        
        btn.disabled=true;btn.innerHTML='ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€ÐºÐ°...';
        info.innerHTML='<div style="color:#666">ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€ÐºÐ° Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½ÑŒ...</div>';
        installBtn.style.display='none';
        
        fetch('/api/update/check').then(function(r){return r.json();}).then(function(d){
            btn.disabled=false;btn.innerHTML='ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€Ð¸Ñ‚Ð¸ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ';
            
            if(!d.success){
                info.innerHTML='<div style="color:#f44">ÐŸÐ¾Ð¼Ð¸Ð»ÐºÐ°: '+d.error+'</div>';
                return;
            }
            
            var html='<div><b>ÐŸÐ¾Ñ‚Ð¾Ñ‡Ð½Ð° Ð²ÐµÑ€ÑÑ–Ñ:</b> '+d.current+'</div>';
            html+='<div style="margin-top:10px"><b>ÐžÑÑ‚Ð°Ð½Ð½Ñ Ð²ÐµÑ€ÑÑ–Ñ:</b> '+d.latest+'</div>';
            
            if(d.available){
                html+='<div style="margin-top:10px;color:#0f0"><b>âœ“ Ð”Ð¾ÑÑ‚ÑƒÐ¿Ð½Ðµ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ!</b></div>';
                if(d.changelog && d.changelog.length>0){
                    html+='<div style="margin-top:10px"><b>Ð—Ð¼Ñ–Ð½Ð¸:</b><ul style="margin:5px 0 0 20px">';
                    for(var i=0;i<d.changelog.length;i++){
                        html+='<li>'+d.changelog[i]+'</li>';
                    }
                    html+='</ul></div>';
                }
                installBtn.style.display='inline-block';
            } else {
                html+='<div style="margin-top:10px;color:#666">âœ“ Ð’Ð¸ Ð²Ð¸ÐºÐ¾Ñ€Ð¸ÑÑ‚Ð¾Ð²ÑƒÑ”Ñ‚Ðµ Ð¾ÑÑ‚Ð°Ð½Ð½ÑŽ Ð²ÐµÑ€ÑÑ–ÑŽ</div>';
            }
            
            info.innerHTML=html;
        }).catch(function(e){
            btn.disabled=false;btn.innerHTML='ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€Ð¸Ñ‚Ð¸ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ';
            info.innerHTML='<div style="color:#f44">ÐŸÐ¾Ð¼Ð¸Ð»ÐºÐ° Ð¿Ñ–Ð´ÐºÐ»ÑŽÑ‡ÐµÐ½Ð½Ñ</div>';
        });
    }
    
    function installUpdate(){
        if(!confirm('Ð’ÑÑ‚Ð°Ð½Ð¾Ð²Ð¸Ñ‚Ð¸ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ? Ð‘ÑƒÐ´Ðµ ÑÑ‚Ð²Ð¾Ñ€ÐµÐ½Ð¾ Ñ€ÐµÐ·ÐµÑ€Ð²Ð½Ñƒ ÐºÐ¾Ð¿Ñ–ÑŽ.'))return;
        
        var btn=document.getElementById('installBtn');
        var info=document.getElementById('updateInfo');
        
        btn.disabled=true;btn.innerHTML='Ð—Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶ÐµÐ½Ð½Ñ...';
        info.innerHTML='<div style="color:#666">Ð—Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶ÐµÐ½Ð½Ñ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ...</div>';
        
        fetch('/api/update/install',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
            if(d.success){
                info.innerHTML='<div style="color:#0f0">âœ“ '+d.message+'</div>';
                log(d.message,true);
                setTimeout(function(){
                    info.innerHTML+='<div style="margin-top:10px">ÐŸÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÐº ÑÐµÑ€Ð²Ñ–ÑÑƒ...</div>';
                    fetch('/api/restart',{method:'POST'}).then(function(){
                        setTimeout(function(){location.reload();},5000);
                    });
                },2000);
            } else {
                btn.disabled=false;btn.innerHTML='Ð’ÑÑ‚Ð°Ð½Ð¾Ð²Ð¸Ñ‚Ð¸ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ';
                info.innerHTML='<div style="color:#f44">âœ— '+d.message+'</div>';
                log(d.message,false);
            }
        }).catch(function(e){
            btn.disabled=false;btn.innerHTML='Ð’ÑÑ‚Ð°Ð½Ð¾Ð²Ð¸Ñ‚Ð¸ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ';
            info.innerHTML='<div style="color:#f44">ÐŸÐ¾Ð¼Ð¸Ð»ÐºÐ° Ð²ÑÑ‚Ð°Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ</div>';
        });
    }
    
    function restartService(){
        if(!confirm('ÐŸÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÑ‚Ð¸Ñ‚Ð¸ ÑÐµÑ€Ð²Ñ–Ñ?'))return;
        log('ÐŸÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÐº ÑÐµÑ€Ð²Ñ–ÑÑƒ...',true);
        fetch('/api/restart',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
            log(d.message,d.success);
            if(d.success){
                setTimeout(function(){location.reload();},3000);
            }
        });
    }
    
    function diagnoseUpdate(){
        var btn=document.getElementById('diagnoseBtn');
        var info=document.getElementById('diagInfo');
        
        btn.disabled=true;btn.innerHTML='ðŸ” Ð”Ñ–Ð°Ð³Ð½Ð¾ÑÑ‚Ð¸ÐºÐ°...';
        info.style.display='block';
        info.innerHTML='<div style="color:#666">ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€ÐºÐ° Ð·\'Ñ”Ð´Ð½Ð°Ð½Ð½Ñ...</div>';
        
        fetch('/api/update/diagnose').then(function(r){return r.json();}).then(function(d){
            btn.disabled=false;btn.innerHTML='ðŸ” Ð”Ñ–Ð°Ð³Ð½Ð¾ÑÑ‚Ð¸ÐºÐ°';
            
            var html='<div style="color:#0f0"><b>Ð”Ñ–Ð°Ð³Ð½Ð¾ÑÑ‚Ð¸ÐºÐ° ÑÐ¸ÑÑ‚ÐµÐ¼Ð¸ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ:</b></div><br>';
            html+='<table style="width:100%;border:none">';
            html+='<tr><td>ÐŸÐ¾Ñ‚Ð¾Ñ‡Ð½Ð° Ð²ÐµÑ€ÑÑ–Ñ:</td><td>'+d.current_version+'</td></tr>';
            html+='<tr><td>Ð ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ñ–Ð¹:</td><td>'+d.repo_url+'</td></tr>';
            html+='<tr><td>Ð†Ð½Ñ‚ÐµÑ€Ð½ÐµÑ‚:</td><td>'+(d.internet?'<span style="color:#0f0">âœ“ OK</span>':'<span style="color:#f44">âœ— ÐÐ•Ð¢</span>')+'</td></tr>';
            html+='<tr><td>DNS:</td><td>'+(d.dns?'<span style="color:#0f0">âœ“ OK</span>':'<span style="color:#f44">âœ— ÐÐ•Ð¢</span>')+'</td></tr>';
            html+='<tr><td>Ð ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ñ–Ð¹ Ð´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹:</td><td>'+(d.repo_reachable?'<span style="color:#0f0">âœ“ OK</span>':'<span style="color:#f44">âœ— ÐÐ•Ð¢</span>')+'</td></tr>';
            html+='<tr><td>version.json Ñ–ÑÐ½ÑƒÑ”:</td><td>'+(d.version_file_exists?'<span style="color:#0f0">âœ“ OK</span>':'<span style="color:#f44">âœ— ÐÐ•Ð¢</span>')+'</td></tr>';
            html+='<tr><td>SSL:</td><td>'+(d.ssl_ok?'<span style="color:#0f0">âœ“ OK</span>':'<span style="color:#f90">âš  Using unverified</span>')+'</td></tr>';
            html+='</table>';
            
            if(d.error){
                html+='<br><div style="color:#f44"><b>ÐŸÐ¾Ð¼Ð¸Ð»ÐºÐ°:</b> '+d.error+'</div>';
            }
            
            if(d.version_content){
                html+='<br><details style="margin-top:10px"><summary style="cursor:pointer;color:#888">version.json (Ð¿ÐµÑ€ÑˆÑ– 500 ÑÐ¸Ð¼Ð²Ð¾Ð»Ñ–Ð²)</summary>';
                html+='<pre style="margin-top:5px;padding:10px;background:#000;border-radius:4px;overflow-x:auto">'+d.version_content+'</pre></details>';
            }
            
            if(!d.repo_reachable || !d.version_file_exists){
                html+='<br><div style="padding:10px;background:#400;border-radius:4px;margin-top:10px">';
                html+='<b>âš ï¸ ÐŸÑ€Ð¾Ð±Ð»ÐµÐ¼Ð°:</b> Ð ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ñ–Ð¹ Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹.<br><br>';
                html+='<b>ÐœÐ¾Ð¶Ð»Ð¸Ð²Ñ– Ð¿Ñ€Ð¸Ñ‡Ð¸Ð½Ð¸:</b><br>';
                html+='1. Ð¤Ð°Ð¹Ð»Ð¸ Ñ‰Ðµ Ð½Ðµ Ð·Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶ÐµÐ½Ñ– Ð½Ð° '+d.repo_url+'<br>';
                html+='2. ÐÐµÐ¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¸Ð¹ URL Ñ€ÐµÐ¿Ð¾Ð·Ð¸Ñ‚Ð¾Ñ€Ñ–ÑŽ<br>';
                html+='3. Ð¡ÐµÑ€Ð²ÐµÑ€ Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹<br><br>';
                html+='<b>Ð Ñ–ÑˆÐµÐ½Ð½Ñ:</b><br>';
                html+='â€¢ Ð—Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶Ñ‚Ðµ Ñ„Ð°Ð¹Ð»Ð¸ Ð½Ð° ÑÐµÑ€Ð²ÐµÑ€<br>';
                html+='â€¢ ÐÐ±Ð¾ Ð²Ð¸ÐºÐ¾Ñ€Ð¸ÑÑ‚Ð°Ð¹Ñ‚Ðµ Ñ€ÑƒÑ‡Ð½Ðµ Ð¾Ð½Ð¾Ð²Ð»ÐµÐ½Ð½Ñ (Ð´Ð¸Ð². Ñ–Ð½ÑÑ‚Ñ€ÑƒÐºÑ†Ñ–ÑŽ Ð½Ð¸Ð¶Ñ‡Ðµ)<br>';
                html+='â€¢ ÐÐ±Ð¾ Ð²ÑÑ‚Ð°Ð½Ð¾Ð²Ñ–Ñ‚ÑŒ Ð·Ð¼Ñ–Ð½Ð½Ñƒ UPDATE_REPO_URL Ð´Ð»Ñ Ð»Ð¾ÐºÐ°Ð»ÑŒÐ½Ð¾Ð³Ð¾ Ñ‚ÐµÑÑ‚ÑƒÐ²Ð°Ð½Ð½Ñ';
                html+='</div>';
            }
            
            info.innerHTML=html;
        }).catch(function(e){
            btn.disabled=false;btn.innerHTML='ðŸ” Ð”Ñ–Ð°Ð³Ð½Ð¾ÑÑ‚Ð¸ÐºÐ°';
            info.innerHTML='<div style="color:#f44">ÐŸÐ¾Ð¼Ð¸Ð»ÐºÐ° Ð´Ñ–Ð°Ð³Ð½Ð¾ÑÑ‚Ð¸ÐºÐ¸: '+e+'</div>';
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

# ========== ROUTES ==========
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username')
        p = request.form.get('password')
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username=? AND password=?', (u, hashlib.sha256(p.encode()).hexdigest())).fetchone()
        conn.close()
        if user:
            session['logged_in'] = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['is_admin'] = bool(user['is_admin'])
            if request.form.get('remember'):
                session.permanent = True
                app.permanent_session_lifetime = timedelta(days=30)
            return redirect('/')
        return render_template_string(LOGIN_HTML, error='Wrong credentials')
    return render_template_string(LOGIN_HTML, error=None)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

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
        current_version=get_current_version(), repo_url=REPO_URL)

@app.route('/stream/<int:id>')
@login_required
def stream(id):
    return Response(generate_rtsp(id), mimetype='multipart/x-mixed-replace; boundary=frame')

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
    d = request.json
    conn = get_db()
    try:
        conn.execute('INSERT INTO users VALUES (NULL,?,?,?)', 
            (d['username'], hashlib.sha256(d['password'].encode()).hexdigest(), int(d.get('is_admin',0))))
        conn.commit()
        return jsonify({'success': True, 'message': 'OK'})
    except:
        return jsonify({'success': False, 'message': 'Exists'})
    finally:
        conn.close()

@app.route('/api/users/<int:id>', methods=['PUT', 'DELETE'])
@admin_required
def api_user(id):
    conn = get_db()
    if request.method == 'DELETE':
        u = conn.execute('SELECT username FROM users WHERE id=?', (id,)).fetchone()
        if u and u['username'] == 'admin':
            return jsonify({'success': False, 'message': 'Cannot delete admin'})
        conn.execute('DELETE FROM users WHERE id=?', (id,))
        conn.execute('DELETE FROM user_order WHERE user_id=?', (id,))
        conn.execute('DELETE FROM user_settings WHERE user_id=?', (id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Deleted'})
    d = request.json
    if 'password' in d:
        conn.execute('UPDATE users SET password=? WHERE id=?', (hashlib.sha256(d['password'].encode()).hexdigest(), id))
        conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'OK'})

@app.route('/api/gpio/<item_type>/<int:id>')
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
    
    if item['gpio_mode'] == 'toggle':
        ok, state = gpio_toggle(item['gpio_pin'])
        return jsonify({'success': ok, 'message': item['name'] + ': ' + ('ON' if state else 'OFF'), 'state': state})
    else:
        threading.Thread(target=lambda: gpio_pulse(item['gpio_pin'], item['gpio_duration']), daemon=True).start()
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
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        diag['internet'] = True
    except:
        pass
    
    # Check DNS
    try:
        socket.gethostbyname('google.com')
        diag['dns'] = True
    except:
        pass
    
    # Check if repo is reachable
    try:
        import ssl
        context = ssl._create_unverified_context()
        req = urllib.request.Request(f"{REPO_URL}/version.json")
        with urllib.request.urlopen(req, timeout=5, context=context) as response:
            diag['repo_reachable'] = True
            diag['ssl_ok'] = True
            if response.code == 200:
                diag['version_file_exists'] = True
                data = response.read().decode()
                diag['version_content'] = data[:500]  # First 500 chars
    except urllib.error.HTTPError as e:
        diag['error'] = f'HTTP {e.code}: {e.reason}'
    except urllib.error.URLError as e:
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
    """Download and install update"""
    success, result = download_update()
    if not success:
        return jsonify({'success': False, 'message': result})
    
    temp_file = result
    success, message = apply_update(temp_file)
    return jsonify({'success': success, 'message': message})

@app.route('/api/restart', methods=['POST'])
@admin_required
def api_restart():
    """Restart the service"""
    try:
        subprocess.Popen(['systemctl', 'restart', 'camera-control'])
        return jsonify({'success': True, 'message': 'Restarting...'})
    except:
        return jsonify({'success': False, 'message': 'Cannot restart service'})

if __name__ == '__main__':
    print("=" * 40)
    print("Camera Control v4.4.1")
    print("=" * 40)
    init_db()
    print("GPIO:   " + ("YES" if GPIO_AVAILABLE else "NO"))
    print("FFmpeg: " + ("YES" if FFMPEG_AVAILABLE else "NO"))
    print("Version: " + get_current_version())
    
    # Start scheduler
    start_scheduler()
    print("Scheduler: STARTED")
    
    print("=" * 40)
    print("http://<IP>:8080")
    print("admin / admin")
    print("=" * 40)
    app.run(host='0.0.0.0', port=8080, threaded=True)