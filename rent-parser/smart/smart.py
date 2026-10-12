"""Умный фильтр: надёжность, признаки приманки, минимальный срок, баллы — отдельный скрипт, базу не трогает.

Читает data/listings.parquet (только чтение) и data/transit.json (если есть), пишет data/smart.json:
  {"items": {"<id>": {"tier": "A|B|C|X", "why": "…", "warn": ["…"], "minTerm": 11, "score": 63, "scoreWhy": "…"}}}
Просмотрщик подхватывает файл, если он есть («Топ» в фильтрах); нет файла — карта как раньше.
Удалить эксперимент целиком: папку rent-parser/smart, data/smart.json и блок «УМНЫЙ ФИЛЬТР» в viewer_template.html.

Надёжность (ступени, а не баллы):
  A — Яндекс Аренда (в т.ч. на Авито): проверенный собственник, без залога и комиссии, договор сервиса;
  B — собственник или отметки проверки (Росреестр, документы, Госуслуги/СберID, реквизиты);
  C — ничего из этого;
  X — похоже на приманку: цена сильно ниже рынка + ещё признак (нет номера дома, агентство без проверок).
Баллы — параллельный вариант для сравнения (SCORE ниже), считаются по каждому объявлению.

  python3 rent-parser/smart/smart.py
  python3 rent-parser/smart/smart.py --check Y5165368795686069583
"""
import argparse
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

RP = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RP), str(RP / "pipeline")]
from config import DATA  # noqa: E402
from export_viewer import _v, costs  # noqa: E402  — та же цена с КУ, что на карте

OUT = DATA / "smart.json"
PARQUET = DATA / "listings.parquet"
TRANSIT = DATA / "transit.json"

CHEAP = 0.75          # цена < 75% медианы того же типа в той же зоне — «сильно ниже рынка»
VERY_CHEAP = 0.55     # так дёшево — подозрительно и без других признаков
MEDIAN_MIN_N = 10     # «рынок» — медиана цены с КУ того же типа по соседям: берём наименьший радиус,
MARKET_RADII = (1.5, 2.5, 4, 7)   # где набралось столько разных квартир; не набралось — медиана по городу
# «Сдаётcя квapтиpa»: латиница вперемешку с кириллицей — обход проверки Авито на одинаковые тексты
# (перекупщики, агенты под видом собственника). Считаем слова, где есть обе азбуки.
MIXED_RX = re.compile(r"\b(?=\w*[а-яё])(?=\w*[a-z])\w+\b", re.I)
MIXED_MIN = 3
VERIFY_RX = re.compile(r"росреестр|документы проверены|госуслуг|сберid|реквизиты проверены|данные подтверждены|собственник", re.I)
TERM_TEXT_RX = re.compile(r"(?:от|не менее|минимум|минимальный срок[^.\d]{0,15})\s*(\d{1,2})\s*(?:-?х\s*)?мес|"
                          r"(?:от|не менее|минимум)\s+(?:одного\s+|1\s+)?года", re.I)


def min_term(badges, text):
    for b in badges:
        m = re.match(r"срок от (\d+) мес", b)
        if m:
            return int(m.group(1))
    m = TERM_TEXT_RX.search(text or "")
    if m:
        return int(m.group(1)) if m.group(1) else 12
    return None


def has_house(addr):
    a = re.sub(r"екатеринбург", "", addr or "", flags=re.I)
    return bool(re.search(r",\s*(?:д\.?\s*)?\d", a))


