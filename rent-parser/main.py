#!/usr/bin/env python3
"""rent-parser: сбор аренды по всему Екатеринбургу → data/listings.parquet → map_all.html.

Запуск (из корня проекта):
    ./rent-parser/run.sh                 # обычный прогон: только новое (по дате, пока есть новые ID)
    ./rent-parser/run.sh --full          # полный проход всех страниц (первый раз / раз в неделю)
    ./rent-parser/run.sh --fast          # без браузера: Яндекс + ВК/Telegram (~1–2 мин)
    ./rent-parser/run.sh --only avito    # один источник: yandex, avito, domclick, social
    ./rent-parser/run.sh --geocode-only  # только догеокодировать адреса без координат

Источник, которого ещё нет в базе, автоматически проходится полностью.
Пропавшие объявления помечаются только после полного прохода источника.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RP = Path(__file__).resolve().parent
sys.path[:0] = [str(RP), str(RP / "scrapers"), str(RP / "pipeline")]

from config import ROOT, TMP, ZONE_KEYWORDS  # noqa: E402
from export_viewer import export_to_viewer, load_statuses  # noqa: E402
from geocode import addr_key, geocode_records  # noqa: E402
from social import scrape_telegram, scrape_vk  # noqa: E402
from to_parquet import DEFAULT_PARQUET, family, known_map, load_parquet, upsert_listings  # noqa: E402
from yandex import SEARCHES, scrape_yandex  # noqa: E402

SOURCES = ["yandex", "social", "domclick", "avito"]
FAMILY = {"yandex": "Y", "avito": "A", "domclick": "D"}


def log(msg):
    print(msg, flush=True)


def backup_old_schema():
    """База предыдущей версии парсера (залог Аренды = 0, комиссия % как рубли) — в архив, собираем заново."""
    if not DEFAULT_PARQUET.exists():
        return
    df = load_parquet(DEFAULT_PARQUET)
    if "commission" in df.columns and df["commission_rub"].isna().all():
        bak = DEFAULT_PARQUET.with_name(f"listings.parquet.bak-{datetime.now():%Y%m%d-%H%M}")
        shutil.move(DEFAULT_PARQUET, bak)
        log(f"📦 Старая база (прошлая версия парсера, с ошибками в залоге/комиссии) → {bak.name}")


def run_node(script, mode, known, extra=()):
    TMP.mkdir(exist_ok=True)
    stamp = f"{script.split('.')[0]}_{datetime.now():%H%M%S}"
    known_file, out_file = TMP / f"known_{stamp}.json", TMP / f"out_{stamp}.json"
    known_file.write_text(json.dumps(known, ensure_ascii=False))
    cmd = ["node", str(RP / "scrapers" / script), "--mode", mode, "--known", str(known_file),
           "--out", str(out_file), *extra]
    log(f"🚀 {script} --mode {mode}  (откроется окно Chrome; если появится капча — реши её там)")
    # stdio не перехватываем: прогресс и вопросы про капчу видны в терминале
    proc = subprocess.run(cmd, cwd=RP / "scrapers")
    known_file.unlink(missing_ok=True)
    if proc.returncode != 0 or not out_file.exists():
        log(f"❌ {script}: завершился с ошибкой (код {proc.returncode})")
        return [], False
    data = json.loads(out_file.read_text())
    out_file.unlink(missing_ok=True)
    return data.get("items", []), bool(data.get("complete"))


def main():
    ap = argparse.ArgumentParser(description="Сбор аренды в Екатеринбурге")
    ap.add_argument("--full", action="store_true", help="полный проход всех страниц всех источников")
    ap.add_argument("--fast", action="store_true", help="без браузера (Яндекс + ВК/Telegram)")
    ap.add_argument("--only", help="через запятую: " + ",".join(SOURCES))
    ap.add_argument("--geocode-limit", type=int, default=100, help="запросов к геокодеру за прогон (~4 с каждый)")
    ap.add_argument("--geocode-only", action="store_true", help="только догеокодировать адреса без координат (без лимита)")
    ap.add_argument("--max-enrich", type=int, help="сколько карточек Авито открыть за прогон")
    args = ap.parse_args()

    t0 = time.time()
    log("=" * 60 + f"\n🏠 rent-parser · {datetime.now():%d.%m.%Y %H:%M}\n" + "=" * 60)
    backup_old_schema()
    db = load_parquet(DEFAULT_PARQUET)
    have = {f: int((db["id"].map(family) == f).sum()) for f in ("Y", "A", "D", "TG", "VK")}
    only = set(args.only.split(",")) if args.only else set(SOURCES)
    if args.geocode_only:
        only = set()
        args.geocode_limit = 100_000
    if args.fast:
        only -= {"avito", "domclick"}
    wants = {rid for rid, (st, _) in load_statuses().items() if st == "Интересно"}

    def mode_for(fam):
        return "full" if args.full or have.get(fam, 0) == 0 else "inc"

    records, complete, summary = [], set(), []

    if "yandex" in only:
        m = mode_for("Y")
        log(f"\n--- Яндекс Недвижимость + Яндекс Аренда ({m}) ---")
        known = set(db.loc[db["id"].map(family) == "Y", "id"])
        items, done = scrape_yandex(mode=m, known_ids=known, log=log)
        records += items
        if m == "full" and len(done) == len(SEARCHES):
            complete.add("Y")
        summary.append(("Яндекс", len(items), m))

    if "social" in only:
        m = "full" if args.full or have["TG"] + have["VK"] == 0 else "inc"
        log(f"\n--- Telegram + ВКонтакте ({m}) ---")
        known = set(db.loc[db["id"].map(family).isin(["TG", "VK"]), "id"])
        tg = scrape_telegram(mode=m, known_ids=known, log=log)
        vk = scrape_vk(mode=m, known_ids=known, log=log)
        records += tg + vk
        summary.append(("ВК/TG", len(tg) + len(vk), m))

    if "domclick" in only:
        m = mode_for("D")
        log(f"\n--- Домклик ({m}) ---")
        items, ok = run_node("domclick.js", m, known_map(db, "D"))
        records += items
        if m == "full" and ok:
            complete.add("D")
        summary.append(("Домклик", len(items), m))

    if "avito" in only:
        m = mode_for("A")
        log(f"\n--- Авито ({m}) ---")
        extra = ["--max-enrich", str(args.max_enrich)] if args.max_enrich is not None else []
        items, ok = run_node("avito.js", m, known_map(db, "A", wants), extra)
        records += items
        if m == "full" and ok:
            complete.add("A")
        summary.append(("Авито", len(items), m))

    log("\n--- Сохранение ---")
    stats = upsert_listings(records, complete_families=complete)
    if records:   # журнал прогонов: от него считается «новое с прошлого прогона»
        runs_file = DEFAULT_PARQUET.with_name("runs.json")
        try:
            runs = json.loads(runs_file.read_text())
        except (OSError, ValueError):
            runs = []
        runs.append({"at": stats["at"], "sources": sorted(only), "new": stats["new"], "full": sorted(complete)})
        runs_file.write_text(json.dumps(runs[-200:], ensure_ascii=False, indent=1))
    db = load_parquet(DEFAULT_PARQUET)

    # Координаты для объявлений без них (Авито, старые хвосты): сначала дома с координатами сайтов
    rows = db.to_dict("records")
    known_coords = {addr_key(r["address"]): (r["lat"], r["lon"]) for r in rows
                    if r.get("geo_source") == "сайт" and r.get("address") and r.get("lat") == r.get("lat")
                    and r.get("lat") is not None and addr_key(r["address"])}
    todo = [r for r in rows if r.get("source_type") != "social" and r.get("is_active") is not False
            and r.get("address")
            and (r.get("lat") is None or r.get("lat") != r.get("lat")
                 or str(r.get("geo_source") or "").startswith("приблиз"))]   # «по улице» — пробуем уточнить
    for r in todo:
        r["lat"] = None
    # Сначала улицы зон НВК, потом то, что в бюджете; остальной город — в следующие прогоны
    zone_rx = re.compile("|".join(rx for _, rx in ZONE_KEYWORDS), re.I)
    todo.sort(key=lambda r: (not zone_rx.search(r.get("address") or ""), (r.get("price") or 1e9) > 60_000))
    if todo:
        log(f"\n--- Геокодер: без координат {len(todo)} ---")
        geocode_records(todo, known_coords, limit=args.geocode_limit, log=log)
        upd = {r["id"]: r for r in todo if r.get("lat") is not None}
        if upd:
            mask = db["id"].isin(list(upd))
            db.loc[mask, "lat"] = db.loc[mask, "id"].map(lambda i: upd[i]["lat"])
            db.loc[mask, "lon"] = db.loc[mask, "id"].map(lambda i: upd[i]["lon"])
            db.loc[mask, "geo_source"] = db.loc[mask, "id"].map(lambda i: upd[i]["geo_source"])
            db.to_parquet(DEFAULT_PARQUET, compression="snappy", index=False)

    log("\n" + "=" * 60)
    for name, n, m in summary:
        log(f"  {name:8s} собрано {n:5d}  ({'полный проход' if m == 'full' else 'только новое'})")
    log(f"  В базе: {len(db)} · новых {stats['new']} · подешевело/подорожало {stats['price_changed']} · "
        f"пропало {stats['gone']} · вернулось {stats['back']}")
    active = db[db["is_active"] != False]  # noqa: E712
    log("  Активных по источникам: " + ", ".join(f"{k}: {v}" for k, v in active["source"].value_counts().items()))
    log(f"  Время: {round((time.time() - t0) / 60, 1)} мин")
    log("=" * 60)
    out = export_to_viewer(log=log)
    if out:
        log(f"Открыть: open {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
