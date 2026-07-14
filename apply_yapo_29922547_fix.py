"""Apply the verified extractor retry for Yapo 29922547 only."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bson import json_util
from pymongo import MongoClient

from config import Config
from owner_scoring import calculate_owner_score

ROOT = Path(__file__).resolve().parent
LISTING_ID = "29922547"
BATCH = "paula_yapo_sale_filtered_20260714"


def main() -> None:
    records = json.loads((ROOT / "scraper" / "reports" / f"processed_{BATCH}.json").read_text(encoding="utf-8"))
    record = next(item for item in records if str(item.get("listing_id")) == LISTING_ID)
    cls = dict(record.get("classification") or {})
    required = {
        "price": record.get("price"), "precio_clp": record.get("precio_clp"),
        "precio_uf": record.get("precio_uf"), "comuna": record.get("comuna"),
        "description": record.get("description"), "publicador_visible": record.get("publicador_visible"),
        "seller_type": record.get("seller_type"), "seller_profile_id": record.get("seller_profile_id"),
    }
    if cls.get("state") != "CORREDOR_SEGURO" or any(value in (None, "") for value in required.values()):
        raise RuntimeError(f"Safety stop: retry is not complete: state={cls.get('state')} required={required}")
    cls.update(calculate_owner_score(record))
    cls["assignment_ready"] = False
    record["classification"] = cls
    # The portal supplied CLP only. Use the official SII value verified for
    # 2026-07-14, independently from any stale local UF environment variable.
    record["precio_clp"] = 80_000_000
    record["precio_uf"] = round(record["precio_clp"] / 40_844.79, 2)
    record["uf_valor_usado"] = 40_844.79
    record["uf_fecha"] = "2026-07-14"
    record["precio_conversion_source"] = "SII_UF_2026_OFFICIAL"

    db = MongoClient(Config.MONGO_URI)[Config.DB_NAME]
    coll = db[Config.CAPTACION_COLLECTION_NAME]
    current = coll.find_one({"listing_id": LISTING_ID, "batch_id": BATCH})
    if not current:
        raise RuntimeError("Safety stop: scoped Mongo document not found")
    if (current.get("gestion") or {}).get("ejecutivo_asignado"):
        raise RuntimeError("Safety stop: blocked listing unexpectedly assigned")
    backup = ROOT / "backups" / f"yapo_{LISTING_ID}_pre_extractor_fix_20260714.json"
    backup.write_text(json_util.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    allowed = {
        key: record[key] for key in (
            "listing_id", "source_url", "title", "price", "precio_raw", "precio_moneda_original",
            "precio_original_num", "precio_clp", "precio_uf", "uf_valor_usado", "uf_fecha",
            "precio_conversion_source", "comuna", "region", "operacion", "tipo_propiedad",
            "description", "descripcion", "descripcion_len", "descripcion_source", "descripcion_is_truncated",
            "publicador_visible", "contact_name", "contact_logo_alt", "contact_badges_text", "seller_type",
            "seller_profile_id", "images", "attributes", "html_path", "fetch_source", "rule_context",
            "deepseek_context", "classification", "processed_at", "scrape_stage",
        ) if key in record
    }
    allowed["updated_at"] = datetime.now(timezone.utc)
    allowed["origen"] = "yapo"
    allowed["source_portal"] = "yapo"
    result = coll.update_one({"_id": current["_id"], "listing_id": LISTING_ID, "batch_id": BATCH}, {"$set": allowed})
    print(json.dumps({"matched": result.matched_count, "modified": result.modified_count,
                      "state": cls["state"], "owner_score": cls["owner_score"],
                      **{key: record.get(key) for key in required}},
                     ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
