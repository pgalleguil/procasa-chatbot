"""Read-only export of the approved 482 records from isolated Yapo staging."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scrapers.scraper_yapo.config import get_config
from scrapers.scraper_yapo.import_yapo_prepared_batch import EXPECTED_STATE_COUNTS, TARGET_COLLECTION, TARGET_DATABASE
from scrapers.scraper_yapo.staging import SOURCE_PORTAL, STAGING_COLLECTION, STAGING_DATABASE

EXPECTED_CANONICAL_SHA256 = "dc9bda51a093abd9a1d7c6a741eeacac275724ea74b29ad4cc2a8bba558e12bf"
DEFAULT_CANONICAL = Path(r"C:\Users\pgall\Documents\PROCASA\YapoMaipu\preimportacion_20261103\yapo_maipu_preimport_482.json")
DEFAULT_OUTPUT = REPO_ROOT / "scrapers" / "scraper_yapo" / "reports" / "yapo_migration_20261104" / "staging_export_482.json"


def _convert(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _convert(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_convert(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_staging_batch(
    output_path: Path,
    *,
    canonical_path: Path = DEFAULT_CANONICAL,
    expected_canonical_sha256: str = EXPECTED_CANONICAL_SHA256,
) -> dict[str, Any]:
    canonical_path = canonical_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    canonical_bytes = canonical_path.read_bytes()
    canonical_hash = hashlib.sha256(canonical_bytes).hexdigest()
    if canonical_hash.lower() != expected_canonical_sha256.lower():
        raise RuntimeError("Approved canonical JSON SHA-256 mismatch; no staging export was created.")
    canonical_data = json.loads(canonical_bytes.decode("utf-8-sig"))
    if not isinstance(canonical_data, list) or len(canonical_data) != 482:
        raise RuntimeError("Canonical source is not the expected 482-record JSON array.")
    canonical_ids = {str(row.get("listing_id") or "").strip() for row in canonical_data}
    if len(canonical_ids) != 482 or "" in canonical_ids:
        raise RuntimeError("Canonical source IDs are missing or duplicated.")

    config = get_config()
    if config.mongo_db != STAGING_DATABASE or config.mongo_collection != TARGET_COLLECTION:
        raise RuntimeError("Atlas configuration does not identify URLS.propiedades_captacion.")
    host = urlsplit(config.mongo_uri).hostname or ""
    if not host.endswith("mongodb.net"):
        raise RuntimeError("Mongo target is not Atlas; no read was started.")

    from pymongo import MongoClient, ReadPreference
    from pymongo.read_concern import ReadConcern

    client = MongoClient(
        config.mongo_uri,
        serverSelectionTimeoutMS=20000,
        read_preference=ReadPreference.SECONDARY_PREFERRED,
    )
    try:
        client.admin.command("ping")
        database = client.get_database(
            STAGING_DATABASE,
            read_concern=ReadConcern("majority"),
            read_preference=ReadPreference.SECONDARY_PREFERRED,
        )
        if TARGET_COLLECTION not in database.list_collection_names() or STAGING_COLLECTION not in database.list_collection_names():
            raise RuntimeError("Required operational or staging collection is missing.")
        staging = database[STAGING_COLLECTION]
        docs = list(staging.find(
            {"source_portal": SOURCE_PORTAL, "canonical_source_sha256": expected_canonical_sha256},
            {"_id": 0},
        ).sort("listing_id", 1))
        ids = [str(doc.get("listing_id") or "").strip() for doc in docs]
        if len(docs) != 482 or len(set(ids)) != 482 or set(ids) != canonical_ids:
            raise RuntimeError("Staging does not contain exactly the 482 canonical IDs; no export was created.")
        states = Counter(str((doc.get("classification") or {}).get("state") or "") for doc in docs)
        if dict(states) != EXPECTED_STATE_COUNTS:
            raise RuntimeError(f"Staged canonical classification counts changed: {dict(states)}")

        observation_count = sum(len(doc.get("manual_audit_observations") or []) for doc in docs)
        review_count = sum(len(doc.get("manual_review_batches") or []) for doc in docs)
        if observation_count != 21 or review_count != 40:
            raise RuntimeError(
                f"Staged audit preservation check failed: observations={observation_count}, reviews={review_count}."
            )

        export_docs = [_convert({key: value for key, value in doc.items() if key != "_id"}) for doc in docs]
        encoded = (json.dumps(export_docs, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(output_path)
        return {
            "mode": "read_only_export",
            "source": f"{STAGING_DATABASE}.{STAGING_COLLECTION}",
            "target_for_following_preflight": f"{TARGET_DATABASE}.{TARGET_COLLECTION}",
            "canonical_count": len(export_docs),
            "canonical_sha256": canonical_hash,
            "export_sha256": _sha256(output_path),
            "listing_ids_unique_and_match_source": True,
            "classification_counts": dict(states),
            "manual_observations": observation_count,
            "pending_manual_reviews": review_count,
            "export_path": str(output_path),
            "mongo_writes": 0,
            "yapo_requests": 0,
            "distribution_runs": 0,
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--expected-canonical-sha256", default=EXPECTED_CANONICAL_SHA256)
    args = parser.parse_args()
    print(json.dumps(export_staging_batch(
        args.output,
        canonical_path=args.canonical,
        expected_canonical_sha256=args.expected_canonical_sha256,
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
