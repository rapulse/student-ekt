"""Примерное время в пути до НВК (пешком + прямой автобус) — отдельный скрипт, базу не трогает.

Читает координаты из data/listings.parquet (только чтение), пишет data/transit.json:
  {"56.8121,60.6310": {"min": 41, "walk": 12, "ride": 22, "wait": 7, "bus": "56", "alt": ["44"], "stop": "…"}, …}
Просмотрщик (export_viewer.py) подхватывает этот файл, если он есть; нет файла — карта как раньше.

Маршруты — из OpenStreetMap (Overpass, без ключа): все автобусы/троллейбусы/трамваи с остановкой
не дальше CAMPUS_STOP_KM от НВК, с порядком остановок. Расписания и пробок в OSM нет, поэтому это ОЦЕНКА:
  пешком до остановки (по прямой × DETOUR, WALK_KMH) + ожидание WAIT_MIN
  + езда по остановкам маршрута (по прямой между соседними × DETOUR_BUS, BUS_KMH) + пешком от остановки до НВК.
Пересадки не считаются: нет прямого автобуса в пределах MAX_WALK_KM — «min»: null.

  python3 rent-parser/transit/transit.py            # посчитать (маршруты OSM обновляются раз в неделю)
  python3 rent-parser/transit/transit.py --refresh  # заново скачать маршруты
  python3 rent-parser/transit/transit.py --check 56.8121,60.631   # разбор одной точки
"""
import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
PARQUET = DATA / "listings.parquet"
OUT = DATA / "transit.json"
ROUTES_CACHE = DATA / "transit_routes.json"

