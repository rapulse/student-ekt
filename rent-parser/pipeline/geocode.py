"""Координаты по адресу для объявлений без координат (Авито, посты).

1) Кэш data/geocache.json: адрес → [lat, lon, откуда].
2) Дома, которые уже есть с координатами у Яндекса/Домклика/карточек Авито (тот же нормализованный адрес).
3) Nominatim (OpenStreetMap): 1 запрос в секунду по правилам сервиса, не больше limit за прогон —
   остальное догеокодируется в следующие прогоны. Уходит только адрес дома, ничего личного.
Координаты из геокодера помечаются geo_source = «геокодер» (точность — до дома, иногда до улицы).
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from config import DATA, RP

CACHE = DATA / "geocache.json"
UA = "student-nvk-rent-parser/1.0 (personal flat search)"
# Екатеринбург с окрестностями — отсекаем однофамильные улицы в других городах
VIEWBOX = "60.35,56.98,60.90,56.68"

TYPE_WORDS = {
    "улица": "улица", "ул": "улица", "переулок": "переулок", "пер": "переулок", "проспект": "проспект",
    "пр-кт": "проспект", "пр-т": "проспект", "пр": "проспект", "бульвар": "бульвар", "б-р": "бульвар",
    "тракт": "тракт", "шоссе": "шоссе", "ш": "шоссе", "площадь": "площадь", "пл": "площадь",
    "набережная": "набережная", "наб": "набережная", "проезд": "проезд", "микрорайон": "микрорайон",
    "мкр": "микрорайон", "мкр-н": "микрорайон", "мкрн": "микрорайон", "микр": "микрорайон", "квартал": "квартал", "тупик": "тупик", "аллея": "аллея",
}
NOISE = re.compile(r"^(?:россия|свердловская обл(?:асть|\.)?|екатеринбург|г\.? ?екатеринбург|"
                   r"жилой район .*|.* м-н|.* р-н|р-н .*|жк .*)$", re.I)


def split_address(addr):
    """«ул. Чапаева, 80к1» / «Рощинская улица, 46/2 (ЖК …)» → (улица-ключ, дом, улица для геокодера)."""
    a = re.sub(r"\(.*?\)", "", addr or "").replace("ё", "е").replace("Ё", "Е").strip()
    # мусор в адресах Авито: «этаж 3», «оф3010», «кв. 5», «подъезд 2»; «3.1» → «3/1»
    a = re.sub(r"(\d)\.(\d)", r"\1/\2", a)
    parts = [p.strip() for p in a.split(",") if p.strip() and not NOISE.match(p.strip())
             and not re.match(r"^(?:этаж|эт\.|оф\.?\s*\d|офис|кв\.?\s*\d|квартира|подъезд|пом\.)", p.strip(), re.I)]
    if not parts:
        return "", "", ""
    house, street = "", parts[0]
    for i in range(len(parts) - 1, -1, -1):
        if re.match(r"^(?:д\.?\s*)?\d+[а-яa-z]?(?:[/к ]\s*\d+[а-я]?)*(?:\s*(?:к|корп\.?|корпус|стр\.?|с)\s*\d+)?$", parts[i], re.I):
            house = parts[i]
            street = parts[i - 1] if i > 0 else ""
            break
    words = [w.strip(".") for w in street.lower().split()]
    typ = next((TYPE_WORDS[w] for w in words if w in TYPE_WORDS), "")
    name = " ".join(w for w in words if w not in TYPE_WORDS)
    house = re.sub(r"\s+", "", house.lower()).replace("корпус", "к").replace("корп.", "к").replace("корп", "к")
    house = re.sub(r"^д\.?", "", house).replace("стр.", "с").replace("стр", "с")
    key_street = " ".join(sorted(name.replace("-", " ").split()))
    return key_street, house, (f"{typ} {name}".strip() if typ else name)


def addr_key(addr):
    s, h, _ = split_address(addr)
    return f"{s}|{h}" if s and h else ""


def _load():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _nominatim(street, house):
    params = {"format": "json", "limit": 1, "countrycodes": "ru", "viewbox": VIEWBOX, "bounded": 1,
              "street": f"{house} {street}", "city": "Екатеринбург"}
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.loads(r.read().decode("utf-8"))
    return (float(res[0]["lat"]), float(res[0]["lon"])) if res else None


# ---------- подстраховка: геокодер Яндекса (бесплатно 1000 запросов в сутки) ----------
YA_DAILY = 900          # оставляем запас от лимита 1000
_ya = {"key": None, "dead": False}


def _ya_key():
    if _ya["key"] is None:
        try:
            _ya["key"] = json.loads((RP / "local_keys.json").read_text(encoding="utf-8")).get("yandex_geocoder", "")
        except (OSError, ValueError):
            _ya["key"] = ""
    return _ya["key"]


def _ya_budget(cache):
    """Сколько запросов к Яндексу осталось на сегодня (счётчик в кэше)."""
    today = time.strftime("%Y-%m-%d")
    st = cache.setdefault("__yandex_usage__", {"date": today, "n": 0})
    if st["date"] != today:
        st.update(date=today, n=0)
    return YA_DAILY - st["n"]


def _yandex(address, cache, log):
    """→ ((lat, lon), точность) или None. Точность Яндекса: exact/number/near — дом, street — улица."""
    key = _ya_key()
    if not key or _ya["dead"] or _ya_budget(cache) <= 0:
        return None
    params = {"apikey": key, "format": "json", "lang": "ru_RU", "results": 1, "rspn": 1,
              "bbox": "60.35,56.68~60.90,56.98", "geocode": f"Екатеринбург, {address}"}
    url = "https://geocode-maps.yandex.ru/1.x/?" + urllib.parse.urlencode(params)
    cache["__yandex_usage__"]["n"] += 1
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        if ex.code in (401, 403, 429):
            _ya["dead"] = True
            log(f"[Геокодер] Яндекс недоступен ({ex.code}: ключ не активен или лимит) — пропускаю до следующего прогона")
        return None
    except Exception:  # noqa: BLE001
        return None
    fm = d.get("response", {}).get("GeoObjectCollection", {}).get("featureMember", [])
    if not fm:
        return None
    g = fm[0]["GeoObject"]
    lon, lat = map(float, g["Point"]["pos"].split())
    return (lat, lon), g["metaDataProperty"]["GeocoderMetaData"].get("precision", "")


def _nominatim_street(street):
    params = {"format": "json", "limit": 1, "countrycodes": "ru", "viewbox": VIEWBOX, "bounded": 1,
              "street": street, "city": "Екатеринбург"}
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.loads(r.read().decode("utf-8"))
    return (float(res[0]["lat"]), float(res[0]["lon"])) if res else None


def geocode_records(records, known_coords, limit=300, log=print):
    """records — список dict с address/lat/lon; заполняет lat/lon/geo_source на месте.
    known_coords — {addr_key: (lat, lon)} из объявлений с координатами сайта.
    Порядок: тот же дом на другом сайте → кэш → Nominatim (дом) → улица по сайтам → Nominatim (улица)."""
    cache = _load()
    by_street = {}
    for k, (la, lo) in known_coords.items():
        by_street.setdefault(k.split("|")[0], []).append((la, lo))
    street_mid = {st: (sorted(p[0] for p in pts)[len(pts) // 2], sorted(p[1] for p in pts)[len(pts) // 2])
                  for st, pts in by_street.items()}
    asked, hits, approx = 0, 0, 0

    def ask(fn, *a):
        nonlocal asked
        try:
            return fn(*a)
        finally:
            asked += 1
            time.sleep(1.1)

    for r in records:
        if r.get("lat") is not None or not r.get("address"):
            continue
        street_key, house, street = split_address(r["address"])
        if not street_key:
            continue
        key = f"{street_key}|{house}"
        if house and key in known_coords:
            r["lat"], r["lon"] = known_coords[key]
            r["geo_source"] = "дом с другого сайта"
            hits += 1
            continue
        pt = None
        if house:
            if key in cache:
                pt = cache[key]
            elif asked < limit:
                try:
                    pt = ask(_nominatim, street, house)
                    if pt is None and re.search(r"[к/]", house):        # «80к1» → «80»
                        pt = ask(_nominatim, street, re.split(r"[к/с]", house)[0])
                except Exception as ex:  # noqa: BLE001
                    log(f"[Геокодер] {r['address']}: ошибка {ex}")
                    time.sleep(5)
                    continue
                cache[key] = list(pt) if pt else None
            else:
                continue
        ya_street = None
        if not pt and house:
            ykey = f"ya:{key}"
            if ykey in cache and cache[ykey]:
                yres = (tuple(cache[ykey][:2]), cache[ykey][2])
            elif ykey in cache:
                yres = None
            else:
                yres = _yandex(r["address"], cache, log)
                if yres or not _ya["dead"]:
                    cache[ykey] = [yres[0][0], yres[0][1], yres[1]] if yres else None
            if yres and yres[1] in ("exact", "number", "near"):
                r["lat"], r["lon"], r["geo_source"] = yres[0][0], yres[0][1], "геокодер Яндекса"
                hits += 1
                continue
            if yres and yres[1] == "street":
                ya_street = yres[0]
        if pt:
            r["lat"], r["lon"], r["geo_source"] = pt[0], pt[1], "геокодер"
            hits += 1
        else:
            # Дома нет в OpenStreetMap (новостройки) или в адресе нет дома — ставим точку по улице
            skey = f"street:{street_key}"
            if ya_street:
                spt = ya_street
            elif street_key in street_mid:
                spt = street_mid[street_key]
            elif skey in cache:
                spt = cache[skey]
            elif asked < limit:
                try:
                    spt = ask(_nominatim_street, street)
                except Exception:  # noqa: BLE001
                    continue
                cache[skey] = list(spt) if spt else None
            else:
                continue
            if spt:
                r["lat"], r["lon"], r["geo_source"] = spt[0], spt[1], "приблизительно (улица)"
                approx += 1
        if asked and asked % 25 == 0:
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    left = sum(1 for r in records if r.get("lat") is None and r.get("address"))
    log(f"[Геокодер] по дому: {hits}, приблизительно по улице: {approx}, запросов к Nominatim: {asked}, "
        f"без координат: {left}")
