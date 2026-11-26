# ⚡ ШВИДКИЙ СТАРТ - Що робити далі

## 📦 Крок 1: Завантажте файли на сервер

Завантажте ці файли в папку `camera-control/` на tehnodron.in.ua:

### Підготовка релізу

```bash
python3 make_version.py camera_control.py "що змінилось"
```

Скрипт перевірить JavaScript сторінки й створить `version.json` і `camera_control.txt`
з контрольною сумою SHA-256. Без правильної суми пристрої оновлення не приймуть.

### Файли які потрібні обов'язково:

1. **camera_control.txt** (створює make_version.py)
2. **install.sh** → перейменувати в **install.txt**
3. **update.sh** → перейменувати в **update.txt**
4. **version.json** (створює make_version.py)
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

Має показати JSON з поточною версією і полем `sha256`

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

# Оновити поточну систему (з перевіркою SHA-256 і відкатом)
wget https://tehnodron.in.ua/camera-control/update.txt -O update.sh && sudo bash update.sh
```

---

## 📋 Фінальна структура на сервері:

```
tehnodron.in.ua/www/camera-control/
├── .htaccess                  ← створити
├── camera_control.txt         ← перейменований .py
├── install.txt                ← перейменований .sh
├── update.txt                 ← перейменований .sh
├── version.json               ← без змін
└── (документація .md)         ← опціонально
```

---

## 🎯 Команди для нової RNPi:

Після розгортання на сервері, на будь-якій новій RNPi просто виконайте:

```bash
wget https://tehnodron.in.ua/camera-control/install.txt -O install.sh && sudo bash install.sh
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
