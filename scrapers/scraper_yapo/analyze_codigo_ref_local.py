"""Analyze local HTML for 'Ref.: XXXXX' patterns and mark codigo_ref in Mongo."""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import load_env_file, AppConfig
load_env_file()
cfg = AppConfig()

from pymongo import MongoClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
LOGGER = logging.getLogger("codigo_ref")

# Patterns: Ref, Referencia, Código, Codigo followed by alphanumeric code
REF_PATTERN = re.compile(
    r'\b(?:Ref\.?\s*:\s*|Referencia\s*:\s*|C[oó]digo\s*:\s*)[A-Za-z0-9][A-Za-z0-9_-]{2,}',
    re.I,
)


def main():
    client = MongoClient(cfg.mongo_uri)
    col = client[cfg.mongo_db][cfg.mongo_collection]

    total = col.count_documents({})
    LOGGER.info("[START] total_docs=%d", total)

    codigo_true = 0
    codigo_false = 0
    missing_html = 0
    start_ts = time.time()

    docs = col.find({}, {"listing_id": 1, "html_path": 1})
    processed = 0

    for doc in docs:
        lid = doc.get("listing_id", "?")
        hp = doc.get("html_path", "")

        if not hp:
            codigo_false += 1
            missing_html += 1
            LOGGER.info("[MISSING_HTML] listing_id=%s html_path=empty", lid)
            col.update_one({"_id": doc["_id"]}, {"$set": {"codigo_ref": False}})
            processed += 1
            continue

        html_path = Path(hp)
        if not html_path.exists():
            codigo_false += 1
            missing_html += 1
            LOGGER.info("[MISSING_HTML] listing_id=%s html_path=%s", lid, hp)
            col.update_one({"_id": doc["_id"]}, {"$set": {"codigo_ref": False}})
            processed += 1
            continue

        try:
            html = html_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            LOGGER.warning("[READ_ERROR] listing_id=%s: %s", lid, e)
            col.update_one({"_id": doc["_id"]}, {"$set": {"codigo_ref": False}})
            codigo_false += 1
            processed += 1
            continue

        has_ref = bool(REF_PATTERN.search(html))
        col.update_one({"_id": doc["_id"]}, {"$set": {"codigo_ref": has_ref}})

        if has_ref:
            codigo_true += 1
        else:
            codigo_false += 1

        processed += 1
        if processed % 100 == 0 or processed == total:
            elapsed = time.time() - start_ts
            rate = processed / (elapsed / 60) if elapsed > 0 else 0
            LOGGER.info(
                "[SCAN] processed=%d codigo_ref_true=%d codigo_ref_false=%d missing_html=%d elapsed=%.0fs rate=%.1f/min",
                processed, codigo_true, codigo_false, missing_html, elapsed, rate,
            )

    elapsed = time.time() - start_ts
    LOGGER.info("[DONE] processed=%d codigo_ref_true=%d codigo_ref_false=%d missing_html=%d elapsed=%.0fs",
                processed, codigo_true, codigo_false, missing_html, elapsed)

    # Summary by classification
    print("\n=== codigo_ref por final_state ===")
    pipeline = [
        {"$group": {
            "_id": {"final_state": "$classification.final_state", "codigo_ref": "$codigo_ref"},
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id.final_state": 1, "_id.codigo_ref": 1}},
    ]
    # Build a matrix
    states = ["CORREDOR_SEGURO", "DUEÑO_SEGURO", "DUEÑO_PROBABLE", "INCIERTO", "AD_REMOVED"]
    rows = {}
    for r in col.aggregate(pipeline):
        state = r["_id"]["final_state"] or "NONE"
        cr = r["_id"]["codigo_ref"]
        rows.setdefault(state, {})[cr] = r["count"]
    for s in states:
        t = rows.get(s, {})
        print(f"  {s}: codigo_ref=true={t.get(True, 0)} codigo_ref=false={t.get(False, 0)}")

    # Verify
    print("\n=== VERIFICACION ===")
    print("total:", col.count_documents({}))
    print("codigo_ref true:", col.count_documents({"codigo_ref": True}))
    print("codigo_ref false:", col.count_documents({"codigo_ref": False}))
    print("sin codigo_ref:", col.count_documents({"codigo_ref": {"$exists": False}}))


if __name__ == "__main__":
    main()
