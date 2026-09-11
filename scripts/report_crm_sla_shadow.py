"""Read-only operational report for the CRM SLA reassignment shadow run."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from chatbot.crm_sla_reassignment_shadow import (
    SHADOW_COLLECTION,
    SHADOW_POLICY_VERSION,
    reconstruct_shadow_distribution_state,
)
from chatbot.crm_sla_reassignment_cutover import parse_explicit_santiago_timestamp
from chatbot.crm_sla_worker_lease import LEASE_COLLECTION, LEADER_KEY
from chatbot.storage import get_db


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _utc(value)
    return parsed.isoformat() if parsed else None


def build_report(db: Any, *, cutover_at: Any = None, now: Any = None) -> dict[str, Any]:
    from config import Config

    current = _utc(now) or datetime.now(timezone.utc)
    configured = cutover_at if cutover_at is not None else Config.CRM_SLA_REASSIGNMENT_SHADOW_CUTOVER_AT
    cutover = parse_explicit_santiago_timestamp(configured)
    rows = list(db[SHADOW_COLLECTION].find({"policy_version": SHADOW_POLICY_VERSION}, {
        "_id": 1, "source_cycle_id": 1, "lead_id": 1, "first_evaluated_at": 1,
        "last_evaluated_at": 1, "evaluated_at": 1, "shadow_cutover_at": 1,
        "cutover_at": 1, "breach_at": 1, "policy_version": 1, "branch": 1,
        "eligibility_outcome": 1, "exclusion_reason": 1, "candidate_user_ids": 1,
        "selected_user_id": 1, "selected_score": 1, "second_candidate_id": 1,
        "score_margin": 1, "score_margin_bucket": 1, "performance_snapshot_id": 1,
        "performance_snapshot": 1, "capacity_snapshot_at": 1, "r2_state_snapshot": 1,
        "j3_target_share_snapshot": 1, "would_execute": 1,
        "shadow_assignment_counted_at": 1, "management_after_shadow": 1,
        "management_after_shadow_minutes": 1, "management_after_shadow_bucket": 1,
        "evaluation_count": 1, "limited_history": 1, "batch_id": 1,
    }))
    since_rows = [row for row in rows if (_utc(row.get("shadow_cutover_at") or row.get("cutover_at")) == cutover)]
    counted = [row for row in since_rows if row.get("would_execute") is True and _utc(row.get("shadow_assignment_counted_at")) and _utc(row.get("shadow_assignment_counted_at")) >= cutover]
    outcomes = Counter(str(row.get("eligibility_outcome") or row.get("exclusion_reason") or "UNKNOWN") for row in since_rows)
    branches = Counter(str(row.get("branch") or "UNDEFINED") for row in counted)
    margins = Counter(str(row.get("score_margin_bucket") or "unknown") for row in counted)
    post_management = Counter(str(row.get("management_after_shadow_bucket") or "never") for row in counted if row.get("management_after_shadow"))
    batch_ids = {str(row.get("batch_id")) for row in since_rows if row.get("batch_id")}
    performance_ids = {str(row.get("performance_snapshot_id")) for row in since_rows if row.get("performance_snapshot_id")}
    state = reconstruct_shadow_distribution_state(counted, policy_version=SHADOW_POLICY_VERSION, cutover_at=cutover)
    lease = db[LEASE_COLLECTION].find_one({"_id": LEADER_KEY}, {
        "_id": 1, "key": 1, "holder_id": 1, "acquired_at": 1,
        "heartbeat_at": 1, "expires_at": 1, "version": 1,
    }) or {}
    expires = _utc(lease.get("expires_at"))
    health = "SHADOW_HEALTHY" if expires and expires > current else "SHADOW_STALE"
    return {
        "mode": "shadow",
        "policy_version": SHADOW_POLICY_VERSION,
        "shadow_cutover_at": cutover.isoformat(),
        "timezone": "America/Santiago",
        "since_cutover": {
            "iterations": len(batch_ids),
            "shadow_documents": len(since_rows),
            "new_breaches": sum(1 for row in since_rows if _utc(row.get("breach_at")) and _utc(row.get("breach_at")) >= cutover),
            "would_execute": len(counted),
        },
        "state": {
            "rm": state.get("rm_history", []),
            "jpc": state.get("jpc_counts", {}),
            "rm_count": len(state.get("rm_history", [])),
            "jpc_count": sum((state.get("jpc_counts") or {}).values()),
            "undefined": outcomes.get("REGIONAL_POLICY_NOT_DEFINED", 0),
            "review": sum(value for key, value in outcomes.items() if "REVIEW" in key or "NO_ELIGIBLE" in key),
            "protected": outcomes.get("PROTECTED_BY_MANAGEMENT", 0),
        },
        "management_after_shadow": dict(post_management),
        "score_margin_buckets": dict(margins),
        "distribution_by_branch": dict(branches),
        "r2_interventions": len(state.get("rm_history", [])),
        "j3_share": (state.get("jpc_counts") or {}),
        "performance": {
            "snapshot_ids_observed": len(performance_ids),
            "last_capacity_snapshot_at": max((_iso(row.get("capacity_snapshot_at")) or "" for row in since_rows), default=None),
            "iteration_duration_ms": [row.get("performance_snapshot", {}).get("iteration_duration_ms") for row in since_rows if isinstance(row.get("performance_snapshot"), dict) and row.get("performance_snapshot", {}).get("iteration_duration_ms") is not None],
        },
        "health": {
            "status": health,
            "lease_holder": lease.get("holder_id"),
            "lease_heartbeat_at": _iso(lease.get("heartbeat_at")),
            "lease_expires_at": _iso(lease.get("expires_at")),
            "last_evaluated_at": max((_iso(row.get("last_evaluated_at") or row.get("evaluated_at")) or "" for row in since_rows), default=None),
        },
        "pii_included": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reporte read-only del worker CRM SLA shadow")
    parser.add_argument("--cutover-at", default=None, help="override explícito, timezone-aware")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_report(get_db(), cutover_at=args.cutover_at), default=str, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