NVK = (56.7735, 60.7607)        # корпус ИРИТ-РТФ, как в config.py
MAX_KM_FROM_NVK = 15            # дальше не считаем
CAMPUS_STOP_KM = 0.8            # остановка «у кампуса» — не дальше этого от корпуса
MAX_WALK_KM = 1.5               # до своей остановки пешком (по прямой)
WALK_KMH = 4.5
DETOUR = 1.3                    # пешком по улицам длиннее, чем по прямой
BUS_KMH = 20                    # средняя скорость автобуса с остановками
DETOUR_BUS = 1.2
WAIT_MIN = 7                    # ожидание: интервалов в OSM нет — средняя оценка
ROUTES_MAX_AGE_DAYS = 7
OVERPASS = ["https://overpass-api.de/api/interpreter",          # основной и зеркала: бывает 504 при нагрузке
            "https://overpass.private.coffee/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
ROUTE_KINDS = "bus|trolleybus|tram|share_taxi|minibus"
KIND_LABEL = {"bus": "авт.", "trolleybus": "тролл.", "tram": "трамв.", "share_taxi": "марш.", "minibus": "марш."}


def km(a_lat, a_lon, b_lat, b_lon):
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def walk_min(dist_km):
    return dist_km * DETOUR / WALK_KMH * 60


def point_key(lat, lon):
    # та же точка, что g.lat.toFixed(4) в просмотрщике
    return f"{lat:.4f},{lon:.4f}"


# ---------- маршруты из OpenStreetMap ----------
def fetch_routes(log=print):
    q = f"""[out:json][timeout:120];
(node(around:{int(CAMPUS_STOP_KM * 1000)},{NVK[0]},{NVK[1]})[highway=bus_stop];
 node(around:{int(CAMPUS_STOP_KM * 1000)},{NVK[0]},{NVK[1]})[public_transport];)->.s;
rel(bn.s)[type=route][route~"^({ROUTE_KINDS})$"]->.r;
.r out body;
node(r.r);
out skel qt;"""
    d, err = None, None
    for url in [u for _ in range(3) for u in OVERPASS]:   # 3 круга по зеркалам с паузой
        if err:
            time.sleep(15)
        req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(),
                                     headers={"User-Agent": "student-nvk-transit/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            break
        except Exception as e:
            err = e
            log(f"⚠️  {url.split('/')[2]}: {e}")
    if d is None:
        raise err
    nodes = {e["id"]: (e["lat"], e["lon"], (e.get("tags") or {}).get("name", "")) for e in d["elements"] if e["type"] == "node"}
    routes = []
    for e in d["elements"]:
        if e["type"] != "relation":
            continue
        t = e.get("tags") or {}
        members = [m for m in e.get("members", []) if m["type"] == "node" and m["ref"] in nodes]
        # предпочитаем точки остановки на дороге (stop*), иначе платформы
        picked = [m for m in members if m.get("role", "").startswith("stop")] or \
                 [m for m in members if m.get("role", "").startswith("platform")]
        stops = []
        for m in picked:
            lat, lon, _ = nodes[m["ref"]]
            if stops and km(lat, lon, stops[-1][0], stops[-1][1]) < 0.06:
                continue    # stop + platform одной остановки
            stops.append([lat, lon, ""])
        # названия: у skel-узлов тегов нет — берём из имени платформы, если было; иначе пусто
        if len(stops) >= 2:
            routes.append({"ref": t.get("ref", "?"), "kind": t.get("route", "bus"), "name": t.get("name", ""),
                           "stops": stops})
    log(f"🚌 OSM: маршрутов через НВК (в обе стороны) — {len(routes)}: "
        f"{', '.join(sorted({r['ref'] for r in routes}, key=lambda x: (len(x), x)))}")
    return routes


def load_routes(refresh=False, log=print):
    if not refresh and ROUTES_CACHE.exists():
        c = json.loads(ROUTES_CACHE.read_text(encoding="utf-8"))
        age = (datetime.now() - datetime.fromisoformat(c["fetched"])).days
        if age < ROUTES_MAX_AGE_DAYS:
            return c["routes"]
    try:
        routes = fetch_routes(log)
    except Exception as e:  # сеть/Overpass недоступны — работаем по старому кэшу, если он есть
        if ROUTES_CACHE.exists():
            log(f"⚠️  Overpass недоступен ({e}) — беру маршруты из кэша")
            return json.loads(ROUTES_CACHE.read_text(encoding="utf-8"))["routes"]
        raise
    ROUTES_CACHE.write_text(json.dumps({"fetched": datetime.now().isoformat(timespec="seconds"), "routes": routes},
                                       ensure_ascii=False), encoding="utf-8")
    return routes


def prepare(routes):
    """Для каждого маршрута: где у него остановки «у кампуса» и накопленный путь вдоль остановок."""
    out = []
    # в OSM у части маршрутов (66) есть только одно направление — второе считаем тем же путём наоборот
    refs = {}
    for r in routes:
        refs.setdefault((r["kind"], r["ref"]), []).append(r)
    routes = routes + [{**v[0], "stops": v[0]["stops"][::-1], "name": v[0]["name"] + " (обратно, по OSM нет)"}
                       for v in refs.values() if len(v) == 1]
    for r in routes:
        st = r["stops"]
        cum = [0.0]
        for a, b in zip(st, st[1:]):
            cum.append(cum[-1] + km(a[0], a[1], b[0], b[1]) * DETOUR_BUS)
        campus = [i for i, s in enumerate(st) if km(s[0], s[1], *NVK) <= CAMPUS_STOP_KM]
        if campus:
            out.append({**r, "cum": cum, "campus": campus})
    return out


def best_trip(lat, lon, routes):
    """Лучший вариант «пешком → прямой автобус к кампусу → пешком» или просто пешком."""
    options = []
    direct = km(lat, lon, *NVK)
    if direct <= 3:
        options.append({"min": walk_min(direct), "walk": walk_min(direct), "ride": 0, "wait": 0, "bus": None})
    for r in routes:
        st, cum = r["stops"], r["cum"]
        for c in r["campus"]:
            to_nvk = walk_min(km(st[c][0], st[c][1], *NVK))
            for i in range(c):           # садимся до кампуса — значит едем в нужную сторону
                d = km(lat, lon, st[i][0], st[i][1])
                if d > MAX_WALK_KM:
                    continue
                ride = (cum[c] - cum[i]) / BUS_KMH * 60
                w = walk_min(d) + to_nvk
                options.append({"min": w + WAIT_MIN + ride, "walk": w, "ride": ride, "wait": WAIT_MIN,
                                "bus": f"{KIND_LABEL.get(r['kind'], '')} {r['ref']}".strip(),
                                "stopKm": d})
    if not options:
        return {"min": None, "note": f"нет прямого транспорта до НВК в {MAX_WALK_KM} км — с пересадкой, уточнить"}
    best = min(options, key=lambda o: o["min"])
    res = {k: (round(v) if isinstance(v, float) else v) for k, v in best.items() if k != "stopKm"}
    if best["bus"]:
        # другие прямые маршруты, которые немногим хуже
        alt = sorted({o["bus"] for o in options if o["bus"] and o["bus"] != best["bus"] and o["min"] <= best["min"] + 7})
        if alt:
            res["alt"] = alt
    return res


def points_from_parquet():
    import pandas as pd
    df = pd.read_parquet(PARQUET, columns=["lat", "lon"]).dropna()
    pts = {}
    for lat, lon in zip(df["lat"], df["lon"]):
        if km(lat, lon, *NVK) <= MAX_KM_FROM_NVK:
            pts[point_key(lat, lon)] = (float(lat), float(lon))
    return pts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="заново скачать маршруты из OSM")
    ap.add_argument("--check", help="разобрать одну точку: lat,lon")
    args = ap.parse_args()
    t0 = time.time()
    routes = prepare(load_routes(args.refresh))
    if not routes:
        print("⚠️  не нашёл маршрутов у кампуса — transit.json не меняю")
        return 1
    if args.check:
        lat, lon = map(float, args.check.split(","))
        print(json.dumps(best_trip(lat, lon, routes), ensure_ascii=False))
        return 0
    pts = points_from_parquet()
    res = {k: best_trip(lat, lon, routes) for k, (lat, lon) in pts.items()}
    payload = {"_meta": {"calc": datetime.now().strftime("%Y-%m-%d %H:%M"),
                         "how": f"оценка: пешком {WALK_KMH} км/ч ×{DETOUR}, ожидание {WAIT_MIN} мин, "
                                f"автобус {BUS_KMH} км/ч; маршруты OSM; без пересадок и пробок"},
               "points": res}
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)
    ok = sum(1 for v in res.values() if v["min"] is not None)
    print(f"🚌 время в пути (оценка): {ok} из {len(res)} точек в {MAX_KM_FROM_NVK} км от НВК, "
          f"без прямого транспорта — {len(res) - ok}; {OUT.name}, {time.time() - t0:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
