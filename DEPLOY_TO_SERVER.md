# 📤 Інструкція з розгортання на сервері tehnodron.in.ua

## 📋 Список файлів для завантаження

Завантажте ці файли в папку `public_html/camera-control/`:

```
camera-control/
├── .htaccess              (обов'язково!)
├── camera-control.txt     (85KB) - основна програма
├── version.json           (812B) - інформація про версію
├── install.txt            (2.4KB) - скрипт встановлення
└── update.txt             (3.3KB) - скрипт оновлення
```

---

## 🔧 Крок 1: Створіть .htaccess

У папці `camera-control/` створіть файл `.htaccess` з таким вмістом:

```apache
# Camera Control System - static file delivery
# Не виконувати файли, тільки віддавати для завантаження

# Python файли
<FilesMatch "\.py$">
    SetHandler None
    ForceType application/octet-stream
    Header set Content-Disposition attachment
</FilesMatch>

# Shell скрипти (якщо будуть .sh)
<FilesMatch "\.sh$">
    SetHandler None
    ForceType application/x-sh
    Header set Content-Disposition attachment
</FilesMatch>

# Text файли (.txt) - віддавати як plain text
<FilesMatch "\.txt$">
    ForceType text/plain
    Header set Content-Type "text/plain; charset=utf-8"
</FilesMatch>

# JSON файли
<FilesMatch "\.json$">
    ForceType application/json
    Header set Content-Type "application/json; charset=utf-8"
</FilesMatch>

# Дозволити доступ до всіх файлів
<Files "*">
    Require all granted
</Files>

# Заборонити перегляд списку файлів
Options -Indexes
```

---

## 📂 Крок 2: Перейменуйте файли

**ВАЖЛИВО:** На сервері файли мають мати розширення `.txt`!

```
camera-control.py  →  camera-control.txt
install.sh         →  install.txt
update.sh          →  update.txt
```

**Чому .txt?** Щоб хостинг не намагався виконати їх як CGI скрипти.

---

## ✅ Крок 3: Перевірте структуру

Після завантаження структура має бути:

```
/home/jg441090/tehnodron.in.ua/www/camera-control/
├── .htaccess
├── camera-control.txt
├── install.txt
├── update.txt
└── version.json
```

---

## 🧪 Крок 4: Перевірте доступність

### Через браузер:

Відкрийте ці URL:
- https://tehnodron.in.ua/camera-control/version.json
- https://tehnodron.in.ua/camera-control/camera-control.txt
- https://tehnodron.in.ua/camera-control/install.txt

**Має показати вміст файлів, НЕ помилку 500!**

### Через командний рядок:

```bash
# Перевірити HTTP статус (має бути 200)
curl -I https://tehnodron.in.ua/camera-control/version.json
curl -I https://tehnodron.in.ua/camera-control/camera-control.txt

# Завантажити version.json
curl https://tehnodron.in.ua/camera-control/version.json

# Перевірити розмір camera-control.txt
curl -sI https://tehnodron.in.ua/camera-control/camera-control.txt | grep -i content-length

# Має показати: Content-Length: 85000 (приблизно)
```

---

## 🚀 Крок 5: Тестування встановлення

На тестовій RNPi або VM:

```bash
# Спробувати встановити
curl -sSL https://tehnodron.in.ua/camera-control/install.txt | sudo bash

# Має виконатись без помилок і показати:
# ✓ Installation completed!
# Web interface: http://<IP>:8080
```

---

## 🔒 Права доступу на файлах

Переконайтесь що файли мають правильні права:

```bash
# На сервері:
cd ~/public_html/camera-control

# Встановити права
chmod 644 *.txt *.json
chmod 644 .htaccess

# Перевірити власника (має бути ваш користувач)
ls -la
```

---

## 🌐 Cloudflare налаштування

Якщо використовуєте Cloudflare:

### 1. Очистити кеш після завантаження:
- Dashboard → Caching → Purge Cache
- Custom Purge → `https://tehnodron.in.ua/camera-control/*`

### 2. Або додати Page Rule (необов'язково):
- URL: `tehnodron.in.ua/camera-control/*`
- Settings: Cache Level = Bypass

---

## ❓ Troubleshooting

### Проблема: HTTP 500 при доступі до файлів

**Рішення:**
1. Перевірте що `.htaccess` створений
2. Перевірте права на файли (644)
3. Перевірте логи хостингу: `tail -f ~/logs/error.log`
4. Спробуйте без Cloudflare (через реальний IP сервера)

### Проблема: Завантажується HTML замість файлу

**Рішення:**
1. Очистити кеш Cloudflare
2. Додати `?v=123` до URL для обходу кешу
3. Перевірити що файл існує на сервері

### Проблема: "End of script output before headers"

**Рішення:**
1. `.htaccess` не працює
2. Спробуйте додати в кореневий `.htaccess`:
   ```apache
   RemoveHandler .txt
   AddType text/plain .txt
   ```

---

## 📊 Очікувані розміри файлів:

```
camera-control.txt    ~85 KB   (основна програма v4.4.1)
version.json          ~800 B   (метадані)
install.txt           ~2.4 KB  (скрипт встановлення)
update.txt            ~3.3 KB  (скрипт оновлення)
.htaccess             ~500 B   (конфігурація Apache)
```

---

## ✅ Checklist перед використанням:

- [ ] Всі файли завантажені на сервер
- [ ] Файли перейменовані в .txt
- [ ] .htaccess створений
- [ ] Права доступу 644
- [ ] version.json відкривається в браузері
- [ ] camera-control.txt завантажується (не 500 помилка)
- [ ] Кеш Cloudflare очищений
- [ ] Тестове встановлення на RNPi пройшло успішно

---

## 🎯 Готово!

Тепер можна використовувати команди:

**Встановлення на новій RNPi:**
```bash
curl -sSL https://tehnodron.in.ua/camera-control/install.txt | sudo bash
```

**Оновлення існуючої системи:**
```bash
curl -sSL https://tehnodron.in.ua/camera-control/update.txt | sudo bash
```

**Або через веб-інтерфейс:**
- Налаштування → Оновлення → Перевірити оновлення

---

**Camera Control System v4.4.1**  
*Готовий до розгортання!* 🚀
