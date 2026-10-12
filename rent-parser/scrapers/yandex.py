"""Сборщик Яндекс Недвижимости и Яндекс Аренды (весь Екатеринбург).
Чистый Python: HTTP-запрос страницы выдачи + разбор window.INITIAL_STATE, без браузера.

Режимы:
  full — все страницы каждого поиска (первый прогон / раз в неделю): видно, что пропало;
  inc  — выдача отсортирована по дате, листаем, пока на странице есть новые ID (максимум max_inc_pages).
"""
import json
import random
import re
import time
import urllib.request

from config import UA

BASE = "https://realty.yandex.ru/ekaterinburg/snyat"
# (ключ поиска, URL, вид жилья). Яндекс Аренда — отдельный поиск, это отдельный сервис.
SEARCHES = [
    ("ya_arenda", f"{BASE}/kvartira/yandex-arenda/?sort=DATE_DESC", "квартира"),
    ("ya_flat", f"{BASE}/kvartira/?priceMax=70000&sort=DATE_DESC", "квартира"),
    ("ya_room", f"{BASE}/komnata/?priceMax=35000&sort=DATE_DESC", "комната"),
]
AUTHOR = {"OWNER": "собственник", "AGENCY": "агентство", "AGENT": "агент",
          "PRIVATE_AGENT": "частный агент", "DEVELOPER": "застройщик"}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def initial_state(page_html):
    marker = "window.INITIAL_STATE ="
    i = page_html.find(marker)
    if i < 0:
        if "captcha" in page_html.lower():
            raise RuntimeError("Яндекс показал капчу")
        raise RuntimeError("не найден window.INITIAL_STATE — изменилась вёрстка?")
    state, _ = json.JSONDecoder().raw_decode(page_html[i + len(marker):].lstrip())
    return state


