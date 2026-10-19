#!/usr/bin/env python3
"""Inspect or add the index missing from the CRM assignment reconciler query.

Mongo access is read from MONGODB_URI or MONGO_URI in the authorized runtime.
The dry-run only checks index metadata and the winning query plan. Execute is
idempotent, adds one ordinary ascending index, and verifies the plan afterward.
It does not update lead, cycle, SLA, notification, or customer records.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys
from typing import Any, Mapping

from pymongo import ASCENDING, MongoClient


DATABASE = "URLS"
COLLECTION = "crm_assignment_cycles"
INDEX_NAME = "idx_crm_assignment_cycle_assigned_at_v1"
CONFIRMATION = "CREATE-CRM-CYCLE-RECONCILIATION-INDEX"
PRODUCTION_CLUSTER_PREFIX = "ac-8f1k7c3-shard-"
PRODUCTION_CLUSTER_SUFFIX = ".mzve39k.mongodb.net"


def reconciliation_filter(now: datetime | None = None, lookback_days: int = 30) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=max(int(lookback_days), 1))
    return {"$or": [
        {"assigned_at": {"$gte": cutoff}},
        {"assigned_at": {"$gte": cutoff.isoformat()}},
        {"cycle_status": "active", "unassigned_at": None},
        {"cycle_status": {"$exists": False}, "unassigned_at": None},
    ]}


def _find_assigned_at_index(indexes: list[Mapping[str, Any]]) -> str | None:
    for index in indexes:
        key = index.get("key") or {}
        first_key = next(iter(key.items()), None)
        if first_key and first_key[0] == "assigned_at" and not index.get("partialFilterExpression"):
            return str(index.get("name") or "") or None
    return None


def _find_index_names(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        index_name = value.get("indexName")
        if index_name:
            found.add(str(index_name))
        for child in value.values():
            found.update(_find_index_names(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_find_index_names(child))
    return found


def _explain(db: Any) -> Mapping[str, Any]:
    return db.command("explain", {
        "find": COLLECTION,
        "filter": reconciliation_filter(),
        "projection": {"_id": 1, "lead_id": 1, "assigned_at": 1,
                       "cycle_status": 1, "unassigned_at": 1},
        "limit": 10000,
    }, verbosity="queryPlanner")


def _validate_production_identity(client: MongoClient, db: Any) -> str:
    hello = client.admin.command("hello")
    hosts = [str(value).rsplit(":", 1)[0] for value in (hello.get("hosts") or [])]
    if (not hello.get("setName") or not any(
            host.startswith(PRODUCTION_CLUSTER_PREFIX) and host.endswith(PRODUCTION_CLUSTER_SUFFIX)
            for host in hosts)):
        raise RuntimeError("connected MongoDB replica set is not the confirmed Procasa production cluster")
    stats = db.command("dbStats")
    if stats.get("db") != DATABASE or int(stats.get("collections") or 0) < 1:
        raise RuntimeError("connected database identity does not match the nonempty production URLS database")
    return str(hello["setName"])


def _summary(indexes: list[Mapping[str, Any]], plan: Mapping[str, Any]) -> dict[str, Any]:
    assigned_index = _find_assigned_at_index(indexes)
    used_indexes = sorted(_find_index_names(plan.get("queryPlanner") or {}))
    return {
        "database": DATABASE,
        "collection": COLLECTION,
        "assigned_at_index": assigned_index,
        "winning_plan_uses_assigned_at_index": bool(assigned_index and assigned_index in used_indexes),
        "winning_plan_index_names": used_indexes,
        "winning_plan": (plan.get("queryPlanner") or {}).get("winningPlan"),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true", help="inspect indexes and explain the query; no changes")
    modes.add_argument("--execute", action="store_true", help="create the missing assigned_at index and re-check the plan")
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--confirm-index", help=f"required with --execute: {CONFIRMATION}")
    args = parser.parse_args(argv)
    if args.database != DATABASE:
        parser.error(f"this maintenance command is restricted to {DATABASE}")
    if args.execute and args.confirm_index != CONFIRMATION:
        parser.error(f"--execute requires --confirm-index {CONFIRMATION}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    if not uri:
        print("ERROR: set MONGODB_URI or MONGO_URI in the authorized runtime; no credential is accepted on the command line.", file=sys.stderr)
        return 2
    client = MongoClient(uri, appName="procasa-crm-cycle-index-check", retryWrites=True,
                         connectTimeoutMS=5000, serverSelectionTimeoutMS=15000,
                         socketTimeoutMS=30000)
    try:
        db = client[DATABASE]
        replica_set = _validate_production_identity(client, db)
        collection = db[COLLECTION]
        indexes = list(collection.list_indexes())
        existing = _find_assigned_at_index(indexes)
        if args.execute and not existing:
            existing = collection.create_index([("assigned_at", ASCENDING)], name=INDEX_NAME)
            indexes = list(collection.list_indexes())
        plan = _explain(db)
        report = _summary(indexes, plan)
        report["replica_set"] = replica_set
        report["production_cluster"] = "confirmed_procasa"
        report["index_created_or_reused"] = existing
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str))
        if args.execute and not report["winning_plan_uses_assigned_at_index"]:
            print("WARNING: the index exists, but MongoDB did not choose it for the reconciliation query; inspect the plan before declaring the timeout resolved.", file=sys.stderr)
            return 3
        if args.dry_run:
            print("STATUS: dry_run_complete; no database changes performed")
        else:
            print("STATUS: index_verified")
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}; no successful outcome reported", file=sys.stderr)
        return 4
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
