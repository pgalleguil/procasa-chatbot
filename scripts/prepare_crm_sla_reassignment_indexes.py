"""Prepare the SLA reassignment index migration.

Dry-run is the default.  This module audits duplicate keys before proposing
unique indexes.  It never runs on import.  ``--apply`` is intentionally
guarded by an explicit confirmation and is not invoked by Phase 2B.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping


LEDGER_COLLECTION = "crm_sla_reassignment_audit_v1"


def _duplicate_groups(collection: Any, field: str) -> list[dict[str, Any]]:
    rows = list(collection.aggregate([
        {"$match": {field: {"$exists": True, "$nin": [None, ""]}}},
        {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$limit": 100},
    ]))
    # Return only key/count, never document payloads.
    return [{"key": str(row.get("_id")), "count": int(row.get("count", 0))} for row in rows]


def build_index_migration_plan(db: Any) -> dict[str, Any]:
    """Read-only duplicate audit and deterministic index proposal."""

    cycles = db["crm_assignment_cycles"]
    cycle_id_duplicates = _duplicate_groups(cycles, "assignment_cycle_id")
    source_cycle_duplicates = _duplicate_groups(
        db[LEDGER_COLLECTION], "source_cycle_id"
    )
    active_duplicates = list(cycles.aggregate([
        {"$match": {
            "cycle_status": "active",
            "unassigned_at": None,
            "lead_id": {"$exists": True},
        }},
        {"$group": {"_id": "$lead_id", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$limit": 100},
    ]))
    active_duplicates = [
        {"key": str(row.get("_id")), "count": int(row.get("count", 0))}
        for row in active_duplicates
    ]
    blocking_duplicates = bool(
        cycle_id_duplicates or source_cycle_duplicates or active_duplicates
    )
    proposed = [
        {
            "collection": LEDGER_COLLECTION,
            "name": "uq_reassignment_source_cycle",
            "keys": [("source_cycle_id", 1)],
            "unique": True,
            "partial_filter": {"event_type": "SLA_REASSIGNMENT_COMMITTED"},
            "blocked": bool(source_cycle_duplicates),
        },
        {
            "collection": "crm_assignment_cycles",
            "name": "uq_crm_assignment_cycle_id_v1",
            "keys": [("assignment_cycle_id", 1)],
            "unique": True,
            "partial_filter": {"assignment_cycle_id": {"$exists": True}},
            "blocked": bool(cycle_id_duplicates),
        },
        {
            "collection": "leads",
            "name": "ix_current_assignment_cycle_v1",
            "keys": [("lifecycle.current_assignment_cycle_id", 1)],
            "unique": False,
            "partial_filter": {"lifecycle.current_assignment_cycle_id": {"$exists": True}},
            "blocked": False,
        },
        {
            "collection": "crm_assignment_cycles",
            "name": "uq_crm_active_cycle_lead_null_guard_v1",
            "keys": [("lead_id", 1)],
            "unique": True,
            "partial_filter": {"cycle_status": "active", "unassigned_at": None},
            "blocked": bool(active_duplicates),
        },
    ]
    return {
        "dry_run": True,
        "blocking_duplicates": blocking_duplicates,
        "duplicates": {
            "assignment_cycle_id": cycle_id_duplicates,
            "source_cycle_id": source_cycle_duplicates,
            "active_lead_id": active_duplicates,
        },
        "proposed_indexes": proposed,
        "apply_allowed": not blocking_duplicates,
    }


def apply_index_migration_plan(db: Any, plan: Mapping[str, Any]) -> list[str]:
    """Apply a previously audited plan; never called by the Phase 2B CLI."""

    if plan.get("blocking_duplicates"):
        raise RuntimeError("INDEX_MIGRATION_BLOCKED_DUPLICATES")
    created: list[str] = []
    for spec in plan.get("proposed_indexes", []):
        kwargs = {
            "name": spec["name"],
            "unique": bool(spec["unique"]),
        }
        if spec.get("partial_filter"):
            kwargs["partialFilterExpression"] = spec["partial_filter"]
        created.append(
            db[spec["collection"]].create_index(spec["keys"], **kwargs)
        )
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan SLA reassignment indexes")
    parser.add_argument("--apply", action="store_true", help="apply after explicit confirmation")
    parser.add_argument(
        "--confirm",
        default="",
        help="must equal APPLY_SLA_REASSIGNMENT_INDEXES to apply",
    )
    args = parser.parse_args()
    from chatbot.storage import get_db

    plan = build_index_migration_plan(get_db())
    if not args.apply:
        print(json.dumps(plan, default=str, ensure_ascii=False, indent=2))
        return 0
    if args.confirm != "APPLY_SLA_REASSIGNMENT_INDEXES":
        raise SystemExit("--apply requires --confirm APPLY_SLA_REASSIGNMENT_INDEXES")
    from config import Config
    if getattr(Config, "IS_PRODUCTION", False):
        raise SystemExit("production index apply is disabled in Phase 2B")
    applied = apply_index_migration_plan(get_db(), plan)
    print(json.dumps({"dry_run": False, "created": applied}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
