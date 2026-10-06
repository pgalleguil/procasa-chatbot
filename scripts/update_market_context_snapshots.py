"""Dry-run-first, period-scoped updater for URLS.market_context_snapshots.

September 2026 uses the approved in-code canonical manifest. Later periods must
provide a verified JSON manifest at data/market_context/<period>.json or via
--manifest. Writes and unique-index creation require --execute.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from owner_portal.market_context import (  # noqa: E402
    MARKET_CONTEXT_NATURAL_KEY,
    MARKET_CONTEXT_SNAPSHOT_COLLECTION,
    MARKET_CONTEXT_UNIQUE_INDEX_NAME,
    SEPTEMBER_2026_PERIOD,
    SEPTEMBER_2026_SEED,
    inspect_unique_key_integrity,
    plan_period_update,
    seed_identity,
    validate_market_context_documents,
)


def _mongo_config() -> tuple[str | None, str]:
    from config import Config

    return Config.MONGO_URI, Config.DB_NAME


def _load_period_documents(period: str, manifest_path: str | None = None) -> list[dict[str, Any]]:
    def _coerce_array(documents: Any) -> list[dict[str, Any]]:
        if not isinstance(documents, list) or any(not isinstance(item, Mapping) for item in documents):
            raise ValueError("MANIFEST_MUST_BE_JSON_ARRAY_OF_OBJECTS")
        return [dict(item) for item in documents]

    if manifest_path:
        path = Path(manifest_path)
        with path.open("r", encoding="utf-8") as source:
            documents = json.load(source)
        return _coerce_array(documents)
    if period == SEPTEMBER_2026_PERIOD:
        return [dict(item) for item in SEPTEMBER_2026_SEED]
    path = Path(__file__).resolve().parents[1] / "data" / "market_context" / f"{period}.json"
    if not path.is_file():
        raise FileNotFoundError(f"VERIFIED_PERIOD_MANIFEST_NOT_FOUND:{path}")
    with path.open("r", encoding="utf-8") as source:
        documents = json.load(source)
    return _coerce_array(documents)


def validate_seed(documents=SEPTEMBER_2026_SEED) -> list[str]:
    """Retain the first-load checks for the approved September 2026 batch."""
    items = list(documents)
    errors = validate_market_context_documents(items, period=SEPTEMBER_2026_PERIOD)
    if len(items) != 22:
        errors.append(f"TOTAL_EXPECTED_22_GOT_{len(items)}")
    kinds = Counter(item.get("indicator_kind") for item in items)
    if dict(kinds) != {
        "MORTGAGE_REFERENCE": 1,
        "UNEMPLOYMENT": 17,
        "TPM": 1,
        "REAL_WAGES": 1,
        "CPI": 1,
        "REGIONAL_HOME_SALES": 1,
    }:
        errors.append("SEPTEMBER_2026_KIND_COMPOSITION_INVALID")
    regional = [item for item in items if item.get("indicator_kind") == "UNEMPLOYMENT" and item.get("geography_level") == "REGION"]
    national = [item for item in items if item.get("indicator_kind") == "UNEMPLOYMENT" and item.get("geography_level") == "COUNTRY"]
    if len(regional) != 16 or len({item.get("geography_code") for item in regional}) != 16:
        errors.append("UNEMPLOYMENT_REGION_COUNT_OR_UNIQUENESS_INVALID")
    if len(national) != 1 or (national and national[0].get("geography_code") != "CL"):
        errors.append("UNEMPLOYMENT_NATIONAL_INVALID")
    required_geographies = {
        "MORTGAGE_REFERENCE": ("REGION", "METROPOLITANA"),
        "REGIONAL_HOME_SALES": ("MARKET", "GRAN_SANTIAGO"),
    }
    for kind, geography in required_geographies.items():
        if any(item.get("indicator_kind") == kind and (item.get("geography_level"), item.get("geography_code")) != geography for item in items):
            errors.append(f"{kind}_GEOGRAPHY_INVALID")
    return errors


def _print_manifest(documents: list[Mapping[str, Any]], period: str) -> bool:
    by_kind = Counter(item.get("indicator_kind") for item in documents)
    errors = validate_market_context_documents(documents, period=period)
    if period == SEPTEMBER_2026_PERIOD:
        errors.extend(validate_seed(documents))
    print(f"PERIOD={period}")
    print(f"SEED_MANIFEST_TOTAL={len(documents)}")
    print(f"SEED_MANIFEST_BY_KIND={json.dumps(dict(sorted(by_kind.items())), ensure_ascii=False)}")
    print(f"DUPLICATE_LOGICAL_IDENTITIES={sum(error == 'DUPLICATE_NATURAL_IDENTITIES' for error in errors)}")
    print(f"SOURCE_REFERENCE_MISSING={sum(not item.get('source_reference') for item in documents)}")
    print(f"OBSERVATION_PERIOD_MISSING={sum(not item.get('observation_period') for item in documents)}")
    print(f"VERIFIED_FALSE={sum(item.get('verified') is not True for item in documents)}")
    print("SEED_VALIDATION=" + ("PASS" if not errors else "FAIL"))
    if errors:
        print("SEED_VALIDATION_ERRORS=" + json.dumps(sorted(set(errors)), ensure_ascii=False))
    return not errors


def _ensure_unique_index(collection) -> bool:
    fields = list(MARKET_CONTEXT_NATURAL_KEY)
    indexes = collection.index_information()
    named = indexes.get(MARKET_CONTEXT_UNIQUE_INDEX_NAME)
    if named:
        return bool(named.get("unique")) and list(named.get("key", [])) == [(key, 1) for key in fields]
    if any(bool(spec.get("unique")) and list(spec.get("key", [])) == [(key, 1) for key in fields] for spec in indexes.values()):
        return True
    collection.create_index(
        [(key, 1) for key in fields],
        unique=True,
        name=MARKET_CONTEXT_UNIQUE_INDEX_NAME,
    )
    return True


def _print_plan(plan: Mapping[str, Any]) -> None:
    print(f"INSERTS={len(plan['inserts'])}")
    print(f"UPDATES={len(plan['updates'])}")
    print(f"UNCHANGED={len(plan['unchanged'])}")
    print(f"CONFLICTS={len(plan['conflicts'])}")
    if plan["conflicts"]:
        print("CONFLICT_DETAILS=" + json.dumps(plan["conflicts"], ensure_ascii=False, default=str))


def run_update(*, period: str, execute: bool = False, manifest_path: str | None = None) -> int:
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(period or "")):
        print("ERROR=INVALID_PERIOD; DB_WRITES=0")
        return 4
    try:
        documents = _load_period_documents(period, manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR={exc}; DB_WRITES=0")
        return 4
    if not _print_manifest(documents, period):
        print("EXECUTION=STOPPED_INVALID_MANIFEST; DB_WRITES=0")
        return 4

    uri, db_name = _mongo_config()
    if not uri:
        if execute:
            print("ERROR=MONGO_URI_NOT_CONFIGURED; DB_WRITES=0")
            return 2
        empty_plan = plan_period_update(period=period, incoming_documents=documents, existing_period_documents=[])
        print("MODE=DRY_RUN")
        print("DATABASE_DIFF=UNAVAILABLE_NO_MONGO_URI")
        print("UNIQUE_INDEX_READY=NO_MONGO_URI")
        _print_plan(empty_plan)
        print("DB_WRITES=0")
        return 0

    from pymongo import InsertOne, MongoClient, UpdateOne

    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    try:
        database = client[db_name]
        collection = database[MARKET_CONTEXT_SNAPSHOT_COLLECTION]
        existing_collection_names = set(database.list_collection_names())
        existing_keys = list(collection.find({}, {key: 1 for key in MARKET_CONTEXT_NATURAL_KEY})) if MARKET_CONTEXT_SNAPSHOT_COLLECTION in existing_collection_names else []
        key_issues = inspect_unique_key_integrity(existing_keys)
        existing_period = list(collection.find({"period": period})) if MARKET_CONTEXT_SNAPSHOT_COLLECTION in existing_collection_names else []
        plan = plan_period_update(
            period=period,
            incoming_documents=documents,
            existing_period_documents=existing_period,
        )
        print("EXISTING_COLLECTION=" + ("YES" if MARKET_CONTEXT_SNAPSHOT_COLLECTION in existing_collection_names else "NO"))
        print(f"TOTAL_EXISTING={collection.count_documents({}) if MARKET_CONTEXT_SNAPSHOT_COLLECTION in existing_collection_names else 0}")
        print(f"UNIQUE_KEY_ISSUES={len(key_issues)}")
        if key_issues:
            print("UNIQUE_KEY_ISSUE_DETAILS=" + json.dumps(key_issues, ensure_ascii=False, default=str))
        _print_plan(plan)
        if key_issues or plan["conflicts"]:
            print("EXECUTION=STOPPED_CONFLICTS_PRESENT; DB_WRITES=0")
            return 3
        if not execute:
            print("MODE=DRY_RUN; DB_WRITES=0")
            return 0

        if not _ensure_unique_index(collection):
            print("EXECUTION=STOPPED_UNIQUE_INDEX_CONFLICT; DB_WRITES=0")
            return 5
        operations = []
        for item in plan["inserts"]:
            operations.append(InsertOne(item))
        for entry in plan["updates"]:
            identity = dict(zip(MARKET_CONTEXT_NATURAL_KEY, entry["identity"]))
            operations.append(UpdateOne(identity, {"$set": entry["document"]}, upsert=False))
        result = collection.bulk_write(operations, ordered=True) if operations else None
        inserted = result.inserted_count if result else 0
        modified = result.modified_count if result else 0
        post_period = list(collection.find({"period": period}))
        post_plan = plan_period_update(
            period=period,
            incoming_documents=documents,
            existing_period_documents=post_period,
        )
        print(f"SEED_INSERTS={inserted}")
        print(f"SEED_UPDATES={modified}")
        print(f"SEED_UNCHANGED={len(post_plan['unchanged'])}")
        print(f"SEED_CONFLICTS={len(post_plan['conflicts'])}")
        print("UNIQUE_INDEX_READY=YES")
        print("POST_WRITE_VALIDATION=" + ("PASS" if not post_plan["conflicts"] and not post_plan["inserts"] and not post_plan["updates"] else "FAIL"))
        print(f"TOTAL_COLLECTION_DOCS={collection.count_documents({})}")
        print("MODE=EXECUTE")
        return 0 if not post_plan["conflicts"] and not post_plan["inserts"] and not post_plan["updates"] else 6
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", required=True, help="Snapshot period in YYYY-MM format")
    parser.add_argument("--manifest", help="Verified JSON document array; defaults to the built-in September seed or data/market_context/<period>.json")
    parser.add_argument("--execute", action="store_true", help="create/verify the unique index and write this period; default is read-only")
    args = parser.parse_args()
    return run_update(period=args.period, execute=args.execute, manifest_path=args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