def offer_record(e, kind):
    """Объявление из INITIAL_STATE → запись. Неизвестное оставляем None (не подставляем 0)."""
    oid = e["offerId"]
    loc = e.get("location") or {}
    pt = loc.get("point") or {}
    price = (e.get("price") or {}).get("value")
    rooms = e.get("roomsTotal")
    studio = bool((e.get("house") or {}).get("studio")) or e.get("roomsTotalKey") == "STUDIO"
    if kind == "комната" or e.get("offerCategory") == "ROOMS":
        category, rooms_n = "Комната", rooms
    elif studio:
        category, rooms_n = "Студия", 0
    else:
        category, rooms_n = (f"{rooms}к" if rooms else "квартира"), rooms
    area = (e.get("area") or {}).get("value")

    is_rent = e.get("yandexRent") is True
    fee_pct = e.get("agentFee")                      # у Яндекса это проценты от месячной аренды
    if fee_pct is None and is_rent:
        fee_pct = 0                                  # у Яндекс Аренды комиссии нет по условиям сервиса
    deposit = e.get("rentDeposit")                   # берём как есть: у части объявлений Аренды он есть
    if deposit is not None and 0 < deposit < 10 and price:
        deposit = deposit * price                    # иногда указан в месяцах

    util_flag = e.get("utilitiesFee") or ""
    if e.get("utilitiesIncluded") is True or util_flag.lower() == "included":
        util_inc = True
    elif e.get("utilitiesIncluded") is False or util_flag.lower() in ("not_included", "meter"):
        util_inc = False
    else:
        util_inc = None

    author = e.get("author") or {}
    org = author.get("organization")
    org_name = org.get("name", "") if isinstance(org, dict) else (org or "")
    trust = e.get("trustedOfferInfo") or {}
    badges = []
    if trust.get("isFullTrustedOwner"):
        badges.append("Проверенный собственник")
    if trust.get("isCadastrPersonMatched"):
        badges.append("Собственник совпал с Росреестром")
    if e.get("suspicious"):
        badges.append("⚠️ Яндекс: подозрительное")
    if e.get("minRentPeriod"):
        badges.append(f"срок от {e['minRentPeriod']} мес")
    floors = e.get("floorsOffered") or []
    metro = (loc.get("metro") or {}).get("name") or ""
    site = (e.get("building") or {}).get("siteDisplayName") or ""
    addr = loc.get("address") or ""
    return {
        "id": f"Y{oid}",
        "source": "Яндекс Аренда" if is_rent else "Яндекс Недвижимость",
        "source_type": "arenda_program" if is_rent else "regular",
        "url": f"https://realty.yandex.ru/offer/{oid}/",
        "title": f"{category}, {area or '?'} м²" + (f", {floors[0]}/{e.get('floorsTotal')} эт." if floors else ""),
        "category": category,
        "price": price,
        "deposit": deposit,
        "commission_pct": fee_pct,
        "commission_rub": round(price * fee_pct / 100) if (price and fee_pct is not None) else None,
        "utilities_included": util_inc,
        "utilities_rub": None,
        "utilities_info": {"included": "включены", "not_included": "отдельно", "meter": "только счётчики"}.get(
            util_flag.lower(), "включены" if util_inc else ("отдельно" if util_inc is False else "не указано")),
        "rooms": rooms_n,
        "area": area,
        "floor": f"{floors[0]}/{e.get('floorsTotal') or '?'}" if floors else "",
        "address": addr + (f" ({site})" if site and site not in addr else ""),
        "district": next((c.get("value") for c in ((loc.get("structuredAddress") or {}).get("component") or [])
                          if c.get("regionType") in ("CITY_DISTRICT", "SUBLOCALITY")), "") or "",
        "metro": metro,
        "lat": pt.get("latitude"),
        "lon": pt.get("longitude"),
        "geo_source": "сайт" if pt.get("latitude") else "",
        "author_type": AUTHOR.get(author.get("category"), author.get("category") or ""),
        "author_name": author.get("agentName") or org_name,
        "badges": " · ".join(badges),
        "description": e.get("description") or "",
        "published": (e.get("creationDate") or "")[:10],
        "updated_on_site": (e.get("updateDate") or "")[:10],
        "raw_data": e,
    }


def scrape_yandex(mode="inc", known_ids=frozenset(), max_inc_pages=5, log=print):
    """Возвращает (records, полностью_пройденные_поиски)."""
    records, complete = {}, []
    for key, base, kind in SEARCHES:
        page, total_pages, ok = 1, 1, True
        limit = 10_000 if mode == "full" else max_inc_pages
        while page <= min(total_pages, limit):
            url = base + (f"&page={page}" if page > 1 else "")
            try:
                st = initial_state(fetch(url))
            except Exception as ex:  # noqa: BLE001
                log(f"[Яндекс/{key}] стр. {page}: ошибка {ex}")
                ok = False
                break
            offers = st["search"]["offers"]
            total_pages = offers.get("pager", {}).get("totalPages") or 1
            new_here = 0
            for e in offers.get("entities", []):
                if e.get("offerType") != "RENT" or not e.get("offerId"):
                    continue
                if (e.get("price") or {}).get("period") not in (None, "PER_MONTH"):
                    continue  # посуточные
                r = offer_record(e, kind)
                if r["id"] not in known_ids and r["id"] not in records:
                    new_here += 1
                records[r["id"]] = r
            log(f"[Яндекс/{key}] стр. {page}/{total_pages}: новых {new_here}, всего {len(records)}")
            if mode != "full" and new_here == 0:
                break  # дальше только уже известные
            page += 1
            time.sleep(random.uniform(2.5, 5))
        if mode == "full" and ok and page > total_pages:
            complete.append(key)
    return list(records.values()), complete


if __name__ == "__main__":
    items, _ = scrape_yandex(mode="inc", max_inc_pages=1)
    if items:
        print(json.dumps({k: v for k, v in items[0].items() if k != "raw_data"}, ensure_ascii=False, indent=2))