def score(tier, pp, dep, comm, price, comm_pct, trip, term, ku_inc, rent):
    """Баллы — простая сумма; каждое слагаемое видно в scoreWhy."""
    parts = []
    parts.append({"A": (40, "Я.Аренда"), "B": (15, "проверки"), "C": (0, "без проверок"), "X": (-50, "похоже на приманку")}[tier])
    parts.append((25, "≤20к/чел") if pp <= 20000 else (15, "≤25к/чел") if pp <= 25000 else (5, "≤30к/чел") if pp <= 30000 else (-20, ">30к/чел"))
    parts.append((10, "без залога") if dep == 0 else (-5, "залог ?") if dep is None else (-10, "залог > месяца") if dep > price else (0, "залог"))
    parts.append((10, "без комиссии") if comm == 0 else (-5, "комиссия ?") if comm is None
                 else (-15, "комиссия ≥50%") if (comm_pct or 0) >= 50 or comm >= price * 0.5 else (-10, "комиссия"))
    if trip is None:
        parts.append((-15, "нет прямого транспорта"))
    else:
        parts.append((15, "≤15 мин") if trip <= 15 else (10, "≤30 мин") if trip <= 30 else (0, "≤45 мин") if trip <= 45 else (-10, ">45 мин"))
    if term and term >= 6 and not rent:
        parts.append((-15, f"срок от {term} мес"))
    if ku_inc:
        parts.append((5, "КУ включены"))
    return sum(p for p, _ in parts), " ".join(f"{p:+d} {w}" for p, w in parts if p)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", help="показать разбор одного объявления по ID")
    args = ap.parse_args()
    import pandas as pd
    df = pd.read_parquet(PARQUET)
    df = df[(df["source_type"] != "social")]
    rows = [{k: _v(v) for k, v in r.items()} for r in df.to_dict("records")]
    try:
        trips = json.loads(TRANSIT.read_text(encoding="utf-8")).get("points", {})
    except (OSError, ValueError):
        trips = {}

    # «рынок»: медиана цены с КУ (аренда + КУ, КУ — как на карте, в т.ч. оценка) того же типа у соседей.
    # Одна квартира на нескольких сайтах считается один раз (тип, точка, площадь, цена).
    import numpy as np
    prep = []
    for r in rows:
        c = costs(r)
        kind = r.get("category") or (f"{r.get('rooms')}к" if r.get("rooms") is not None else "?")
        prep.append((r, c, kind))
    pool = {}
    for r, c, kind in prep:
        if r.get("is_active") is False or not c["total"] or r.get("lat") is None:
            continue
        pool.setdefault(kind, {})[(round(r["lat"], 4), round(r["lon"], 4), r.get("area"), c["total"])] = (r["lat"], r["lon"], c["total"])
    arr = {k: np.array(list(v.values()), dtype=float) for k, v in pool.items()}
    city = {k: float(np.median(a[:, 2])) for k, a in arr.items() if len(a) >= MEDIAN_MIN_N}

    def market(kind, lat, lon):
        a = arr.get(kind)
        if a is None or len(a) < MEDIAN_MIN_N:
            return None
        if lat is not None:
            dy = (a[:, 0] - lat) * 111.2
            dx = (a[:, 1] - lon) * 111.2 * math.cos(math.radians(lat))
            d = np.hypot(dx, dy)
            for rad in MARKET_RADII:
                sel = a[d <= rad, 2]
                if len(sel) >= MEDIAN_MIN_N:
                    return float(np.median(sel)), f"{kind} в {str(rad).replace('.', ',')} км", len(sel)
        return city[kind], f"{kind} по городу", len(a)

    out = {}
    for r, c, kind in prep:
        badges = [b for b in (r.get("badges") or "").split(" · ") if b]
        rent = r.get("source_type") == "arenda_program"
        mixed = len(MIXED_RX.findall(r.get("description") or "")) >= MIXED_MIN
        # «собственник» со слов автора при замаскированном тексте — не проверка (Росреестр и т. п. — да)
        owner = r.get("author_type") == "собственник" and not mixed
        verified = owner or any(VERIFY_RX.search(b) and not (mixed and b.lower() == "собственник") for b in badges)
        warn = []
        if mixed:
            warn.append("в тексте латиница вместо русских букв — маскировка (перекупщик/агент?)")
        mk = market(kind, r.get("lat"), r.get("lon"))
        ratio = c["total"] / mk[0] if mk and c["total"] else None
        mkt = {"med": round(mk[0]), "pct": round((ratio - 1) * 100), "where": mk[1], "n": mk[2]} if ratio else None
        if ratio and ratio < CHEAP:
            warn.append(f"цена на {round((1 - ratio) * 100)}% ниже рынка ({mk[1]}, медиана {round(mk[0] / 1000)}к)")
        if not has_house(r.get("address")):
            warn.append("нет номера дома")
        if r.get("author_type") == "агентство" and not verified and not rent:
            warn.append("агентство без проверок")
        cheap = bool(ratio and ratio < CHEAP)
        scam = not rent and cheap and (len(warn) >= 2 or ratio < VERY_CHEAP)
        tier = "A" if rent else "X" if scam else "B" if verified else "C"
        why = ("Яндекс Аренда" if rent else "приманка? " + "; ".join(warn) if scam
               else "проверки: " + ", ".join([b for b in badges if VERIFY_RX.search(b)] + (["собственник"] if owner else []))
               if verified else "нет отметок проверки")
        term = min_term(badges, r.get("description"))
        lat, lon = r.get("lat"), r.get("lon")
        t = trips.get(f"{lat:.4f},{lon:.4f}") if lat is not None else None
        trip = t.get("min") if t else None
        sc, sc_why = score(tier, c["pp"] if c["share"] == 1 else round(c["total"] / c["share"]), c["dep"], c["comm"],
                           c["price"], _v(r.get("commission_pct")), trip, term, c["ku"] == 0 and "включены" in c["kuNote"], rent)
        out[r["id"]] = {"tier": tier, "why": why, "warn": warn, "minTerm": term, "score": sc, "scoreWhy": sc_why,
                       "mkt": mkt}

    if args.check:
        print(json.dumps(out.get(args.check), ensure_ascii=False, indent=1))
        return 0
    payload = {"_meta": {"calc": datetime.now().strftime("%Y-%m-%d %H:%M")}, "items": out}
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)
    n = {k: sum(1 for v in out.values() if v["tier"] == k) for k in "ABCX"}
    print(f"⭐ умный фильтр: A {n['A']} · B {n['B']} · C {n['C']} · похоже на приманку {n['X']}; "
          f"с мин. сроком {sum(1 for v in out.values() if v['minTerm'])}; {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
