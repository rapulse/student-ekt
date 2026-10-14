"""Экспорт базы rent-parser в просмотрщик map_all.html (карта + список + фильтры).

Статусы и заметки — из выгрузки отметок ~/Downloads/nvk-marks*.json (кнопка «Выгрузить отметки»)
и из базы старого monitor/ (_tmp/data/*.csv, общий ID). Сами отметки хранятся в браузере.
Реальная стоимость считается как в monitor/: (аренда + КУ) + комиссия / HORIZON_MONTHS, на человека.
"""
import csv
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import (DATA, HORIZON_MONTHS, KU_ESTIMATE, NVK, PAIR_MAX, POLYGON, ROOT, RP, SOCIAL_ENABLED, SOLO_MAX,
                    SOLO_TARGET, ZONE_TRAVEL, km, zone_of)
import districts
from dedup import find_duplicates, find_post_duplicates
from to_parquet import DEFAULT_PARQUET, load_parquet

OUT = ROOT / "map_all.html"              # карта OpenStreetMap (Leaflet)
OUT_YANDEX = ROOT / "map_yandex.html"     # та же страница на Яндекс Картах (нужен ключ JS API)
TEMPLATE = RP / "viewer_template.html"
KEYS = RP / "local_keys.json"             # {"yandex_maps_js": "…"} — не в git
LEAFLET_HEAD = ('<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">\n'
                '<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>\n'
                '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.min.css">\n'
                '<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.js"></script>')
METER_ESTIMATE = 1_500          # «ЖКУ N ₽ + счётчики»: счётчики сверху — оценка
MARK_STATUS = {"like": "Интересно", "no": "Отказ"}
# «25000 + квартплата (лето 3000, зима 6000)», «+ КУ 4 т.р.» — коммуналка сверху по тексту объявления
KU_PLUS_RX = re.compile(r"\+\s*(?:квар?т\.?\s*плат\w*|ку\b|к\.\s*у\.?|жку\b|коммунал\w*|коммуналк\w*)", re.I)
KU_STOP_RX = re.compile(r"залог|депозит|комисс|предоплат|страхов", re.I)
KU_NUM_RX = re.compile(r"(\d+(?:[.,]\d)?)\s*(?:т\.?\s?р|тыс|к\b)|(?<![\d.,])(\d{1,2})[\s\u00a0.]?(\d{3})(?![\d])", re.I)


def ku_from_text(text):
    """(есть ли «+ КУ» в тексте, сумма ₽ или None, фрагмент). Сумма — наибольшая из «лето/зима».
    «+ КУ по счётчикам» — это только счётчики: ("meters", None, фрагмент)."""
    m = KU_PLUS_RX.search(text or "")
    if not m:
        return False, None, ""
    win = text[m.end():m.end() + 90]
    stop = KU_STOP_RX.search(win)
    win = win[:stop.start()] if stop else win
    if re.match(r"\s*(?:\(?\s*)?(?:по\s+)?(?:сч[её]тчик|свет|электр|вод)", win, re.I):
        return "meters", None, ""
    nums, end = [], None
    for x in KU_NUM_RX.finditer(win):
        k, a, b = x.groups()
        v = float(k.replace(",", ".")) * 1000 if k else int(a + b)
        if 500 <= v <= 15_000:
            nums.append(int(v))
            end = x.end()
    if end is None:   # цитата — до конца фразы
        cut = re.search(r"[.;!\n]", win)
        end = cut.start() if cut else len(win)
    tail = re.match(r"\s*(?:руб\w*\.?|р\.|₽)?\s*\)?", win[end:])
    frag = re.sub(r"\s+", " ", text[m.start():m.end() + end + len(tail.group(0))]).strip()[:80]
    return True, (max(nums) if nums else None), frag


def _v(x):
    if x is None or x == "":
        return None
    try:
        return None if pd.isna(x) else x
    except (TypeError, ValueError):
        return x


def _int(x):
    x = _v(x)
    return int(round(float(x))) if x is not None else None


