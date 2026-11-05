"""Full pipeline: discover all pages from seed, skip existing, process new only."""
from __future__ import annotations

import asyncio
import logging
import re
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
    ScraperConfig, discover_new_properties, process_url, extract_fast_path,
    LOGGER,
)
from mongo_store import MongoStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

SEED_URL = (
    "https://www.yapo.cl/searchresult/bienes-raices-venta-de-propiedades"
    "?regionslug=region-metropolitana-santiago"
    "&q=withcat.bienes-raices-venta-de-propiedades-apartamentos,"
    "bienes-raices-venta-de-propiedades-casas|f_price.120000000-|f_currency.CLP"
)
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")


def _extract_lid(url: str) -> str:
    m = re.search(r'/(\d{6,})$', url.rstrip("/"))
    return m.group(1) if m else ""


def main():
    client = MongoClient(cfg.mongo_uri)
    col = client[cfg.mongo_db][cfg.mongo_collection]
    store = MongoStore(config=cfg)
    store.connect()

    # ── State BEFORE ────────────────────────────────────────────────
    total_before = col.count_documents({})
    states_before = {}
    for s in col.distinct("classification.final_state"):
        states_before[s] = col.count_documents({"classification.final_state": s})

    existing_ids = set()
    for d in col.find({}, {"listing_id": 1}):
        lid = d.get("listing_id")
        if lid:
            existing_ids.add(str(lid))

    print(f"\n{'='*60}")
    print(f"RUN ID: {RUN_ID}")
    print(f"Seed: {SEED_URL[:90]}...")
    print(f"Mongo total antes: {total_before}")
    print(f"listing_ids existentes: {len(existing_ids)}")
    print(f"{'='*60}\n")

    # ── DISCOVERY: all pages ────────────────────────────────────────
    scraper_cfg = ScraperConfig(
        base_url=SEED_URL,
        max_pages=100,
        max_urls_per_session=5000,
    )

    discovered = asyncio.run(
        discover_new_properties(
            scraper_cfg,
            base_url=SEED_URL,
            max_pages=100,
            max_urls_per_session=5000,
        )
    )

    print(f"\n[DISCOVERY] total encontradas: {len(discovered)}")

    # Separate new vs existing
    new_urls: list[str] = []
    existing_skip = 0
    for url in discovered:
        lid = _extract_lid(url)
        if lid and lid in existing_ids:
            existing_skip += 1
        else:
            new_urls.append(url)

    print(f"  nuevas: {len(new_urls)}")
    print(f"  ya existentes: {existing_skip}")

    if not new_urls:
        print("\nNo hay URLs nuevas. Discovery completo agotado.")
        return

    # ── PROCESS new URLs ────────────────────────────────────────────
    print(f"\n[PROCESS] {len(new_urls)} URLs nuevas...")
    start_ts = time.time()
    processed = 0
    inserted = 0
    failed = 0
    cache_hits = 0
    downloads = 0
    classification_counts = {}
    deepseek_used = 0
    ds_invalid = 0
    src_counts = {}

    for i, url in enumerate(new_urls):
        lid = _extract_lid(url)
        try:
            # Check if local HTML exists
            html_dir = Path(__file__).resolve().parent / "html_dumps"
            import hashlib
            md5 = hashlib.md5(url.encode()).hexdigest()
            local_html = html_dir / f"{md5}.html"

            if local_html.exists():
                cache_hits += 1
                LOGGER.info("[CACHE_HIT] listing_id=%s html_path=%s", lid, local_html.name)
            else:
                downloads += 1
                LOGGER.info("[DOWNLOAD] listing_id=%s reason=new_listing_no_local_html", lid)

            result = process_url(url, scraper_cfg, force_refresh=False)
            result["run_id"] = RUN_ID
            store.upsert_listing(result)
            inserted += 1
            processed += 1

            cls = result.get("classification", {})
            fs = cls.get("final_state", "N/A")
            classification_counts[fs] = classification_counts.get(fs, 0) + 1
            if cls.get("ai_used"):
                deepseek_used += 1
            if "Invalid JSON from LLM" in cls.get("reason", ""):
                ds_invalid += 1
            ds = cls.get("decision_source", "unknown")
            src_counts[ds] = src_counts.get(ds, 0) + 1

            if (i + 1) % 25 == 0 or i == len(new_urls) - 1:
                elapsed = time.time() - start_ts
                rate = (i + 1) / (elapsed / 60) if elapsed > 0 else 0
                mongo_total = col.count_documents({})
                LOGGER.info(
                    "[PROGRESS] processed=%d/%d inserted=%d failed=%d cache=%d dl=%d "
                    "elapsed=%.0fs rate=%.1f/min mongo_total=%d",
                    i + 1, len(new_urls), inserted, failed, cache_hits, downloads,
                    elapsed, rate, mongo_total,
                )

        except Exception as e:
            LOGGER.error("[ERROR] listing_id=%s url=%s: %s", lid, url[:60], e)
            failed += 1

    elapsed = time.time() - start_ts

    # ── State AFTER ─────────────────────────────────────────────────
    total_after = col.count_documents({})
    states_after = {}
    for s in col.distinct("classification.final_state"):
        states_after[s] = col.count_documents({"classification.final_state": s})

    print(f"\n{'='*60}")
    print(f"RESUMEN FINAL — run_id={RUN_ID}")
    print(f"{'='*60}")

    print(f"\nANTES:")
    print(f"  Mongo total: {total_before}")
    for s in ["CORREDOR_SEGURO", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO", "AD_REMOVED"]:
        print(f"    {s}: {states_before.get(s, 0)}")

    print(f"\nDISCOVERY:")
    print(f"  Páginas recorridas: hasta agotar paginación")
    print(f"  URLs encontradas: {len(discovered)}")
    print(f"  Ya existentes en Mongo: {existing_skip}")
    print(f"  Nuevas únicas: {len(new_urls)}")

    print(f"\nPROCESAMIENTO:")
    print(f"  Procesadas nuevas: {processed}")
    print(f"  Insertadas/upserted: {inserted}")
    print(f"  Fallidas: {failed}")
    print(f"  Usaron HTML local: {cache_hits}")
    print(f"  Descargadas nuevas: {downloads}")
    print(f"  Tiempo: {elapsed:.0f}s ({inserted/(elapsed/60):.1f}/min)")

    print(f"\nDESPUÉS:")
    print(f"  Mongo total: {total_after}")
    for s in ["CORREDOR_SEGURO", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO", "AD_REMOVED"]:
        print(f"    {s}: {states_after.get(s, 0)}")

    print(f"\nCLASIFICACIÓN NUEVA CORRIDA:")
    for s in ["CORREDOR_SEGURO", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO", "AD_REMOVED"]:
        print(f"    {s}: {classification_counts.get(s, 0)}")

    print(f"\nAUDITORÍA:")
    print(f"  DeepSeek usado: {deepseek_used}")
    print(f"  Invalid JSON DeepSeek: {ds_invalid}")
    for src in ["seller_avatar_alt", "seller_name", "title", "description", "deepseek", ""]:
        label = src if src else "NONE"
        print(f"  Clasificados por {label}: {src_counts.get(src, 0)}")


if __name__ == "__main__":
    main()
