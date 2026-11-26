#!/usr/bin/env python3
"""
Генерує version.json для публікації оновлення Camera Control.

Використання:
    python3 make_version.py camera_control.py "Опис зміни 1" "Опис зміни 2"

Версія береться з CURRENT_VERSION у самому файлі.
Результат: version.json + camera_control.txt поруч - обидва заливаєш
у теку camera-control на хостингу.

Перед публікацією перевіряє, що JavaScript у шаблонах сторінок валідний:
одна синтаксична помилка в <script> вимикає всі кнопки на сторінці.
"""

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO_URL = "https://tehnodron.in.ua/camera-control"
PUBLISH_NAME = "camera_control.txt"
LETTERS = "A-Za-zА-Яа-яІіЇїЄєҐґ"


def html_templates(text):
    """Значення рядків *_HTML / HTML такими, як їх побачить браузер."""
    out = {}
    for node in ast.parse(text).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.endswith("HTML")
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def check_scripts(text):
    problems = []
    for name, html in html_templates(text).items():
        for n, script in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S), 1):
            # 1. Найчастіша пастка: апостроф між літерами всередині JS-рядка
            #    ('з\'єднання' у Python-рядку стає 'з'єднання' у браузері)
            for i, line in enumerate(script.splitlines(), 1):
                code = re.sub(r"(^|[\s;{}])//.*$", r"\1", line)   # без коментарів
                if re.search(rf"[{LETTERS}]'[{LETTERS}]", code):
                    problems.append(f"{name}: апостроф у JS-рядку: {line.strip()[:90]}")
            # 2. Повна перевірка синтаксису, якщо є Node.js
            if shutil.which("node"):
                js = re.sub(r"\{%.*?%\}", "", script)            # теги Jinja
                js = re.sub(r"\{\{.*?\}\}", "0", js)               # вирази Jinja
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                    f.write(js)
                r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
                Path(f.name).unlink()
                if r.returncode:
                    err = next((l for l in r.stderr.splitlines() if "Error" in l), r.stderr[:200])
                    problems.append(f"{name}: синтаксична помилка JS: {err}")
    return problems


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    src = Path(sys.argv[1])
    changelog = sys.argv[2:]

    data = src.read_bytes()
    text = data.decode("utf-8")

    m = re.search(r'^CURRENT_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        print("✗ Не знайдено CURRENT_VERSION у файлі")
        sys.exit(1)
    version = m.group(1)

    if not text.startswith("#!"):
        print("✗ Файл має починатись з #! - інакше програма відхилить оновлення")
        sys.exit(1)

    compile(text, str(src), "exec")  # впаде тут, якщо синтаксис Python битий

    problems = check_scripts(text)
    if problems:
        print("✗ JavaScript у сторінках зламаний - реліз НЕ створено:")
        for p in problems:
            print("   -", p)
        print("  Порада: в українських словах у JS використовуйте ’ замість '")
        sys.exit(1)
    print("✓ JavaScript у шаблонах валідний" + ("" if shutil.which("node") else " (базова перевірка, Node.js не знайдено)"))

    out_dir = src.parent
    shutil.copyfile(src, out_dir / PUBLISH_NAME)

    manifest = {
        "version": version,
        "date": date.today().isoformat(),
        "changelog": changelog,
        "files": {
            "main": {
                "url": f"{REPO_URL}/{PUBLISH_NAME}",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        },
    }

    (out_dir / "version.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"✓ Версія:  {version}")
    print(f"✓ SHA-256: {manifest['files']['main']['sha256']}")
    print(f"✓ Створено: version.json, {PUBLISH_NAME}")
    print()
    print(f"Залий обидва файли в {REPO_URL}/")


if __name__ == "__main__":
    main()