def load_statuses():
    """{id: (статус, заметка)} из CSV monitor/ + отметки из «Загрузок» (свежее важнее)."""
    st = {}
    for name in ("listings.csv", "social.csv"):
        p = ROOT / "_tmp" / "data" / name      # база старого monitor/ (в архиве)
        if p.exists():
            with p.open(encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    st[r["ID"]] = (r.get("Статус") or "", r.get("Контакты и заметки") or "")
    files = sorted((Path.home() / "Downloads").glob("nvk-marks*.json"), key=lambda p: p.stat().st_mtime)
    if files:
        try:
            marks = json.loads(files[-1].read_text(encoding="utf-8")).get("marks", {})
        except (OSError, ValueError):
            marks = {}
        for rid, m in marks.items():
            s, n = st.get(rid, ("", ""))
            st[rid] = (MARK_STATUS.get(m.get("mark"), s), (m.get("note") or n or "").strip())
    return st


def costs(r):
    price = _int(r.get("price")) or 0
    cat = r.get("category") or ""
    kind = "room" if cat == "Комната" else "solo" if cat in ("Студия", "1к") or _int(r.get("rooms")) in (0, 1) else "pair"
    share = 2 if kind == "pair" else 1
    info = (r.get("utilities_info") or "").lower()
    util_inc, util_rub = _v(r.get("utilities_included")), _int(r.get("utilities_rub"))
    # «ЖКУ включены без счетчиков» (Авито) = квартплата в цене, счётчики — отдельно
    meters_extra = bool(re.search(r"\+\s*сч[её]тчик|сч[её]тчики:\s*оплач|без\s+сч[её]тчик", info))
    plus, text_ku, frag = ku_from_text(r.get("description"))
    if plus == "meters":
        plus, meters_extra = False, True
    if util_inc is True and plus:
        ku = text_ku or KU_ESTIMATE[kind]
        ku_note = (f"❓ КУ: в форме «включены», в тексте «{frag}» — уточнить, считаем "
                   + (f"{ku:,} ₽".replace(",", " ") if text_ku else f"~{ku // 1000}к (оценка)"))
    elif util_inc is True and meters_extra:
        ku, ku_note = METER_ESTIMATE, "⚠️ КУ включены, счётчики ~1.5к (оценка)"
    elif util_inc is True:
        ku, ku_note = 0, "✅ КУ включены"
    elif util_rub is not None:
        ku = util_rub + (METER_ESTIMATE if meters_extra else 0)
        ku_note = f"⚠️ ЖКУ {util_rub:,} ₽".replace(",", " ") + (" + счётчики ~1.5к (оценка)" if meters_extra else "")
    elif text_ku:
        ku, ku_note = text_ku, f"⚠️ КУ {text_ku:,} ₽ (из текста)".replace(",", " ")
    else:
        ku = KU_ESTIMATE[kind]
        ku_note = f"⚠️ КУ ~{ku // 1000}к (оценка)"
    pct, rub = _v(r.get("commission_pct")), _int(r.get("commission_rub"))
    if rub is None and pct is not None and price:
        rub = round(price * float(pct) / 100)
    dep = _int(r.get("deposit"))
    rent_default = dep is None and r.get("source") == "Яндекс Аренда"
    if rent_default:
        dep = 0  # Яндекс не прислал залог: по условиям сервиса его нет (явный залог показываем как есть)
    src_txt = " (из текста)" if r.get("source") == "Домклик" else ""
    terms = [ku_note,
             ("✅ без залога (условия Аренды)" if rent_default else "✅ без залога") if dep == 0 else (f"⚠️ залог {dep:,} ₽".replace(",", " ") + src_txt if dep else "❓ залог — уточнить"),
             "✅ без комиссии" if rub == 0 else (
                 (f"❌ комиссия {int(pct)}% ({rub:,} ₽)" if pct else f"❌ комиссия {rub:,} ₽").replace(",", " ") + src_txt
                 if rub else "❓ комиссия — уточнить")]
    if r.get("source_type") == "arenda_program" and dep:
        terms.append("❗ у Яндекс Аренды обычно без залога — уточнить")
    total = price + ku
    real = round(total + (rub or 0) / HORIZON_MONTHS)
    pp = round(real / share)
    unknown = rub is None or dep is None
    entry = price + (dep or 0) + (rub or 0)
    if share == 2:
        budget = "OK" if total <= PAIR_MAX else "дорого"
    else:
        budget = "цель" if pp <= SOLO_TARGET else ("OK" if pp <= SOLO_MAX else "дорого")
    return {
        "price": price, "total": total, "share": share, "pp": pp, "kind": kind,
        "entry": f"{entry:,}".replace(",", " ") + ("+?" if unknown else ""),
        "entryPP": f"{round(entry / share):,}".replace(",", " ") + ("+?" if unknown else ""),
        "terms": " · ".join(terms), "budget": budget,
        "dep": dep, "comm": rub, "ku": ku, "kuNote": ku_note, "rentDefault": rent_default,
    }


def _load_extra(name):
    """Данные отдельных скриптов: transit.json (время в пути), smart.json (умный фильтр).
    Нет файла или битый — пусто, карта как раньше."""
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def export_to_viewer(path=DEFAULT_PARQUET, out=OUT, log=print):
    df = load_parquet(path)
    if df.empty:
        log("База пуста — нечего показывать")
        return None
    rows = [{k: _v(v) for k, v in r.items()} for r in df.to_dict("records")]
    statuses = load_statuses()
    flats = [r for r in rows if r.get("source_type") != "social"]
    posts = [r for r in rows if r.get("source_type") == "social"] if SOCIAL_ENABLED else []
    dup = find_duplicates([r for r in flats if r.get("is_active") is not False])
    pdup = find_post_duplicates(posts)
    by_id = {r["id"]: r for r in rows}
    groups = {}
    for rid, g in dup.items():
        groups.setdefault(g, []).append(rid)

    mkr = districts.assign(flats)
    mkr_stats = districts.stats(flats, mkr, lambda r: costs(r)["total"])
    listings = []
    for r in flats:
        lat, lon = _v(r.get("lat")), _v(r.get("lon"))
        zcode, zname = zone_of(lat, lon)
        c = costs(r)
        status, notes = statuses.get(r["id"], ("", ""))
        hist = json.loads(r.get("price_history") or "[]")
        others = [o for o in groups.get(dup.get(r["id"]), []) if o != r["id"]]
        badges = [b for b in (r.get("badges") or "").split(" · ") if b]
        listings.append({
            "id": r["id"], "rent": r.get("source_type") == "arenda_program", "src": r.get("source") or "",
            "link": r.get("url") or "", "addr": r.get("address") or "адрес ?", "district": r.get("district") or "",
            "metro": r.get("metro") or "", "mkr": mkr.get(r["id"], ""),
            "zone": f"{zcode} {zname}", "inPoly": zcode != "Z0" or zname == "в полигоне, вне зон",
            "km": round(km(lat, lon, *NVK), 1) if lat is not None else 99.0, "geo": r.get("geo_source") or "",
            "travel": ZONE_TRAVEL.get(zcode, "уточнить"),
            "route": f"https://yandex.ru/maps/?rtext={lat},{lon}~{NVK[0]},{NVK[1]}&rtt=mt" if lat is not None else "",
            "type": r.get("category") or "квартира", "area": _v(r.get("area")) or "?", "floor": r.get("floor") or "",
            **c,
            "prev": _int(r.get("prev_price")), "hist": hist,
            "who": r.get("author_type") or "", "seller": r.get("author_name") or "",
            "sellerStats": r.get("seller_stats") or "",
            "badges": badges,
            "rosreestr": any("росреестр" in b.lower() for b in badges),
            "rooms": _int(r.get("rooms")),
            "owner": (r.get("author_type") == "собственник") or any(b == "Собственник" for b in badges),
            "desc": (r.get("description") or "")[:700],
            "pub": r.get("published") or "", "added": r.get("first_seen") or "", "seen": r.get("last_seen") or "",
            "addedAt": r.get("first_seen_at") or "",   # пусто = из первого (базового) сбора, не «новое»
            "active": r.get("is_active") is not False, "gone": r.get("inactive_since") or "",
            "status": status, "notes": notes,
            "dupGroup": dup.get(r["id"]) or "",
            "dups": [{"id": o, "src": by_id[o].get("source"), "link": by_id[o].get("url"),
                      "price": _int(by_id[o].get("price"))} for o in others],
            "lat": lat, "lon": lon,
        })

    social = []
    for r in posts:
        status, notes = statuses.get(r["id"], ("", ""))
        raw = {}
        try:
            raw = json.loads(r.get("raw_json") or "{}")
        except ValueError:
            pass
        social.append({
            "id": r["id"], "src": r.get("source") or "", "link": r.get("url") or "",
            "date": r.get("published") or "", "hits": r.get("hits") or "", "price": _int(r.get("price")),
            "addr": r.get("address") or "", "dragee": raw.get("dragee") or "",
            "text": (r.get("description") or "")[:900], "added": r.get("first_seen") or "",
            "addedAt": r.get("first_seen_at") or "",
            "badges": r.get("badges") or "", "dupOf": pdup.get(r["id"], ""),
            # «отпечаток» текста: «Отказ» на посте скрывает и его перепосты под новыми ID
            "fp": re.sub(r"\W+", "", (r.get("description") or "").lower())[:160],
            "status": status, "notes": notes,
        })

    try:
        runs = json.loads((DATA / "runs.json").read_text())
    except (OSError, ValueError):
        runs = []
    data = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "lastRun": runs[-1]["at"] if runs else "",
        "prevRun": runs[-2]["at"] if len(runs) > 1 else "",
        "nvk": list(NVK),
        "polygon": [list(map(float, p.split(","))) for p in POLYGON.split(";")],
        "listings": listings,
        "social": social,
        "transit": _load_extra("transit.json"),
        "smart": _load_extra("smart.json"),
        "districts": mkr_stats,
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    tpl = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", blob)
    out.write_text(tpl.replace("__MAP_HEAD__", LEAFLET_HEAD).replace("__MAPMODE__", "leaflet"), encoding="utf-8")
    try:
        key = json.loads(KEYS.read_text(encoding="utf-8")).get("yandex_maps_js", "")
    except (OSError, ValueError):
        key = ""
    if key:
        head = f'<script src="https://api-maps.yandex.ru/2.1/?apikey={key}&lang=ru_RU"></script>'
        OUT_YANDEX.write_text(tpl.replace("__MAP_HEAD__", head).replace("__MAPMODE__", "yandex"), encoding="utf-8")
    n_groups = len(set(dup.values()))
    log(f"🗺️  {out.name}{' + ' + OUT_YANDEX.name if key else ''}: объявлений {len(listings)} (активных {sum(x['active'] for x in listings)}), "
        f"постов {len(social)}; одна квартира в нескольких объявлениях: {n_groups} групп")
    return out


if __name__ == "__main__":
    export_to_viewer()
