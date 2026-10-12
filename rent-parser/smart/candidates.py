"""Выборка кандидатов для агента «топ-10» (rent-parser/AGENT_TOP10.md) — только чтение, базу не трогает.

Собирает по квартирам (одна квартира на нескольких сайтах — одна запись) всё, что уже посчитано:
цена с КУ, въезд, на человека, «реальная» цена за 3 и 8 мес, надёжность и приманка (smart.json),
цена к рынку, время в пути до НВК / РТФ / фитнеса (transit.json), отметки «интересно/отказ» (если выгружены).
Пишет rent-parser/tmp/candidates.json и печатает краткую сводку.

  python3 rent-parser/smart/candidates.py                 # по умолчанию: ≤32 000 ₽/чел в мес, ≤60 мин до НВК, 80 лучших
  python3 rent-parser/smart/candidates.py --limit 300 --max-pp 35000 --max-trip 75
"""
import argparse
import json
import sys
from pathlib import Path

RP = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RP), str(RP / "pipeline")]
from config import DATA, NVK, km, zone_of  # noqa: E402
from dedup import find_duplicates  # noqa: E402
from export_viewer import _int, _v, costs, load_statuses  # noqa: E402

OUT = RP / "tmp" / "candidates.json"
TIER_RANK = {"A": 0, "B": 1, "C": 2, "X": 3}
TRIP_RUB_PER_MIN = 100


def load(name):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def per_person(c, rows_pair, months):
    """(аренда + КУ + (залог + комиссия) / мес) / жильцов; неизвестные залог/комиссия — по худшему."""
    dep = c["dep"] if c["dep"] is not None else c["price"]
    comm = c["comm"] if c["comm"] is not None else round(c["price"] * 0.5)
    return round((c["total"] + (dep + comm) / months) / rows_pair)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--max-pp", type=int, default=32000, help="макс. ₽/чел в мес с КУ (без залога/комиссии)")
    ap.add_argument("--max-trip", type=int, default=60, help="макс. минут до НВК (оценка)")
    args = ap.parse_args()
    import pandas as pd
    df = pd.read_parquet(DATA / "listings.parquet")
    rows = [{k: _v(v) for k, v in r.items()} for r in df.to_dict("records")]
    flats = [r for r in rows if r.get("source_type") != "social" and r.get("is_active") is not False]
    smart = load("smart.json").get("items", {})
    trips = load("transit.json").get("points", {})
    statuses = load_statuses()
    dup = find_duplicates(flats)
    groups = {}
    for r in flats:
        groups.setdefault(dup.get(r["id"]) or "I" + r["id"], []).append(r)

    out = []
    for key, items in groups.items():
        offers = []
        for r in items:
            c = costs(r)
            if not c["total"]:
                continue
            s = smart.get(r["id"], {})
            offers.append({"r": r, "c": c, "s": s})
        if not offers:
            continue
        # как в просмотрщике: не берём условия, которые противоречат другим сайтам (неизвестное при известном)
        est = lambda o: "оценка" in o["c"]["kuNote"] and "счётчики" not in o["c"]["kuNote"]
        contra = lambda o: ((o["c"]["comm"] is None and any((x["c"]["comm"] or 0) > 0 for x in offers)) or
                            (o["c"]["dep"] is None and any((x["c"]["dep"] or 0) > 0 for x in offers)) or
                            (est(o) and any(not est(x) for x in offers)))
        pool = [o for o in offers if not contra(o)] or offers
        best = min(pool, key=lambda o: (TIER_RANK.get(o["s"].get("tier"), 2), o["c"]["total"]))
        r, c, s = best["r"], best["c"], best["s"]
        lat, lon = r.get("lat"), r.get("lon")
        if lat is None:
            continue
        people = c["share"]
        t = trips.get(f"{lat:.4f},{lon:.4f}") or {}
        monthly_pp = round(c["total"] / people)
        if monthly_pp > args.max_pp or s.get("tier") == "X":
            continue
        if t.get("min") is None or t["min"] > args.max_trip:
            continue
        marks = {statuses[o["r"]["id"]][0] for o in offers if statuses.get(o["r"]["id"], ("", ""))[0]}
        if "Отказ" in marks:
            continue
        pp3, pp8 = per_person(c, people, 3), per_person(c, people, 8)
        wavg = t.get("wavg", t.get("min"))
        out.append({
            "key": key, "addr": r.get("address"), "zone": " ".join(zone_of(lat, lon)), "kmNVK": round(km(lat, lon, *NVK), 1),
            "type": r.get("category"), "area": _v(r.get("area")), "floor": r.get("floor"), "people": people,
            "price": c["price"], "ku": c["ku"], "kuNote": c["kuNote"], "totalMonth": c["total"], "monthlyPP": monthly_pp,
            "deposit": c["dep"], "commission": c["comm"], "entry": c["price"] + (c["dep"] or 0) + (c["comm"] or 0),
            "entryUnknown": c["dep"] is None or c["comm"] is None,
            "realPP_3m": pp3, "realPP_8m": pp8,
            "tier": s.get("tier"), "trust": s.get("why"), "warn": s.get("warn", []), "minTerm": s.get("minTerm"),
            "market": s.get("mkt"), "score": s.get("score"),
            "tripNVK": {k: t.get(k) for k in ("min", "walk", "bus", "transfers")},
            "tripRTF": {k: (t.get("dest", {}).get("rtf") or {}).get(k) for k in ("min", "bus")},
            "tripGym": {k: (t.get("dest", {}).get("gym") or {}).get(k) for k in ("min", "bus")},
            "tripWavg": wavg,
            "costIndex_3m": pp3 + (wavg or 45) * TRIP_RUB_PER_MIN,
            "mark": ", ".join(sorted(marks)),
            "offers": [{"id": o["r"]["id"], "source": o["r"].get("source"), "url": o["r"].get("url"),
                        "price": o["c"]["price"], "terms": o["c"]["terms"], "author": f'{o["r"].get("author_type") or ""} {o["r"].get("author_name") or ""}'.strip(),
                        "badges": o["r"].get("badges"), "published": o["r"].get("published"), "firstSeen": o["r"].get("first_seen")}
                       for o in offers],
            "desc": (max((o["r"].get("description") or "" for o in offers), key=len))[:400],
        })
    out.sort(key=lambda x: (TIER_RANK.get(x["tier"], 2), x["costIndex_3m"]))
    total = len(out)
    out = out[:args.limit]
    OUT.parent.mkdir(exist_ok=True)
    # одна квартира — одна строка: файл удобно читать по частям
    OUT.write_text('{"filters": ' + json.dumps(vars(args)) + f', "matched": {total}, "candidates": [\n'
                   + ",\n".join(json.dumps(x, ensure_ascii=False) for x in out) + "\n]}\n", encoding="utf-8")
    from collections import Counter
    print(f"кандидатов: {total} (в файле {len(out)}), уровни {dict(Counter(x['tier'] for x in out))} → {OUT.relative_to(RP.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
