"""Микрорайоны и медианы цен по ним — для слоя «Районы» на карте.

Название микрорайона есть только у части объявлений (ЦИАН, Домклик, треть Авито; у Яндекса почти нет).
Поэтому каждое объявление с координатами получает микрорайон голосованием K ближайших подписанных соседей
(«мкр. Уктус», «Уктус м-н» — одно и то же). Административные районы («р-н Кировский») слишком крупные — не берём.
Сайты режут город по-разному: ЦИАН зовёт «Птицефабрикой» и ЖК «Новокольцовский», Домклик и Яндекс — нет.
Поэтому у дома побеждает вложенное название: его дома почти все лежат внутри более широкого, а широкое
выходит за его пределы (Новокольцовский внутри «Птицефабрики» ЦИАН, Краснолесье внутри Академического; см. assign).
Медиана — цена с КУ (как «рынок» в smart.py), одна квартира на нескольких сайтах считается один раз.
"""
import re
from collections import Counter

import numpy as np

K = 9                 # сколько ближайших подписанных соседей голосуют
MAX_KM = 1.5          # дальше ближайшего подписанного соседа — микрорайон не знаем
MIN_TOTAL = 10        # микрорайон на карте — если в нём столько разных квартир
CELL = (0.002, 0.0035)   # клетка ~220 м: «следы» названий на карте сравниваем по клеткам
NEST_SHARE = 0.6     # B вложено в A: столько клеток B есть и у A…
NEST_OUT = 2         # …а у A есть хотя бы столько клеток вне B
NEST_MIN = 5         # и под B не меньше стольких объявлений (иначе мелкие ЖК раздробят районы)
MIN_TYPE = 3          # медиану по типу показываем от стольких квартир
TYPES = ("Студия", "1к", "2к", "3к", "4к+", "Комната")
ADMIN = {"Верх-Исетский", "Чкаловский", "Кировский", "Октябрьский", "Железнодорожный", "Ленинский",
         "Орджоникидзевский"}


def norm(d):
    """«мкр. Уктус» / «Уктус м-н» / «жилой район Вторчермет» / «р-н Академический» → «Уктус» и т. п.; иначе None."""
    if not d:
        return None
    s = re.sub(r"^(?:мкр\.?|мкрн\.?|микрорайон|жилой район|жилой комплекс|жк|ж/р|р-н)\s+|\s+(?:м-н|мкр\.?|микрорайон)$", "", d.strip(), flags=re.I)
    s = s.strip()
    if not s or s in ADMIN or re.search(r"\bр-н\b|район", s, re.I):
        return None
    return s[0].upper() + s[1:]


def kind_of(r):
    cat = r.get("category") or ""
    if cat in ("Студия", "Комната"):
        return cat
    n = r.get("rooms")
    m = re.match(r"(\d+)к", cat)
    n = int(m.group(1)) if m else (int(n) if n is not None else None)
    if n == 0:
        return "Студия"
    if n is None or n < 1:
        return None
    return "4к+" if n >= 4 else f"{n}к"


def from_addr(addr, known):
    """«Светлый микрорайон, 9», «мкр. Светлый, 7» → «Светлый», если такой микрорайон уже встречался."""
    m = re.search(r"(?:мкр\.?|микрорайон)\s+([А-ЯЁ][\w-]+)|([А-ЯЁ][\w-]+)\s+(?:микрорайон|мкр\b)", addr or "")
    name = m and (m.group(1) or m.group(2))
    return name if name in known else None


def assign(rows):
    """{id: микрорайон} для всех объявлений с координатами (или пусто, если соседи далеко).
    Микрорайон в адресе важнее всего (ЦИАН пишет «Светлый мкр, 9» в Уктус). Иначе голосуют K ближайших
    подписанных (своя подпись — самая близкая), а затем вложенное название рядом перебивает широкое."""
    lab = [(r["lat"], r["lon"], norm(r.get("district"))) for r in rows
           if r.get("lat") is not None and norm(r.get("district"))]
    if not lab:
        return {}
    known = {n for _, _, n in lab}
    pts = np.array([(a, b) for a, b, _ in lab], dtype=float)
    names = [n for _, _, n in lab]
    cos = np.cos(np.radians(56.8))
    cell = lambda la, lo: (round(la / CELL[0]), round(lo / CELL[1]))  # noqa: E731
    cells, cnt = {}, Counter(names)
    for (la, lo), n in zip(pts, names):
        cells.setdefault(n, set()).add(cell(la, lo))
    by_cell = {}
    for c_, ns in ((c_, n) for n, cs in cells.items() for c_ in cs):
        by_cell.setdefault(c_, set()).add(ns)

    def nested(b, a):
        cb, ca = cells[b], cells[a]
        return cnt[b] >= NEST_MIN and len(cb & ca) >= NEST_SHARE * len(cb) and len(ca - cb) >= NEST_OUT

    out = {}
    for r in rows:
        if r.get("lat") is None:
            continue
        own = from_addr(r.get("address"), known)
        if own:
            out[r["id"]] = own
            continue
        d = np.hypot((pts[:, 0] - r["lat"]) * 111.2, (pts[:, 1] - r["lon"]) * 111.2 * cos)
        idx = np.argpartition(d, min(K, len(d) - 1))[:K]
        idx = idx[d[idx] <= MAX_KM]
        if not len(idx):
            continue
        # ближние весят больше: 1 / (d + 200 м)
        votes = Counter()
        for i in idx:
            votes[names[i]] += 1 / (d[i] + 0.2)
        win = votes.most_common(1)[0][0]
        cy, cx = cell(r["lat"], r["lon"])
        near = set().union(*(by_cell.get((cy + i, cx + j), ()) for i in (-1, 0, 1) for j in (-1, 0, 1)))
        finer = [n for n in near if n != win and nested(n, win)]
        out[r["id"]] = min(finer, key=lambda n: len(cells[n])) if finer else win
    return out


def stats(rows, mkr, total_of):
    """[{name, lat, lon, n, types: {тип: {med, n, lo, hi}}}] — по активным квартирам без дублей."""
    pool = {}
    for r in rows:
        name, kind, tot = mkr.get(r["id"]), kind_of(r), total_of(r)
        if not name or not kind or not tot or r.get("is_active") is False:
            continue
        key = (round(r["lat"], 4), round(r["lon"], 4), r.get("area"), tot)
        pool.setdefault(name, {})[key] = (r["lat"], r["lon"], kind, tot)
    res = []
    for name, flats in pool.items():
        if len(flats) < MIN_TOTAL:
            continue
        a = list(flats.values())
        types = {}
        for t in TYPES:
            p = np.array([x[3] for x in a if x[2] == t], dtype=float)
            if len(p) >= MIN_TYPE:
                types[t] = {"med": int(round(np.median(p), -2)), "n": len(p),
                            "lo": int(round(np.percentile(p, 25), -2)), "hi": int(round(np.percentile(p, 75), -2))}
        res.append({"name": name, "lat": round(float(np.median([x[0] for x in a])), 5),
                    "lon": round(float(np.median([x[1] for x in a])), 5), "n": len(a), "types": types})
    return sorted(res, key=lambda x: -x["n"])
