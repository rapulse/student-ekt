"""Поиск одной и той же квартиры в разных объявлениях (между сайтами и внутри сайта).

Квартиры: одинаковый дом (нормализованный «улица|дом»), комнатность, этаж (если известен у обоих)
и площадь ±2 м². Без адреса — по координатам (до 60 м) + те же комнаты/этаж/площадь.
Посты ВК/TG: одинаковое начало текста (перепосты, повторы в канале).
Дубли не удаляются: у каждого объявления появляется dup_group и список «то же на других сайтах».
"""
import re

from config import km
from geocode import addr_key


def _floor(f):
    m = re.match(r"\s*(\d+)", str(f or ""))
    return int(m.group(1)) if m else None


def _rooms(r):
    if r.get("category") == "Студия":
        return 0
    v = r.get("rooms")
    return int(v) if v is not None and v == v else None


def same_flat(a, b):
    if a.get("category") == "Комната" or b.get("category") == "Комната":
        return False  # комнаты в одной квартире — разные предложения
    ra, rb = _rooms(a), _rooms(b)
    if ra is not None and rb is not None and ra != rb:
        return False
    fa, fb = _floor(a.get("floor")), _floor(b.get("floor"))
    if fa is not None and fb is not None and fa != fb:
        return False
    aa, ab = a.get("area"), b.get("area")
    if aa and ab and abs(float(aa) - float(ab)) > 2:
        return False
    if a["id"][:1] == b["id"][:1]:
        # внутри одного сайта: разные квартиры одного хозяина в одном доме (мини-отели, студии) —
        # дублем считаем только ту же цену и площадь
        if not (aa and ab and abs(float(aa) - float(ab)) <= 0.5 and a.get("price") == b.get("price")):
            return False
    # без этажа и площади совпадение по дому слишком слабое
    return (fa is not None and fb is not None) or bool(aa and ab)


def find_duplicates(rows):
    """rows — список dict объявлений (не посты). Возвращает {id: group_id}."""
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_house = {}
    for r in rows:
        k = addr_key(r.get("address") or "")
        if k:
            by_house.setdefault(k, []).append(r)
    for group in by_house.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if same_flat(a, b):
                    union(a["id"], b["id"])
    # по координатам — для тех, у кого адреса не совпали по написанию
    geo = [r for r in rows if r.get("lat") is not None and r.get("geo_source") == "сайт"]
    geo.sort(key=lambda r: r["lat"])
    for i, a in enumerate(geo):
        for b in geo[i + 1:]:
            if b["lat"] - a["lat"] > 0.0006:
                break
            if km(a["lat"], a["lon"], b["lat"], b["lon"]) <= 0.06 and same_flat(a, b) \
                    and a.get("area") and b.get("area") and _floor(a.get("floor")) is not None:
                union(a["id"], b["id"])
    groups = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r["id"])
    return {i: g for g, ids in groups.items() if len(ids) > 1 for i in ids}


def find_post_duplicates(posts):
    """Посты с одинаковым началом текста → {id: group_id}."""
    seen, out = {}, {}
    for p in sorted(posts, key=lambda p: p["id"]):
        key = re.sub(r"\W+", "", (p.get("description") or "").lower())[:160]
        if len(key) < 40:
            continue
        if key in seen:
            out[p["id"]] = seen[key]
            out[seen[key]] = seen[key]
        else:
            seen[key] = p["id"]
    return out
