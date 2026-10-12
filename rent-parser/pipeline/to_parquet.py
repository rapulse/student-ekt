"""База объявлений data/listings.parquet: слияние прогонов без потери данных.

* Новое значение поля заменяет старое, только если оно известно (не None/""): то, что дозагрузили
  из карточки раньше (адрес, координаты, продавец), не затирается беглым чтением выдачи.
* Яндекс Аренда «липкая»: если объявление хоть раз опознано как Аренда, так и остаётся.
* Цена: при изменении пишется история price_history ([[дата, цена], …]) и prev_price.
* Пропавшие: только после ПОЛНОГО прохода источника (complete) — иначе нельзя отличить
  «снято» от «не дошли до этой страницы». Помечаются is_active = False и inactive_since.
"""
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from config import DATA

DEFAULT_PARQUET = DATA / "listings.parquet"

COLS = [
    "id", "source", "source_type", "url", "title", "category",
    "price", "prev_price", "price_history",
    "deposit", "commission_pct", "commission_rub", "utilities_included", "utilities_rub", "utilities_info",
    "rooms", "area", "floor", "address", "district", "metro", "lat", "lon", "geo_source",
    "author_type", "author_name", "seller_stats", "badges", "hits", "description",
    "published", "updated_on_site", "views", "enriched", "closed_on_site",
    "first_seen", "first_seen_at", "last_seen", "is_active", "inactive_since", "raw_json",
]
NUM = {"price", "prev_price", "deposit", "commission_pct", "commission_rub", "utilities_rub", "rooms", "area",
       "lat", "lon", "views"}
BOOL = {"utilities_included", "enriched", "closed_on_site", "is_active"}
STICKY_TRUE = {"enriched"}


def family(rid):
    """Источник по префиксу ID: Y — Яндекс, A — Авито, D — Домклик, C — ЦИАН, TG_/VK_ — посты."""
    for p in ("TG_", "VK_", "Y", "A", "D", "C"):
        if rid.startswith(p):
            return p.rstrip("_")
    return "?"


def load_parquet(path=DEFAULT_PARQUET) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=COLS)
    df = pd.read_parquet(p)
    for c in COLS:
        if c not in df.columns:
            df[c] = None
    return df


def _empty(v):
    return v is None or v == "" or (isinstance(v, float) and v != v)


def _clean(r):
    out = {}
    for c in COLS:
        v = r.get(c)
        if c in NUM and not _empty(v):
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = None
        out[c] = None if _empty(v) else v
    raw = r.get("raw_data")
    out["raw_json"] = json.dumps(raw, ensure_ascii=False) if raw is not None else r.get("raw_json")
    return out


def upsert_listings(new_records, complete_families=(), path=DEFAULT_PARQUET):
    """Возвращает статистику {new, updated, price_changed, gone}."""
    now = datetime.now()
    now_s, today = now.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d")
    old = load_parquet(path)
    base = {r["id"]: r for r in old.to_dict("records")}
    stats = {"new": 0, "updated": 0, "price_changed": 0, "gone": 0, "back": 0}
    seen = set()

    for rec in new_records:
        n = _clean(rec)
        rid = n["id"]
        if not rid or not n["url"]:
            continue
        seen.add(rid)
        o = base.get(rid)
        if o is None:
            n["first_seen"] = today
            n["first_seen_at"] = now_s          # точное время: «новые с прошлого прогона»
            n["price_history"] = json.dumps([[today, n["price"]]]) if n["price"] else "[]"
            stats["new"] += 1
            merged = n
        else:
            merged = dict(o)
            for c in COLS:
                if c in ("first_seen", "first_seen_at", "price_history", "prev_price"):
                    continue
                if c in STICKY_TRUE:
                    merged[c] = bool(o.get(c)) or bool(n.get(c))
                elif not _empty(n.get(c)):
                    merged[c] = n[c]
            if o.get("source_type") == "arenda_program" and n.get("source_type") != "arenda_program":
                merged["source_type"], merged["source"] = o["source_type"], o["source"]
                merged["author_type"], merged["author_name"] = o.get("author_type"), o.get("author_name")
            # не теряем дозагруженную карточку в raw_json
            try:
                oraw, nraw = json.loads(o.get("raw_json") or "{}"), json.loads(n.get("raw_json") or "{}")
                if isinstance(oraw, dict) and isinstance(nraw, dict) and "item_page" in oraw and "item_page" not in nraw:
                    nraw["item_page"] = oraw["item_page"]
                    merged["raw_json"] = json.dumps(nraw, ensure_ascii=False)
            except ValueError:
                pass
            if n["price"] and not _empty(o.get("price")) and float(o["price"]) != n["price"]:
                hist = json.loads(o.get("price_history") or "[]")
                hist.append([today, n["price"]])
                merged["price_history"] = json.dumps(hist)
                merged["prev_price"] = float(o["price"])
                stats["price_changed"] += 1
            if o.get("is_active") is False:
                stats["back"] += 1
            stats["updated"] += 1
        merged["last_seen"] = now_s
        merged["is_active"] = not bool(merged.get("closed_on_site"))
        merged["inactive_since"] = None if merged["is_active"] else (merged.get("inactive_since") or today)
        base[rid] = merged

    for rid, r in base.items():
        if rid in seen or family(rid) not in complete_families:
            continue
        if r.get("is_active") is not False:
            r["is_active"] = False
            r["inactive_since"] = today
            stats["gone"] += 1

    df = pd.DataFrame(list(base.values()), columns=COLS)
    for c in NUM:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in BOOL:
        df[c] = df[c].map(lambda v: None if _empty(v) else bool(v)).astype("object")
    for c in set(COLS) - NUM - BOOL:
        df[c] = df[c].map(lambda v: None if _empty(v) else str(v)).astype("object")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, compression="snappy", index=False)
    stats["at"] = now_s
    return stats


def known_map(df, family_code=None, wants=frozenset()):
    """Для браузерных сборщиков: {id: {enriched, want, url, source, source_type, title}}."""
    out = {}
    for r in df.to_dict("records"):
        if family_code and family(r["id"]) != family_code:
            continue
        out[r["id"]] = {"enriched": bool(r.get("enriched")), "want": r["id"] in wants, "url": r.get("url"),
                        "source": r.get("source"), "source_type": r.get("source_type"), "title": r.get("title")}
    return out
