"""Scoped score/price maintenance for Paula's portfolio and July 14 discovery cohort.

Dry-run is the default. This script never changes classification state or assignment.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from bson import json_util
from pymongo import MongoClient

from config import Config
from owner_scoring import calculate_owner_score

ROOT = Path(__file__).resolve().parent
PAULA = "Paula Morales"
TOCTOC_RUN_RE = "^toctoc_scrape_20260714_18"
YAPO_BATCH = "paula_yapo_sale_filtered_20260714"
UF_VALUE = 40844.79
UF_DATE = "2026-07-14"
UF_SOURCE = "SII_UF_2026_OFFICIAL"


def _valid_number(value):
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if re.search(r"\d[.,]\d{3}(?:[.,]\d{3})", raw):
        normalized = re.sub(r"\D", "", raw)
    else:
        normalized = raw.replace(" ", "").replace(".", "").replace(",", ".")
        normalized = re.sub(r"[^\d.]", "", normalized)
    try:
        number = float(normalized)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def resolve_price_patch(doc: dict, uf_value: float, uf_date: str) -> dict:
    """Derive UF only for CLP-only listings; an original UF is never replaced."""
    details = doc.get("details") or {}
    uf_candidates = [doc.get("precio_uf"), doc.get("price_uf"), details.get("precio_uf")]
    if any(_valid_number(value) for value in uf_candidates):
        return {}

    clp = None
    for value in (doc.get("precio_clp"), doc.get("price_clp"), details.get("precio_clp")):
        parsed = _valid_number(value)
        if parsed and parsed >= 1_000:
            clp = int(round(parsed))
            break
    if clp is None:
        for value in (doc.get("precio_raw"), doc.get("price"), details.get("precio_raw"), details.get("price")):
            text = str(value or "")
            match = re.search(r"(?:\$|CLP\s*)\s*([\d.]+(?:,\d+)?)", text, re.I)
            if match:
                parsed = _valid_number(match.group(1))
                if parsed and parsed >= 1_000:
                    clp = int(round(parsed))
                    break
    if not clp:
        return {}
    return {
        "precio_clp": clp,
        "precio_uf": round(clp / uf_value, 2),
        "precio_conversion_source": UF_SOURCE,
        "uf_valor_usado": uf_value,
        "uf_fecha": uf_date,
    }


def _signal_labels(items):
    return [f"{item['code']} ({item['weight']:+d}): {item['evidence']}" for item in items]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--uf-value", type=float, default=UF_VALUE)
    parser.add_argument("--uf-date", default=UF_DATE)
    args = parser.parse_args()

    db = MongoClient(Config.MONGO_URI)[Config.DB_NAME]
    coll = db[Config.CAPTACION_COLLECTION_NAME]
    paula_docs = list(coll.find({"gestion.ejecutivo_asignado": PAULA}))
    new_docs = list(coll.find({"$or": [
        {"origen": "toctoc", "run_id": {"$regex": TOCTOC_RUN_RE}},
        {"batch_id": YAPO_BATCH},
    ]}))
    if len(paula_docs) != 24 or len(new_docs) != 9:
        raise RuntimeError(f"Safety stop: expected 24 Paula and 9 new, found {len(paula_docs)} and {len(new_docs)}")

    now = datetime.now(timezone.utc)
    score_updates = {}
    price_updates = {}
    table = []
    for doc in paula_docs:
        scoring = calculate_owner_score(doc, now)
        score_updates[doc["_id"]] = {f"classification.{key}": value for key, value in scoring.items()}
        signals = scoring["owner_score_signals"]
        cls = doc.get("classification") or {}
        table.append({
            "listing_id": str(doc.get("listing_id") or doc["_id"]),
            "origin": doc.get("origen"),
            "commune": doc.get("comuna"),
            "state": cls.get("state") or cls.get("final_state"),
            "technical_confidence": cls.get("confidence"),
            "owner_score": scoring["owner_score"],
            "positive_signals": _signal_labels(signals["positive"]),
            "negative_signals": _signal_labels(signals["negative"]),
        })

    union = {doc["_id"]: doc for doc in paula_docs + new_docs}
    for doc in union.values():
        patch = resolve_price_patch(doc, args.uf_value, args.uf_date)
        if patch:
            price_updates[doc["_id"]] = patch

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "scope": {"paula": len(paula_docs), "new": len(new_docs), "unique_price_scope": len(union)},
        "uf": {"value": args.uf_value, "date": args.uf_date, "source": UF_SOURCE},
        "technical_confidence_distribution": dict(Counter(
            str((doc.get("classification") or {}).get("confidence")) for doc in paula_docs
        )),
        "owner_score_distribution": dict(Counter(str(row["owner_score"]) for row in table)),
        "price_uf_calculated": len(price_updates),
        "price_updates": [{"listing_id": str(union[_id].get("listing_id")), **patch}
                          for _id, patch in price_updates.items()],
        "new_price_status": [
            {
                "listing_id": str(doc.get("listing_id")), "origin": doc.get("origen") or doc.get("source_portal"),
                "precio_clp": (price_updates.get(doc["_id"]) or {}).get("precio_clp", doc.get("precio_clp")),
                "precio_uf": (price_updates.get(doc["_id"]) or {}).get("precio_uf", doc.get("precio_uf")),
                "conversion_source": (price_updates.get(doc["_id"]) or {}).get(
                    "precio_conversion_source", doc.get("precio_conversion_source")
                ),
            }
            for doc in new_docs
        ],
        "cases": table,
    }

    if args.apply:
        backup_path = ROOT / "backups" / f"paula_score_price_pre_{now:%Y%m%d_%H%M%S}.json"
        backup_path.parent.mkdir(exist_ok=True)
        backup_path.write_text(json_util.dumps({"documents": list(union.values())}, ensure_ascii=False, indent=2), encoding="utf-8")
        for _id, patch in score_updates.items():
            coll.update_one({"_id": _id, "gestion.ejecutivo_asignado": PAULA}, {"$set": patch})
        for _id, patch in price_updates.items():
            coll.update_one({"_id": _id}, {"$set": patch})
        report["backup"] = str(backup_path)

    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    suffix = "applied" if args.apply else "dry_run"
    out = reports / f"paula_score_price_{suffix}_20260714.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
