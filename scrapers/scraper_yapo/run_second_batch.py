"""Second controlled batch: discover new URLs from the same seed, skip duplicates, process only new ones."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import load_env_file, AppConfig
load_env_file()
cfg = AppConfig()

from pymongo import MongoClient
from scraping_yapo_proxys import (
    ScraperConfig, discover_new_properties, process_url,
    _normalize_url, LOGGER, _now_iso,
)
from mongo_store import MongoStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

# ── Config ──────────────────────────────────────────────────────────────
SEED_URL = (
    "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades"
    "?regionslug=region-metropolitana-la-florida"
    "&q=withcat.bienes-raices-venta-de-propiedades-apartamentos,"
    "bienes-raices-venta-de-propiedades-casas|f_price.160000000-"
)
TARGET_NEW = 500
CANARY = False  # set True for 50-URL test

import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--canary", action="store_true", help="Run 50-URL canary first")
args = parser.parse_args()
if args.canary:
    CANARY = True
    TARGET_NEW = 50

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")


def main():
    client = MongoClient(cfg.mongo_uri)
    col = client[cfg.mongo_db][cfg.mongo_collection]
    store = MongoStore(config=cfg)
    store.connect()

    # ── State before ───────────────────────────────────────────────────
    total_before = col.count_documents({})
    states_before = {}
    for s in col.distinct("classification.final_state"):
        states_before[s] = col.count_documents({"classification.final_state": s})

    print(f"\n=== SEGUNDA CORRIDA ===")
    print(f"run_id: {RUN_ID}")
    print(f"seed: {SEED_URL[:80]}...")
    print(f"target_new: {TARGET_NEW}")
    print(f"Base actual: {total_before} documentos")

    # ── Load existing listing_ids ──────────────────────────────────────
    existing_ids = set()
    for d in col.find({}, {"listing_id": 1}):
        lid = d.get("listing_id")
        if lid:
            existing_ids.add(str(lid))
    print(f"listing_ids existentes: {len(existing_ids)}")

    # ── Discovery: paginate from the seed ──────────────────────────────
    scraper_cfg = ScraperConfig(
        base_url=SEED_URL,
        max_pages=50,
        max_urls_per_session=max(TARGET_NEW * 2, 500),
    )

    discovered = asyncio.run(
        discover_new_properties(
            scraper_cfg,
            base_url=SEED_URL,
            max_pages=50,
            max_urls_per_session=max(TARGET_NEW * 2, 500),
        )
    )
    print(f"\n[DISCOVERY] total URLs encontradas: {len(discovered)}")

    # ── Extract listing_ids and filter duplicates ──────────────────────
    new_urls: list[str] = []
    skipped = 0
    for url in discovered:
        # Extract listing_id from URL (last numeric segment)
        import re
        m = re.search(r'/(\d{6,})$', url.rstrip("/"))
        lid = m.group(1) if m else ""
        if lid and lid in existing_ids:
            LOGGER.info("[SKIP_DUPLICATE] listing_id=%s already_exists=true", lid)
            skipped += 1
            continue
        new_urls.append(url)

    print(f"  nuevas: {len(new_urls)}, duplicadas saltadas: {skipped}")
    new_urls = new_urls[:TARGET_NEW]

    if not new_urls:
        print("No hay URLs nuevas para procesar.")
        return

    # ── Process new URLs ───────────────────────────────────────────────
    print(f"\n[PROCESS] procesando {len(new_urls)} URLs nuevas...")
    start_ts = time.time()
    processed = 0
    upserted = 0
    errors = 0
    classification_counts = {}

    for i, url in enumerate(new_urls):
        try:
            result = process_url(url, scraper_cfg, force_refresh=False)
            # Set fixed run_id
            result["run_id"] = RUN_ID
            result["updated_at"] = _now_iso()
            store.upsert_listing(result)
            processed += 1
            upserted += 1

            cls = result.get("classification", {})
            fs = cls.get("final_state", "N/A")
            classification_counts[fs] = classification_counts.get(fs, 0) + 1

            if (i + 1) % 25 == 0 or i == len(new_urls) - 1:
                elapsed = time.time() - start_ts
                rate = (i + 1) / (elapsed / 60) if elapsed > 0 else 0
                mongo_total = col.count_documents({})
                LOGGER.info(
                    "[PROGRESS] processed=%d/%d upserted=%d failed=%d elapsed=%.0fs rate=%.1f/min mongo_total=%d",
                    i + 1, len(new_urls), upserted, errors, elapsed, rate, mongo_total,
                )
        except Exception as e:
            LOGGER.error("[ERROR] URL=%s: %s", url[:60], e)
            errors += 1

    elapsed = time.time() - start_ts

    # ── Summary ────────────────────────────────────────────────────────
    total_after = col.count_documents({})
    states_after = {}
    for s in col.distinct("classification.final_state"):
        states_after[s] = col.count_documents({"classification.final_state": s})

    # Count classification sources for new docs
    avatar_count = 0
    title_count = 0
    description_count = 0
    deepseek_count = 0
    ds_invalid = 0
    avatar_found = 0
    for d in col.find({"run_id": RUN_ID}):
        cls = d.get("classification", {})
        ev = str(cls.get("evidence", []))
        if "avatar/logo" in ev:
            avatar_count += 1
        if "title" in ev:
            title_count += 1
        if "descripci" in ev:
            description_count += 1
        if cls.get("ai_used"):
            deepseek_count += 1
        if "Invalid JSON from LLM" in cls.get("reason", ""):
            ds_invalid += 1
        if d.get("seller_avatar_alt"):
            avatar_found += 1

    print(f"\n{'='*60}")
    print(f"RESUMEN SEGUNDA CORRIDA — run_id={RUN_ID}")
    print(f"{'='*60}")
    print(f"BASE ANTERIOR:")
    print(f"  Total antes: {total_before}")
    for s in ["CORREDOR_SEGURO", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO", "AD_REMOVED"]:
        print(f"    {s}: {states_before.get(s, 0)}")
    print()
    print(f"NUEVA CORRIDA:")
    print(f"  run_id: {RUN_ID}")
    print(f"  URLs nuevas descubiertas: {len(new_urls)}")
    print(f"  Procesadas: {processed}")
    print(f"  Insertadas/upserted: {upserted}")
    print(f"  Duplicadas saltadas: {skipped}")
    print(f"  Errores: {errors}")
    print(f"  Tiempo: {elapsed:.0f}s ({upserted/(elapsed/60):.1f}/min)")
    print(f"\n  RESULTADO NUEVA CORRIDA:")
    for s in ["CORREDOR_SEGURO", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO", "AD_REMOVED"]:
        print(f"    {s}: {classification_counts.get(s, 0)}")
    print()
    print(f"ACUMULADO MONGO:")
    print(f"  Total: {total_after}")
    for s in ["CORREDOR_SEGURO", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO", "AD_REMOVED"]:
        print(f"    {s}: {states_after.get(s, 0)}")
    print()
    print(f"DETALLE:")
    print(f"  DeepSeek usado: {deepseek_count}")
    print(f"  Invalid JSON DeepSeek: {ds_invalid}")
    print(f"  seller_avatar_alt extraidos: {avatar_found}")
    print(f"  clasificados por avatar/logo: {avatar_count}")
    print(f"  clasificados por title: {title_count}")
    print(f"  clasificados por description: {description_count}")
    print(f"  clasificados por DeepSeek: {deepseek_count}")


if __name__ == "__main__":
    main()
