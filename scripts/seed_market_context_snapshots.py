"""Dry-run-first seed for URLS.market_context_snapshots.

The default mode only reads existing records (when MONGO_URI is configured).
Writes require the explicit --execute flag.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from owner_portal.market_context import (  # noqa: E402
    MARKET_CONTEXT_SNAPSHOT_COLLECTION,
    SEPTEMBER_2026_SEED,
    plan_seed,
    seed_identity,
)


def _mongo_config():
    from config import Config

    return Config.MONGO_URI, Config.DB_NAME


def _existing_documents(collection, seed_documents):
    periods = sorted({item["period"] for item in seed_documents})
    return list(collection.find({"period": {"$in": periods}}))


def validate_seed(documents=SEPTEMBER_2026_SEED):
    """Validate exact composition, natural keys, provenance, and geography."""
    errors = []
    identities = [seed_identity(item) for item in documents]
    if len(documents) != 22:
        errors.append(f"TOTAL_EXPECTED_22_GOT_{len(documents)}")
    if len(set(identities)) != len(identities):
        errors.append("DUPLICATE_NATURAL_IDENTITIES")
    required = (
        "period", "indicator_kind", "value", "display_value", "observation_period",
        "geography_level", "geography_code", "geography_label", "source_name",
        "source_reference", "verified", "relevant_operations",
    )
    for index, item in enumerate(documents):
        for key in required:
            if item.get(key) in (None, "", []):
                errors.append(f"DOC_{index}_MISSING_{key.upper()}")
        if item.get("verified") is not True:
            errors.append(f"DOC_{index}_NOT_VERIFIED")
        kind = item.get("indicator_kind")
        geography = (item.get("geography_level"), item.get("geography_code"))
        expected_geo = {
            "MORTGAGE_REFERENCE": ("REGION", "METROPOLITANA"),
            "REGIONAL_HOME_SALES": ("MARKET", "GRAN_SANTIAGO"),
            "TPM": ("COUNTRY", "CL"),
            "REAL_WAGES": ("COUNTRY", "CL"),
            "CPI": ("COUNTRY", "CL"),
        }.get(kind)
        if expected_geo and geography != expected_geo:
            errors.append(f"DOC_{index}_INVALID_GEOGRAPHY_{kind}")
        if kind == "UNEMPLOYMENT" and geography[0] not in {"REGION", "COUNTRY"}:
            errors.append(f"DOC_{index}_INVALID_UNEMPLOYMENT_LEVEL")
    regional_unemployment = [
        item for item in documents
        if item.get("indicator_kind") == "UNEMPLOYMENT" and item.get("geography_level") == "REGION"
    ]
    national_unemployment = [
        item for item in documents
        if item.get("indicator_kind") == "UNEMPLOYMENT" and item.get("geography_level") == "COUNTRY"
    ]
    region_codes = [item.get("geography_code") for item in regional_unemployment]
    if len(region_codes) != 16 or len(set(region_codes)) != 16:
        errors.append("UNEMPLOYMENT_REGION_COUNT_OR_UNIQUENESS_INVALID")
    if len(national_unemployment) != 1 or (national_unemployment and national_unemployment[0].get("geography_code") != "CL"):
        errors.append("UNEMPLOYMENT_NATIONAL_INVALID")
    allowed_kinds = {
        "UNEMPLOYMENT", "MORTGAGE_REFERENCE", "TPM", "REAL_WAGES", "CPI", "REGIONAL_HOME_SALES",
    }
    if any(item.get("indicator_kind") not in allowed_kinds for item in documents):
        errors.append("FORBIDDEN_PRIMARY_SEED_KIND")
    return errors


def print_manifest(documents=SEPTEMBER_2026_SEED):
    by_kind = Counter(item.get("indicator_kind") for item in documents)
    unique_regional_codes = {
        item.get("geography_code") for item in documents
        if item.get("indicator_kind") == "UNEMPLOYMENT" and item.get("geography_level") == "REGION"
    }
    regional_unemployment = sum(
        item.get("indicator_kind") == "UNEMPLOYMENT" and item.get("geography_level") == "REGION"
        for item in documents
    )
    national_unemployment = sum(
        item.get("indicator_kind") == "UNEMPLOYMENT" and item.get("geography_level") == "COUNTRY"
        for item in documents
    )
    print(f"SEED_MANIFEST_TOTAL={len(documents)}")
    print(f"SEED_MANIFEST_BY_KIND={json.dumps(dict(sorted(by_kind.items())), ensure_ascii=False)}")
    print(f"UNEMPLOYMENT_REGION={regional_unemployment}")
    print(f"UNEMPLOYMENT_NATIONAL={national_unemployment}")
    for kind in ("MORTGAGE_REFERENCE", "TPM", "REAL_WAGES", "CPI", "REGIONAL_HOME_SALES"):
        print(f"{kind}={by_kind.get(kind, 0)}")
    print(f"UNEMPLOYMENT_REGIONS_UNIQUE={len(unique_regional_codes)}")
    print(f"UNEMPLOYMENT_DUPLICATE_REGIONS={max(0, regional_unemployment - len(unique_regional_codes))}")
    print(f"MORTGAGE_GEOGRAPHY_MISMATCH={sum(item.get('indicator_kind') == 'MORTGAGE_REFERENCE' and (item.get('geography_level'), item.get('geography_code')) != ('REGION', 'METROPOLITANA') for item in documents)}")
    print(f"GRAN_SANTIAGO_LABELED_NATIONAL={sum(item.get('indicator_kind') == 'REGIONAL_HOME_SALES' and (item.get('geography_level'), item.get('geography_code')) != ('MARKET', 'GRAN_SANTIAGO') for item in documents)}")
    print(f"QA_RENTAL_INDICATOR_INCLUDED={sum(item.get('indicator_kind') == 'REGIONAL_RENTAL_INDICATOR' for item in documents)}")
    print(f"FOGAES_INCLUDED_IN_PRIMARY_SEED={sum(item.get('indicator_kind') == 'FOGAES_CONTEXT' for item in documents)}")
    print(f"SOURCE_REFERENCE_MISSING={sum(not item.get('source_reference') for item in documents)}")
    print(f"OBSERVATION_PERIOD_MISSING={sum(not item.get('observation_period') for item in documents)}")
    print(f"VERIFIED_FALSE={sum(item.get('verified') is not True for item in documents)}")
    errors = validate_seed(documents)
    print("SEED_VALIDATION=" + ("PASS" if not errors else "FAIL"))
    if errors:
        print("SEED_VALIDATION_ERRORS=" + json.dumps(errors, ensure_ascii=False))
    return not errors


def run(*, execute: bool = False) -> int:
    if not print_manifest():
        print("EXECUTION=STOPPED_INVALID_SEED")
        return 4
    uri, db_name = _mongo_config()
    if not uri:
        if execute:
            print("ERROR=MONGO_URI_NOT_CONFIGURED; no writes performed")
            return 2
        plan = plan_seed([])
        print("MODE=DRY_RUN")
        print("EXISTING_COLLECTION=UNKNOWN_NO_MONGO_URI")
        print("DATABASE_DIFF=UNAVAILABLE_NO_MONGO_URI")
        _print_plan(plan)
        return 0

    from pymongo import MongoClient, ReplaceOne

    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        collection = client[db_name][MARKET_CONTEXT_SNAPSHOT_COLLECTION]
        existing_collections = set(client[db_name].list_collection_names())
        print("EXISTING_COLLECTION=" + ("YES" if MARKET_CONTEXT_SNAPSHOT_COLLECTION in existing_collections else "NO"))
        existing = _existing_documents(collection, SEPTEMBER_2026_SEED)
        plan = plan_seed(existing)
        _print_plan(plan)
        if plan["conflicts"]:
            print("EXECUTION=STOPPED_CONFLICTS_PRESENT")
            return 3
        if not execute:
            print("MODE=DRY_RUN; DB_WRITES=0")
            return 0

        operations = []
        for item in [*plan["inserts"], *(entry["document"] for entry in plan["updates"])]:
            identity = dict(zip(("period", "indicator_kind", "geography_level", "geography_code"), seed_identity(item)))
            operations.append(ReplaceOne(identity, item, upsert=True))
        if operations:
            result = collection.bulk_write(operations, ordered=True)
            print(f"WRITTEN={result.upserted_count + result.modified_count}")
        print("MODE=EXECUTE")
        return 0
    finally:
        client.close()


def _print_plan(plan):
    print(f"INSERTS={len(plan['inserts'])}")
    print(f"UPDATES={len(plan['updates'])}")
    print(f"UNCHANGED={len(plan['unchanged'])}")
    print(f"CONFLICTS={len(plan['conflicts'])}")
    if plan["conflicts"]:
        print("CONFLICT_DETAILS=" + json.dumps(plan["conflicts"], ensure_ascii=False, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="write seed records; default is read-only dry-run")
    args = parser.parse_args()
    return run(execute=args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
