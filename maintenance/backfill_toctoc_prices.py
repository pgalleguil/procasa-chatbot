"""Dry-run/apply del backfill de precios Toctoc sin tocar otros campos."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config
from scraper_toctoc.enrich import parse_toctoc_price

REPORT = "reports/toctoc_team/toctoc_price_backfill_20260817.json"


def valid_uf(value):
    try:
        value = float(value)
        return math.isfinite(value) and 0 < value < 1_000_000
    except (TypeError, ValueError):
        return False


def valid_clp(value):
    try:
        value = float(value)
        return math.isfinite(value) and 0 < value < 100_000_000_000
    except (TypeError, ValueError):
        return False


def main(apply: bool):
    load_dotenv(".env")
    client = MongoClient(os.getenv("MONGO_URI"), serverSelectionTimeoutMS=30000,
                         connectTimeoutMS=10000, socketTimeoutMS=60000)
    db = client[os.getenv("MONGO_DB", "yapo")]
    coll = db[Config.CAPTACION_COLLECTION_NAME]
    query = {"origen": "toctoc", "precio_raw": {"$exists": True, "$nin": [None, ""]}}
    projection = {
        "listing_id": 1, "precio_raw": 1, "precio_uf": 1, "precio_clp": 1,
        "operacion": 1, "tipo_propiedad": 1, "gestion.ejecutivo_id": 1,
    }
    docs = list(coll.find(query, projection, batch_size=2000))
    counts = Counter()
    repairs = []
    samples = []
    for doc in docs:
        raw = str(doc.get("precio_raw") or "")
        parsed = parse_toctoc_price(raw, Config.UF_VALOR_CLP)
        uf_before = doc.get("precio_uf")
        clp_before = doc.get("precio_clp")
        uf_ok = valid_uf(uf_before)
        clp_ok = valid_clp(clp_before)
        if uf_ok and clp_ok:
            counts["already_correct"] += 1
            continue
        counts["affected"] += 1
        uf_after = uf_before if uf_ok else parsed.get("precio_uf")
        clp_after = clp_before if clp_ok else parsed.get("precio_clp")
        uf_repairable = uf_ok or valid_uf(uf_after)
        clp_repairable = clp_ok or valid_clp(clp_after)
        if uf_repairable or clp_repairable:
            counts["repairable"] += 1
            updates = {}
            if not uf_ok and valid_uf(uf_after): updates["precio_uf"] = float(uf_after)
            if not clp_ok and valid_clp(clp_after): updates["precio_clp"] = int(round(float(clp_after)))
            repairs.append({"_id": doc["_id"], "listing_id": doc.get("listing_id"), "raw": raw,
                            "uf_before": uf_before, "clp_before": clp_before,
                            "uf_after": uf_after, "clp_after": clp_after, "updates": updates,
                            "gestion": (doc.get("gestion") or {}).get("ejecutivo_id")})
        else:
            counts["not_parseable"] += 1
        if len(samples) < 20:
            samples.append({"listing_id": doc.get("listing_id"), "precio_raw": raw,
                            "precio_uf_before": uf_before, "precio_clp_before": clp_before,
                            "precio_uf_after": uf_after, "precio_clp_after": clp_after,
                            "updates": repairs[-1]["updates"] if repairs and repairs[-1]["listing_id"] == doc.get("listing_id") else {}})

    assigned_query = {"origen": "toctoc", "gestion.asignacion_version": "v1_admin_toctoc_campaign_20260817"}
    assigned_total = coll.count_documents(assigned_query)
    assigned_before = coll.count_documents({**assigned_query, "precio_uf": {"$gt": 0}, "precio_clp": {"$gt": 0}})
    hernan_query = {**assigned_query, "gestion.ejecutivo_id": "6a681413140190dde11f26d1"}
    hernan_total = coll.count_documents(hernan_query)
    hernan_before = coll.count_documents({**hernan_query, "precio_uf": {"$gt": 0}, "precio_clp": {"$gt": 0}})

    applied = 0
    errors = []
    if apply:
        for item in repairs:
            if not item["updates"]:
                continue
            result = coll.update_one({"_id": item["_id"], "origen": "toctoc"}, {"$set": item["updates"]})
            if result.modified_count == 1:
                applied += 1
            else:
                errors.append({"listing_id": item["listing_id"], "reason": "not_modified"})

    report = {
        "mode": "APPLY" if apply else "DRY_RUN", "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query, "toctoc_total_with_precio_raw": len(docs), "counts": dict(counts),
        "repairs_planned": len(repairs), "applied": applied, "errors": errors, "samples": samples,
        "assigned_730": {"total": assigned_total, "with_numeric_before": assigned_before},
        "hernan": {"total": hernan_total, "with_numeric_before": hernan_before},
        "fields_modified": ["precio_uf", "precio_clp"],
        "protected_fields": ["classification", "owner_probability", "gestion", "ejecutivo", "assignment_ready", "run_id"],
        "rescraping": 0,
    }
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, default=str, indent=2)
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    main(p.parse_args().apply)
