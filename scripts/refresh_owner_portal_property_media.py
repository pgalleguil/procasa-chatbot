"""Verify and persist exact property photos for the monthly owner portal.

This is an offline preparation command. It is never called while serving /p.
Dry-run is the default; pass --execute to append immutable monthly snapshots.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chatbot.storage import get_db
from config import Config
from owner_portal.email_artifacts import EMAIL_ARTIFACT_COLLECTION, verify_original_email_artifact
from owner_portal.monthly import owner_property_portal_id, persist_monthly_snapshot
from owner_portal.property_media import (
    fetch_verified_property_media,
    verified_historical_media,
    verified_media_for_property,
    verified_media_from_property_record,
)


def _source_for_row(db, row):
    code = str(row.get("property_code") or "").strip()
    result = fetch_verified_property_media(code)
    if result.get("status") == "FOUND":
        return result
    existing = verified_media_for_property(code, row=row)
    if existing:
        existing["status"] = "EXISTING_PROPERTY_DATA"
        return existing
    master = db["universo_cartera_prop360"].find_one(
        {"codigo": code},
        {"_id": 0, "codigo": 1, "property_code": 1, "main_image_url": 1, "image_url": 1,
         "photo_url": 1, "foto_principal": 1, "imagen_principal": 1, "image_urls": 1,
         "images": 1, "photos": 1, "fotos": 1, "imagenes": 1},
    )
    property_image = verified_media_from_property_record(master, code)
    if property_image:
        property_image["status"] = "EXISTING_PROPERTY_DATA"
        return property_image
    capture = db["propiedades_captacion"].find_one(
        {"listing_id": code},
        {"_id": 0, "listing_id": 1, "property_code": 1, "main_image_url": 1, "image_urls": 1, "images": 1},
    )
    capture_image = verified_media_from_property_record(capture, code)
    if capture_image:
        capture_image["status"] = "EXISTING_PROPERTY_DATA"
        return capture_image
    campaign_id = str(row.get("campaign_id") or "")
    artifact = db[EMAIL_ARTIFACT_COLLECTION].find_one({"_id": f"{campaign_id}:{code}", "campaign_id": campaign_id, "property_code": code})
    if artifact:
        try:
            historical = verified_historical_media(verify_original_email_artifact(artifact), code)
            if historical:
                historical["status"] = "HISTORICAL_PROPERTY_IMAGE"
                return historical
        except ValueError:
            pass
    return {"status": result.get("status", "IMAGE_MISSING"), "property_code_match": bool(result.get("property_code_match"))}


def _prepare_one(db, row, period, now, execute):
    code = str(row.get("property_code") or "").strip()
    owner_email = str(row.get("owner_email") or "").strip()
    if not code or not owner_email:
        return "identity_error"
    portal_id = owner_property_portal_id(code, owner_email)
    current_record = db["owner_property_portals"].find_one({"_id": portal_id}, {"_id": 1, "monthly_snapshots.period": 1})
    if any(item.get("period") == period for item in (current_record or {}).get("monthly_snapshots", []) if isinstance(item, dict)):
        return "snapshot_period_already_prepared"
    media = _source_for_row(db, row)
    status = str(media.get("status") or "IMAGE_MISSING")
    property_media = None
    if status == "FOUND" or status in {"EXISTING_PROPERTY_DATA", "HISTORICAL_PROPERTY_IMAGE"}:
        property_media = {
            "public_page_url": media.get("public_page_url") or f"https://www.procasa.cl/{code}",
            "hero_image_url": media["hero_image_url"],
            "image_source": media.get("image_source"),
            "verified_property_code": code,
            "verified_at": media.get("verified_at") or now,
            "public_page_active": bool(media.get("public_page_active")),
        }
    snapshot = {
        "period": period,
        "property_code": code,
        "generated_at": now,
        "source_live_sha": (row.get("campaign_snapshot") or {}).get("source_live_sha"),
        "property_media": property_media or {"status": "NO_VERIFIED_IMAGE", "verified_property_code": code},
    }
    if execute:
        try:
            persisted = persist_monthly_snapshot(db, property_code=code, owner_email=owner_email, snapshot=snapshot)
            return f"{status.casefold()}_persist_{persisted['status'].casefold()}"
        except Exception:
            return "persist_error"
    return status.casefold()


def prepare(*, execute: bool = False) -> dict[str, int]:
    db = get_db()
    now = datetime.now(timezone.utc)
    ledger = db[Config.COLLECTION_CAMPANAS_LOG]
    query = {
        "portal_access.status": "ACTIVE",
        "portal_access.revoked_at": None,
        "portal_access.expires_at": {"$gt": now},
    }
    projection = {"_id": 1, "campaign_id": 1, "property_code": 1, "owner_email": 1,
                  "campaign_snapshot": 1, "portal_access.status": 1}
    rows = list(ledger.find(query, projection).sort("property_code", 1))
    counts: Counter[str] = Counter(total=len(rows))
    period = now.strftime("%Y-%m")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = pool.map(lambda row: _prepare_one(db, row, period, now, execute), rows)
        for index, status in enumerate(results, 1):
            counts[status] += 1
            if index % 25 == 0 or index == len(rows):
                print(f"PROCESSED={index}/{len(rows)}", flush=True)
    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="persist verified media in immutable monthly snapshots")
    args = parser.parse_args()
    counts = prepare(execute=args.execute)
    for key, value in sorted(counts.items()):
        print(f"{key.upper()}={value}")
    print(f"WRITES_ENABLED={'YES' if args.execute else 'NO'}")
    return 0 if counts.get("identity_error", 0) == 0 and counts.get("persist_error", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
