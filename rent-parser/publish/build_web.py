"""Веб-версия карты для GitHub Pages: map_yandex.html без чужих телефонов и с запретом индексации.

  python3 rent-parser/publish/build_web.py <папка сайта>   → <папка>/index.html

Ключ Яндекс Карт: если в rent-parser/local_keys.json есть "yandex_maps_js_web" (ключ с ограничением
по HTTP Referer на <логин>.github.io) — подставляется он, иначе остаётся локальный "yandex_maps_js".
"""
import json
import re
import sys
from pathlib import Path

RP = Path(__file__).resolve().parents[1]
SRC = RP.parent / "map_yandex.html"
KEYS = RP / "local_keys.json"

# +7 900 123-45-67, 89001234567, 8 (900) 123 45 67 … → +7 900 ***-**-67
# (в JSON перевод строки — «\n», поэтому «n» перед номером не считаем буквой); «Тел:9126072933» — тоже номер
PHONE = re.compile(r"(?:(?<=\\n)|(?<![\w+]))(?:(?:\+?7|8)[\s(\-]*)?(9\d\d)[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*(\d{2})(?!\d)")


def main():
    out_dir = Path(sys.argv[1])
    html = SRC.read_text(encoding="utf-8")
    html, n = PHONE.subn(lambda m: f"+7 {m.group(1)} ***-**-{m.group(2)}", html)
    try:
        keys = json.loads(KEYS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        keys = {}
    if keys.get("yandex_maps_js_web") and keys.get("yandex_maps_js"):
        html = html.replace(f"apikey={keys['yandex_maps_js']}", f"apikey={keys['yandex_maps_js_web']}")
    html = html.replace("<head>", '<head>\n<meta name="robots" content="noindex, nofollow">', 1)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    left = len(re.findall(r"(?<![\d*])9\d\d[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)", html))
    print(f"🌐 веб-версия: телефонов скрыто {n}, осталось {left}; "
          f"ключ {'веб' if keys.get('yandex_maps_js_web') else 'локальный'}; {len(html) // 1024} КБ")


if __name__ == "__main__":
    main()
