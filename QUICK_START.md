# ⚡ ШВИДКИЙ СТАРТ - Що робити далі

## 📦 Крок 1: Завантажте файли на сервер

Завантажте ці файли в папку `camera-control/` на tehnodron.in.ua:

### Файли які потрібні обов'язково:

1. **camera-control.py** → перейменувати в **camera-control.txt**
2. **install.sh** → перейменувати в **install.txt**
3. **update.sh** → перейменувати в **update.txt**
4. **version.json** → залишити як є
5. **Створити .htaccess** (див. нижче)

### Файли документації (необов'язкові):
- README.md
- CHANGELOG.md
- SCHEDULER_GUIDE.md
- UPDATE_TROUBLESHOOTING.md

---

## 🔧 Крок 2: Створіть .htaccess

У папці `camera-control/` створіть файл `.htaccess`:

```apache
# Camera Control System - static file delivery
<FilesMatch "\.py$">
    SetHandler None
    ForceType application/octet-stream
    Header set Content-Disposition attachment
</FilesMatch>
<FilesMatch "\.sh$">
    SetHandler None
    ForceType application/x-sh
    Header set Content-Disposition attachment
</FilesMatch>
<FilesMatch "\.txt$">
    ForceType text/plain
    Header set Content-Type "text/plain; charset=utf-8"
</FilesMatch>
<FilesMatch "\.json$">
    ForceType application/json
    Header set Content-Type "application/json; charset=utf-8"
</FilesMatch>
<Files "*">
    Require all granted
</Files>
Options -Indexes
```

---

## ✅ Крок 3: Перевірте

Відкрийте в браузері:
```
https://tehnodron.in.ua/camera-control/version.json
```

Має показати JSON з версією 4.4.1

---

## 🧹 Крок 4: Очистіть Cloudflare кеш

1. Зайдіть в Cloudflare Dashboard
2. Виберіть домен tehnodron.in.ua
3. Caching → Purge Cache → Custom Purge
4. URL: `https://tehnodron.in.ua/camera-control/*`
5. Purge

---

## 🚀 Крок 5: Тест на RNPi

На вашій поточній RNPi або новій:

```bash
# Перевірити доступність
curl -I https://tehnodron.in.ua/camera-control/version.json

# Має показати: HTTP/2 200

# Оновити поточну систему
cd ~/gate-control
curl -o camera_control.py https://tehnodron.in.ua/camera-control/camera-control.txt
sudo systemctl restart <ваш-сервіс>
```

---

## 📋 Фінальна структура на сервері:

```
tehnodron.in.ua/www/camera-control/
├── .htaccess                  ← створити
├── camera-control.txt         ← перейменований .py
├── install.txt                ← перейменований .sh
├── update.txt                 ← перейменований .sh
├── version.json               ← без змін
└── (документація .md)         ← опціонально
```

---

## 🎯 Команди для нової RNPi:

Після розгортання на сервері, на будь-якій новій RNPi просто виконайте:

```bash
curl -sSL https://tehnodron.in.ua/camera-control/install.txt | sudo bash
```

Готово! 🎉

---

## ⚠️ Важливо:

- ✅ Файли ОБОВ'ЯЗКОВО перейменувати в .txt
- ✅ .htaccess ОБОВ'ЯЗКОВО створити
- ✅ Очистити кеш Cloudflare після завантаження
- ✅ Перевірити через curl перед використанням

---

**Детальні інструкції:** DEPLOY_TO_SERVER.md  
**Вирішення проблем:** UPDATE_TROUBLESHOOTING.md  
**Про scheduler:** SCHEDULER_GUIDE.md
