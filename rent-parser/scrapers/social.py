"""Сборщик постов Telegram и ВКонтакте (серый список). Чистый Python, без браузера.

Собираем все посты (фильтр по зонам — в просмотрщике), но для каждого отмечаем совпадения
с улицами зон (поле hits), вытаскиваем цену, адрес, дату поста и ссылку на Драже.
Режимы: full — глубоко назад; inc — пока на странице есть новые посты.
"""
import html
import random
import re
import time
import urllib.request

from config import UA, ZONE_KEYWORDS

# m.vk.com без входа отдаёт посты в HTML только поисковым роботам; браузерному UA — пустую JS-страницу.
VK_UA = "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)"

TG_CHANNELS = ["ekatarenda"]
VK_GROUPS = [
    # (группа, доп. запросы-теги; стена листается через ?offset=)
    ("ekb_arenda", ["?q=%23uktus%40ekb_arenda", "?q=%23vtuz%40ekb_arenda",
                    "?q=%23kom%40ekb_arenda", "?q=%23center%40ekb_arenda"]),
    ("rent_flats", []),
    ("podselenieeburg", []),
]
PAGES = {"full": {"tg": 40, "vk": 15}, "inc": {"tg": 8, "vk": 3}}
# Втузгородок у старого главного корпуса — не подходит (метка, а не фильтр)
FAR_ADDR = r"\b(?:мира|комсомольская|студенческая|библиотечная)\b"


def fetch_url(url, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "ru-RU,ru"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def post_text(fragment):
    t = html.unescape(re.sub(r"<br\s*/?>", "\n", fragment))
    return re.sub(r"<[^>]+>", "", t).strip()


def text_price(text):
    """Цена из текста: сначала по меткам «Стоимость/Цена/Аренда», потом любое «NN NNN руб»."""
    num = r"(\d{2}[\s .]?\d{3}|\d{2}\s?(?:т\.?р|тыс|к\b))"
    for pat in (r"(?:стоимость|цена|аренда|условия аренды)[^\d\n]{0,25}" + num,
                num + r"\s*(?:₽|р\b|р\.|руб)"):
        m = re.search(pat, text, re.I)
        if m:
            raw = m.group(1).lower()
            digits = re.sub(r"\D", "", raw)
            if re.search(r"т\.?р|тыс|к", raw):
                return int(digits) * 1000
            return int(digits)
    return None


def text_money(text, words):
    """Залог/депозит из текста: 0 при «без залога», сумма, или None."""
    low = text.lower()
    if re.search(r"без\s+(?:" + words + ")", low):
        return 0
    m = re.search(r"(?:" + words + r")[^\d\n]{0,25}(\d{1,3}(?:[\s .]?\d{3})+|\d{4,6})", low)
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def post_where(text):
    """Где квартира: ПЕРВАЯ строка «Адрес:» (+ первая «Район:»). Дальше в посте бывают адреса
    других объектов / офиса (Орловская 77 … «Адрес: Малышева 109/А»). Без «Адрес:» — шапка поста."""
    low = text.lower()
    addr = re.findall(r"адрес[:\s]+([^\n]{3,80})", low)
    rayon = re.findall(r"(?:^|\n)\s*(?:микро)?район[:\s]+([^\n]{3,80})", low)
    if addr:
        return " ".join(addr[:1] + rayon[:1])
    return " ".join(low.split("\n")[:3] + rayon[:1])


def post_record(src, pid, link, dt, text, extra):
    low = text.lower()
    where = post_where(text)
    hits = [label for label, rx in ZONE_KEYWORDS if re.search(rx, where)]
    addr = re.search(r"адрес[:\s]+([^\n]{3,80})", text, re.I)
    price = text_price(text)
    first = next((l for l in text.split("\n") if l.strip()), "")[:90]
    return {
        "id": pid,
        "source": src,
        "source_type": "social",
        "url": link,
        "title": first,
        "category": "пост",
        "price": price,
        "deposit": text_money(text, "залога|залог|депозит"),
        "commission_pct": 0 if re.search(r"без\s+комисси", low) else None,
        "address": addr.group(1).strip() if addr else "",
        "published": dt,
        "description": text,
        "hits": ", ".join(hits),
        "badges": "⚠️ старый Втузгородок" if re.search(FAR_ADDR, where) else "",
        "raw_data": dict(extra, text=text, date=dt),
    }


def scrape_telegram(mode="inc", known_ids=frozenset(), log=print):
    found = {}
    for ch in TG_CHANNELS:
        url, seen = f"https://t.me/s/{ch}", 0
        for _ in range(PAGES[mode]["tg"]):
            try:
                page = fetch_url(url)
            except Exception as ex:  # noqa: BLE001
                log(f"[Telegram] {ch}: ошибка {ex}")
                break
            ids, new_here = [], 0
            for block in page.split('class="tgme_widget_message_wrap')[1:]:
                post = re.search(r'data-post="([^"]+)"', block)
                text = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
                dt = re.search(r'<time datetime="([^"]+)"', block)
                if not post:
                    continue
                ids.append(int(post.group(1).rsplit("/", 1)[1]))
                seen += 1
                if not text:
                    continue  # фото без подписи
                pid = "TG_" + post.group(1).replace("/", "_")
                new_here += pid not in known_ids and pid not in found
                found[pid] = post_record(f"Telegram @{ch}", pid, f"https://t.me/{post.group(1)}",
                                         dt.group(1)[:16].replace("T", " ") if dt else "",
                                         post_text(text.group(1)), {"channel": ch})
            if not ids or (mode == "inc" and new_here == 0):
                break
            url = f"https://t.me/s/{ch}?before={min(ids)}"
            time.sleep(random.uniform(1, 2.5))
        log(f"[Telegram] {ch}: просмотрено {seen}, с текстом {len(found)}")
    return list(found.values())


def scrape_vk(mode="inc", known_ids=frozenset(), log=print):
    found = {}
    for group, tags in VK_GROUPS:
        wall = [f"?offset={10 * i}" if i else "" for i in range(PAGES[mode]["vk"])]
        stop_wall = False
        for q in wall + tags:
            if stop_wall and q in wall:
                continue
            try:
                page = fetch_url(f"https://m.vk.com/{group}{q}", ua=VK_UA)
            except Exception as ex:  # noqa: BLE001
                log(f"[ВК] {group}{q}: ошибка {ex}")
                continue
            new_here = 0
            for block in page.split('class="wall_item')[1:]:
                pid = re.search(r'data-post-id="(-?\d+_\d+)"', block)
                text = re.search(r'class="pi_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
                if not (pid and text):
                    continue
                dt = re.search(r'class="wi_date"[^>]*>([^<]+)<', block)
                dragee = re.search(r"arenda\.dragee\.ru%2Foffer%2F(\d+)", block)
                rid = f"VK_{pid.group(1)}"
                new_here += rid not in known_ids and rid not in found
                found[rid] = post_record(
                    f"ВК {group}", rid, f"https://vk.com/wall{pid.group(1)}",
                    dt.group(1).strip() if dt else "", post_text(text.group(1)),
                    {"group": group, "dragee": f"https://arenda.dragee.ru/offer/{dragee.group(1)}" if dragee else ""})
            time.sleep(random.uniform(1.5, 3))
            if mode == "inc" and q in wall and new_here == 0:
                stop_wall = True  # стена дальше уже известна; теги всё равно смотрим
        log(f"[ВК] {group}: всего постов {sum(1 for r in found.values() if r['raw_data']['group'] == group)}")
    return list(found.values())
