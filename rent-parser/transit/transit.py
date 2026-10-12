"""Примерное время в пути до НВК, старого корпуса РТФ и фитнеса — отдельный скрипт, базу не трогает.

Читает координаты из data/listings.parquet (только чтение), пишет data/transit.json:
  {"_meta": {"how": "…", "dests": {"nvk": {"name", "pt", "w"}, …}},
   "points": {"56.8121,60.6310": {"min": 41, "walk": 12, "ride": 22, "wait": 7, "bus": "авт. 56", "alt": […],
                                  "transfers": 0, "dest": {"rtf": {…}, "gym": {…}}, "wavg": 38}, …}}
Верхний уровень точки — путь до НВК (как раньше); dest — до остальных точек; wavg — среднее по весам DESTS.
Просмотрщик (export_viewer.py) подхватывает этот файл, если он есть; нет файла — карта как раньше.

Маршруты — из OpenStreetMap (Overpass, без ключа): все автобусы, троллейбусы, трамваи и метро города с порядком
остановок. Расписаний и пробок в OSM нет, поэтому это ОЦЕНКА — кратчайший путь по сети (Дейкстра) с пересадками:
  пешком до остановки (по прямой × DETOUR, WALK_KMH) + ожидание на каждой посадке (WAIT)
  + езда по остановкам (по прямой между соседними × DETOUR_RIDE, SPEED по виду транспорта)
  + пересадки пешком до TRANSFER_KM + пешком от остановки до цели.

  python3 rent-parser/transit/transit.py            # посчитать (маршруты OSM обновляются раз в неделю)
  python3 rent-parser/transit/transit.py --refresh  # заново скачать маршруты
  python3 rent-parser/transit/transit.py --check 56.8121,60.631   # разбор одной точки
"""
import argparse
import heapq
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

# Куда ездим и как часто (вес — доля поездок; «среднее время» = Σ вес × время / Σ весов)
DESTS = {
    "nvk": {"name": "НВК", "addr": "ул. 100-летия Уральского университета, 5", "pt": (56.7735, 60.7607), "w": 6},
    "rtf": {"name": "Старый корпус РТФ", "addr": "ул. Мира, 32", "pt": (56.8408, 60.6508), "w": 1},
    "gym": {"name": "Фитнес", "addr": "ул. Фурманова / ул. 8 Марта (≈, точка по скриншоту)", "pt": (56.8157, 60.6050), "w": 1},
}
NVK = DESTS["nvk"]["pt"]
MAX_KM_FROM_NVK = 15            # дальше не считаем
MAX_WALK_KM = 1.5               # до первой остановки и от последней (по прямой)
DIRECT_WALK_KM = 4              # пешком целиком — если не дальше этого
TRANSFER_KM = 0.4               # пересадка пешком
WALK_KMH = 4.5
DETOUR = 1.3                    # пешком по улицам длиннее, чем по прямой
DETOUR_RIDE = 1.2
SPEED = {"bus": 20, "trolleybus": 18, "tram": 17, "subway": 40, "light_rail": 30, "share_taxi": 22, "minibus": 22}
WAIT = {"subway": 4}            # ожидание на посадке, мин; остальные — WAIT_DEFAULT (интервалов в OSM нет)
WAIT_DEFAULT = 7
ROUTES_MAX_AGE_DAYS = 7
BBOX = "56.70,60.40,56.97,60.90"   # Екатеринбург
OVERPASS = ["https://overpass-api.de/api/interpreter",          # основной и зеркала: бывает 504 при нагрузке
            "https://overpass.private.coffee/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
ROUTE_KINDS = "bus|trolleybus|tram|subway|light_rail|share_taxi|minibus"
KIND_LABEL = {"bus": "авт.", "trolleybus": "тролл.", "tram": "трамв.", "subway": "метро", "light_rail": "трамв.",
              "share_taxi": "марш.", "minibus": "марш."}


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
    q = f"""[out:json][timeout:180];
rel[type=route][route~"^({ROUTE_KINDS})$"]({BBOX});
out body;
node(r);
out skel qt;"""
    d, err = None, None
    for url in [u for _ in range(3) for u in OVERPASS]:   # 3 круга по зеркалам с паузой
        if err:
            time.sleep(15)
        req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(),
                                     headers={"User-Agent": "student-nvk-transit/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                d = json.load(r)
            break
        except Exception as e:
            err = e
            log(f"⚠️  {url.split('/')[2]}: {e}")
    if d is None:
        raise err
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in d["elements"] if e["type"] == "node"}
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
            lat, lon = nodes[m["ref"]]
            if stops and km(lat, lon, stops[-1][0], stops[-1][1]) < 0.06:
                continue    # stop + platform одной остановки
            stops.append([lat, lon])
        if len(stops) >= 2:
            routes.append({"ref": t.get("ref", "?"), "kind": t.get("route", "bus"), "name": t.get("name", ""),
                           "stops": stops})
    log(f"🚌 OSM: маршрутов по городу — {len(routes)}")
    return routes


def load_routes(refresh=False, log=print):
    if not refresh and ROUTES_CACHE.exists():
        c = json.loads(ROUTES_CACHE.read_text(encoding="utf-8"))
        age = (datetime.now() - datetime.fromisoformat(c["fetched"])).days
        if age < ROUTES_MAX_AGE_DAYS and c.get("bbox") == BBOX:
            return c["routes"]
    try:
        routes = fetch_routes(log)
    except Exception as e:  # сеть/Overpass недоступны — работаем по старому кэшу, если он есть
        if ROUTES_CACHE.exists():
            log(f"⚠️  Overpass недоступен ({e}) — беру маршруты из кэша")
            return json.loads(ROUTES_CACHE.read_text(encoding="utf-8"))["routes"]
        raise
    ROUTES_CACHE.write_text(json.dumps({"fetched": datetime.now().isoformat(timespec="seconds"), "bbox": BBOX,
                                        "routes": routes}, ensure_ascii=False), encoding="utf-8")
    return routes


# ---------- сеть: остановки и «вагоны» ----------
class Grid:
    """Поиск остановок рядом с точкой (ячейки ~1 км)."""
    def __init__(self, pts):
        self.pts, self.cells = pts, {}
        for i, (lat, lon) in enumerate(pts):
            self.cells.setdefault(self._c(lat, lon), []).append(i)

    @staticmethod
    def _c(lat, lon):
        return int(lat / 0.009), int(lon / 0.0165)

    def near(self, lat, lon, radius_km):
        cy, cx = self._c(lat, lon)
        r = int(radius_km) + 1
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                for i in self.cells.get((cy + dy, cx + dx), ()):
                    d = km(lat, lon, *self.pts[i])
                    if d <= radius_km:
                        yield i, d


class Network:
    def __init__(self, routes):
        # в OSM у части маршрутов только одно направление или второе недорисовано (66: 16 и 8 остановок) —
        # добавляем самое полное направление наоборот
        refs = {}
        for r in routes:
            refs.setdefault((r["kind"], r["ref"]), []).append(r)
        extra = []
        for v in refs.values():
            full = max(v, key=lambda r: len(r["stops"]))
            if len(v) == 1 or min(len(r["stops"]) for r in v) < 0.6 * len(full["stops"]):
                extra.append({**full, "stops": full["stops"][::-1]})
        routes = routes + extra
        self.routes = routes
        # остановки: одна координата (до ~10 м) — одна остановка
        idx, self.stops = {}, []
        self.route_stops = []
        for r in routes:
            ids = []
            for lat, lon in r["stops"]:
                k = (round(lat, 4), round(lon, 4))
                if k not in idx:
                    idx[k] = len(self.stops)
                    self.stops.append((lat, lon))
                ids.append(idx[k])
            self.route_stops.append(ids)
        self.grid = Grid(self.stops)
        # на какой остановке какие маршруты (route, позиция)
        self.at_stop = [[] for _ in self.stops]
        for ri, ids in enumerate(self.route_stops):
            for i, s in enumerate(ids):
                self.at_stop[s].append((ri, i))
        self.transfers = [[(j, walk_min(d)) for j, d in self.grid.near(*p, TRANSFER_KM) if j != s]
                          for s, p in enumerate(self.stops)]
        self.ride = []
        for ri, ids in enumerate(self.route_stops):
            kind = routes[ri]["kind"]
            v = SPEED.get(kind, 20)
            self.ride.append([km(*self.stops[a], *self.stops[b]) * DETOUR_RIDE / v * 60 for a, b in zip(ids, ids[1:])])

    def label(self, ri):
        r = self.routes[ri]
        return f"{KIND_LABEL.get(r['kind'], '')} {r['ref']}".strip()

    def to_dest(self, dest):
        """Обратная Дейкстра: для каждой остановки — минуты до цели и следующий шаг.
        Узлы: ("P", s) — стою на остановке s; ("V", ri, i) — еду маршрутом ri, сейчас у его i-й остановки."""
        dist, nxt, heap = {}, {}, []
        for s, d in self.grid.near(*dest, MAX_WALK_KM):
            node, t = ("P", s), walk_min(d)
            if t < dist.get(node, 1e9):
                dist[node], nxt[node] = t, ("dest", d)
                heapq.heappush(heap, (t, node))
        while heap:
            t, u = heapq.heappop(heap)
            if t > dist.get(u, 1e9):
                continue
            preds = []
            if u[0] == "P":
                s = u[1]
                preds += [(("V", ri, i), 0.0, ("alight",)) for ri, i in self.at_stop[s]]
                preds += [(("P", j), w, ("walk", s)) for j, w in self.transfers[s]]
            else:
                _, ri, i = u
                if i > 0:
                    preds.append((("V", ri, i - 1), self.ride[ri][i - 1], ("ride",)))
                wait = WAIT.get(self.routes[ri]["kind"], WAIT_DEFAULT)
                preds.append((("P", self.route_stops[ri][i]), wait, ("board", ri)))
            for v, c, how in preds:
                nt = t + c
                if nt < dist.get(v, 1e9):
                    dist[v], nxt[v] = nt, (u, how)
                    heapq.heappush(heap, (nt, v))
        return dist, nxt

    def path(self, start, dist, nxt):
        """Разбор пути от остановки start до цели: пешком/ожидание/езда, маршруты."""
        walk = wait = ride = 0.0
        legs, u = [], ("P", start)
        while True:
            step = nxt[u]
            if step[0] == "dest":
                walk += walk_min(step[1])
                break
            v, how = step
            c = dist[u] - dist[v]
            if how[0] == "board":
                wait += c
                legs.append(self.label(how[1]))
            elif how[0] == "ride":
                ride += c
            elif how[0] == "walk":
                walk += c
            u = v
        return walk, wait, ride, legs


def trip(lat, lon, dest, net, table):
    dist, nxt = table
    direct = km(lat, lon, *dest)
    options = []
    if direct <= DIRECT_WALK_KM:
        w = walk_min(direct)
        options.append((w, {"min": w, "walk": w, "ride": 0, "wait": 0, "bus": None, "transfers": 0}))
    for s, d in net.grid.near(lat, lon, MAX_WALK_KM):
        if ("P", s) in dist:
            options.append((walk_min(d) + dist[("P", s)], s, d))
    if not options:
        return {"min": None, "note": "нет транспорта в пределах 1,5 км — уточнить"}
    options.sort(key=lambda o: o[0])
    best, out, alts = options[0], None, []
    for o in options:
        if len(o) == 2:
            continue
        walk, wait, ride, legs = net.path(o[1], dist, nxt)
        if not legs:
            continue        # «доехать» без посадки — это пешком, уже учтено
        label = " → ".join(legs)
        rec = {"min": o[0], "walk": walk_min(o[2]) + walk, "ride": ride, "wait": wait, "bus": label,
               "transfers": len(legs) - 1}
        if out is None:
            out = rec
        elif o[0] <= out["min"] + 7 and label != out["bus"] and label not in alts and len(alts) < 2:
            alts.append(label)
        if out and o[0] > out["min"] + 7:
            break
    if len(best) == 2 and (out is None or best[0] <= out["min"]):
        out, alts = best[1], []
    if out is None:
        return {"min": None, "note": "нет транспорта в пределах 1,5 км — уточнить"}
    res = {k: (round(v) if isinstance(v, float) else v) for k, v in out.items()}
    if alts:
        res["alt"] = alts
    return res


def points_from_parquet():
    import pandas as pd
    df = pd.read_parquet(PARQUET, columns=["lat", "lon"]).dropna()
    pts = {}
    for lat, lon in zip(df["lat"], df["lon"]):
        if km(lat, lon, *NVK) <= MAX_KM_FROM_NVK:
            pts[point_key(lat, lon)] = (float(lat), float(lon))
    return pts


def all_trips(lat, lon, net, tables):
    res = trip(lat, lon, NVK, net, tables["nvk"])
    res["dest"] = {k: trip(lat, lon, d["pt"], net, tables[k]) for k, d in DESTS.items() if k != "nvk"}
    times = [(d["w"], res if k == "nvk" else res["dest"][k]) for k, d in DESTS.items()]
    if all(t["min"] is not None for _, t in times):
        res["wavg"] = round(sum(w * t["min"] for w, t in times) / sum(w for w, _ in times))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="заново скачать маршруты из OSM")
    ap.add_argument("--check", help="разобрать одну точку: lat,lon")
    args = ap.parse_args()
    t0 = time.time()
    routes = load_routes(args.refresh)
    if not routes:
        print("⚠️  маршрутов нет — transit.json не меняю")
        return 1
    net = Network(routes)
    tables = {k: net.to_dest(d["pt"]) for k, d in DESTS.items()}
    if args.check:
        lat, lon = map(float, args.check.split(","))
        print(json.dumps(all_trips(lat, lon, net, tables), ensure_ascii=False, indent=1))
        return 0
    pts = points_from_parquet()
    res = {k: all_trips(lat, lon, net, tables) for k, (lat, lon) in pts.items()}
    waits = ", ".join(f"{KIND_LABEL[k]} {v}" for k, v in WAIT.items())
    payload = {"_meta": {"calc": datetime.now().strftime("%Y-%m-%d %H:%M"),
                         "how": f"оценка: пешком {WALK_KMH} км/ч ×{DETOUR}, ожидание {WAIT_DEFAULT} мин ({waits}), "
                                f"автобус ~{SPEED['bus']} км/ч, трамвай ~{SPEED['tram']}, метро ~{SPEED['subway']}; "
                                f"маршруты OSM, пересадки до {int(TRANSFER_KM * 1000)} м; без расписаний и пробок",
                         "dests": {k: {"name": d["name"], "addr": d["addr"], "pt": list(d["pt"]), "w": d["w"]}
                                   for k, d in DESTS.items()}},
               "points": res}
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)
    ok = sum(1 for v in res.values() if v["min"] is not None)
    print(f"🚌 время в пути (оценка): до НВК — {ok} из {len(res)} точек в {MAX_KM_FROM_NVK} км, "
          f"среднее по 3 точкам — {sum(1 for v in res.values() if 'wavg' in v)}; {OUT.name}, {time.time() - t0:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
