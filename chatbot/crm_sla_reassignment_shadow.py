"""Shadow-only storage, metrics and health contracts for CRM SLA rescue.

The production collection is deliberately not created here.  The adapter is
dry-run by default and the in-memory store is used by tests to prove restart,
deduplication and late-management behavior without MongoDB writes.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


SHADOW_COLLECTION = "crm_sla_reassignment_shadow_v1"
SHADOW_SCHEMA_VERSION = "crm_sla_reassignment_shadow_v1"
SHADOW_POLICY_VERSION = "crm_sla_reassignment_v1"
SHADOW_HISTORY_LIMIT = 20
HEARTBEAT_STATE_KEY = "crm_sla_reassignment_worker_v1"
MANAGEMENT_AFTER_SHADOW_OUTCOME = "SHADOW_WOULD_HAVE_REASSIGNED_BUT_MANAGEMENT_ARRIVED"
SHADOW_WRITE_COLLECTIONS = frozenset({SHADOW_COLLECTION, "crm_worker_leases"})


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
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str:
    parsed = _utc(value)
    return parsed.isoformat() if parsed else str(value or "")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def stable_shadow_document_id(source_cycle_id: Any, policy_version: str = SHADOW_POLICY_VERSION) -> str:
    return f"{policy_version}:{_text(source_cycle_id)}"


def deterministic_shadow_evaluation_id(source_cycle_id: Any, policy_version: str, relevant_state_version: Any) -> str:
    return _hash({"source_cycle_id": _text(source_cycle_id), "policy_version": policy_version, "relevant_state_version": _text(relevant_state_version)})


def management_after_shadow_bucket(minutes: float | None) -> str:
    if minutes is None or minutes < 0:
        return "unknown"
    if minutes <= 5:
        return "<=5 min"
    if minutes <= 15:
        return "6-15 min"
    if minutes <= 30:
        return "16-30 min"
    if minutes <= 60:
        return "31-60 min"
    return ">60 min"


def score_margin_bucket(margin: float | None) -> str:
    if margin is None:
        return "unknown"
    if margin < 1:
        return "<1"
    if margin <= 5:
        return "1-5"
    if margin <= 10:
        return "5-10"
    if margin <= 20:
        return "10-20"
    return ">20"


def build_shadow_document(evaluation: Mapping[str, Any], *, decision: Mapping[str, Any] | None = None, worker_instance_id: str = "", batch_id: str = "", human_management_at: Any = None, capacity_snapshot_at: Any = None) -> dict[str, Any]:
    """Build the PII-free future document; current records remain in memory."""
    decision = decision or {}
    cycle_id = _text(evaluation.get("source_cycle_id") or evaluation.get("assignment_cycle_id"))
    policy_version = _text(evaluation.get("policy_version") or decision.get("policy_version") or SHADOW_POLICY_VERSION)
    relevant_state = _text(evaluation.get("distribution_state_version") or evaluation.get("performance_snapshot_version"))
    evaluated_at = _iso(evaluation.get("evaluated_at"))
    selected_score = evaluation.get("selected_score", decision.get("selected_score"))
    margin = evaluation.get("score_margin", evaluation.get("margin"))
    candidate_ids = list(evaluation.get("candidate_ids") or decision.get("candidate_user_ids") or [])
    selected_id = evaluation.get("selected_user_id", decision.get("selected_user_id"))
    doc = {
        "_id": stable_shadow_document_id(cycle_id, policy_version),
        "shadow_evaluation_id": deterministic_shadow_evaluation_id(cycle_id, policy_version, relevant_state),
        "decision_id": _text(evaluation.get("decision_id") or decision.get("decision_id")),
        "lead_id": _text(evaluation.get("lead_id")),
        "source_cycle_id": cycle_id,
        "evaluated_at": evaluated_at,
        "first_evaluated_at": evaluated_at,
        "last_evaluated_at": evaluated_at,
        "breach_at": _iso(evaluation.get("breach_at")),
        "cutover_at": _iso(evaluation.get("cutover") or evaluation.get("cutover_at")),
        "shadow_cutover_at": _iso(evaluation.get("cutover") or evaluation.get("cutover_at")),
        "policy_version": policy_version,
        "branch": _text(evaluation.get("branch") or decision.get("policy_branch")),
        "eligibility_outcome": "WOULD_REASSIGN" if evaluation.get("would_execute") else _text(evaluation.get("exclusion_reason") or "NOT_ELIGIBLE"),
        "candidate_user_ids": candidate_ids,
        "selected_user_id": _text(selected_id) if selected_id else None,
        "selected_score": selected_score,
        "second_candidate_id": _text(evaluation.get("second_user_id") or evaluation.get("second_candidate_id") or decision.get("second_user_id")) or None,
        "score_margin": margin,
        "score_margin_bucket": score_margin_bucket(float(margin) if margin is not None else None),
        "r2_state_snapshot": copy.deepcopy(evaluation.get("r2_state_snapshot") or {}),
        "j3_target_share_snapshot": copy.deepcopy(evaluation.get("j3_target_share_snapshot") or evaluation.get("jpc_target_share") or {}),
        "performance_snapshot_id": _text(evaluation.get("performance_snapshot_id") or evaluation.get("performance_snapshot_version")),
        "performance_snapshot": copy.deepcopy(evaluation.get("performance_snapshot") or {}),
        "capacity_snapshot_at": _iso(capacity_snapshot_at or evaluation.get("capacity_snapshot_at")),
        "reassignment_number": evaluation.get("assignment_number", evaluation.get("reassignment_number", 0)),
        "would_execute": bool(evaluation.get("would_execute")),
        "exclusion_reason": _text(evaluation.get("exclusion_reason")),
        "worker_instance_id": _text(worker_instance_id),
        "batch_id": _text(batch_id),
        "schema_version": SHADOW_SCHEMA_VERSION,
        "shadow_version": SHADOW_SCHEMA_VERSION,
        "still_eligible": bool(evaluation.get("eligible")) and bool(evaluation.get("would_execute")),
        "human_management_at": _iso(human_management_at) if human_management_at else None,
        "management_after_shadow": bool(human_management_at),
        "management_after_shadow_minutes": None,
        "management_after_shadow_bucket": "never",
        "limited_history": [],
    }
    return doc


def _material_signature(document: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        bool(document.get("would_execute")), _text(document.get("selected_user_id")),
        _text(document.get("eligibility_outcome")), _text(document.get("exclusion_reason")),
    )


class InMemoryShadowStore:
    """Mutable current view plus bounded history, used without Mongo."""

    def __init__(self, *, history_limit: int = SHADOW_HISTORY_LIMIT):
        self.history_limit = int(history_limit)
        self.documents: dict[str, dict[str, Any]] = {}

    def observe(self, document: Mapping[str, Any], *, now: Any = None) -> dict[str, Any]:
        incoming = copy.deepcopy(dict(document))
        key = _text(incoming.get("_id")) or stable_shadow_document_id(incoming.get("source_cycle_id"), _text(incoming.get("policy_version") or SHADOW_POLICY_VERSION))
        current = copy.deepcopy(self.documents.get(key))
        timestamp = _iso(now or incoming.get("evaluated_at"))
        if current is None:
            current = {**incoming, "first_seen_at": timestamp, "last_seen_at": timestamp, "evaluation_count": 0, "shadow_assignment_counted_at": None, "shadow_assignment_count": 0, "history": []}
        current.setdefault("first_evaluated_at", current.get("first_seen_at") or timestamp)
        current["evaluation_count"] = int(current.get("evaluation_count") or 0) + 1
        current["last_seen_at"] = timestamp
        current["last_evaluation_id"] = incoming.get("shadow_evaluation_id")
        before = _material_signature(current)
        after = _material_signature(incoming)
        if current.get("shadow_assignment_counted_at"):
            # Once a cycle would have been reassigned, that hypothetical action
            # stays true in history even if management arrives later.
            incoming["would_execute"] = True
            if incoming.get("exclusion_reason") == "PROTECTED_BY_MANAGEMENT" or incoming.get("human_management_at"):
                incoming["eligibility_outcome"] = MANAGEMENT_AFTER_SHADOW_OUTCOME
                incoming["still_eligible"] = False
            after = _material_signature(incoming)
        if incoming.get("would_execute") and not current.get("shadow_assignment_counted_at"):
            current["shadow_assignment_counted_at"] = timestamp
            current["shadow_assignment_count"] = 1
            current["first_would_execute_at"] = timestamp
        if before != after:
            history_entry = {
                "at": timestamp, "shadow_evaluation_id": incoming.get("shadow_evaluation_id"),
                "decision_id": incoming.get("decision_id"), "eligibility_outcome": incoming.get("eligibility_outcome"),
                "selected_user_id": incoming.get("selected_user_id"), "would_execute": bool(incoming.get("would_execute")),
            }
            current.setdefault("history", []).append(history_entry)
            current["history"] = current["history"][-self.history_limit:]
            current["limited_history"] = copy.deepcopy(current["history"])
        prior_first_evaluated = current.get("first_evaluated_at")
        prior_counted_at = current.get("shadow_assignment_counted_at")
        prior_count = current.get("shadow_assignment_count")
        prior_human_at = current.get("human_management_at")
        prior_after_minutes = current.get("management_after_shadow_minutes")
        prior_after_bucket = current.get("management_after_shadow_bucket")
        current.update(incoming)
        current["first_evaluated_at"] = prior_first_evaluated or current.get("first_evaluated_at") or timestamp
        if prior_counted_at:
            current["shadow_assignment_counted_at"] = prior_counted_at
            current["shadow_assignment_count"] = max(int(prior_count or 0), int(current.get("shadow_assignment_count") or 0), 1)
        if prior_human_at and not incoming.get("human_management_at"):
            current["human_management_at"] = prior_human_at
            current["management_after_shadow_minutes"] = prior_after_minutes
            current["management_after_shadow_bucket"] = prior_after_bucket
            current["management_after_shadow"] = prior_after_minutes is not None
        if current.get("shadow_assignment_counted_at") and incoming.get("human_management_at"):
            decided = _utc(current.get("shadow_assignment_counted_at"))
            arrived = _utc(incoming.get("human_management_at"))
            minutes = ((arrived - decided).total_seconds() / 60.0) if decided and arrived and arrived >= decided else None
            current["human_management_at"] = _iso(arrived) if arrived else incoming.get("human_management_at")
            current["management_after_shadow_minutes"] = minutes
            current["management_after_shadow_bucket"] = management_after_shadow_bucket(minutes)
            current["management_after_shadow"] = minutes is not None
        current["last_evaluated_at"] = timestamp
        current["limited_history"] = copy.deepcopy(current.get("history") or [])[-self.history_limit:]
        current["evaluation_count"] = int(current.get("evaluation_count") or 1)
        current["shadow_assignment_count"] = int(current.get("shadow_assignment_count") or 0)
        self.documents[key] = current
        return copy.deepcopy(current)

    def snapshot(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.documents.values()))


def apply_management_after_shadow(document: Mapping[str, Any], human_management_at: Any, *, now: Any = None) -> dict[str, Any]:
    result = copy.deepcopy(dict(document))
    decided = _utc(result.get("shadow_assignment_counted_at") or result.get("evaluated_at"))
    arrived = _utc(human_management_at)
    minutes = ((arrived - decided).total_seconds() / 60.0) if decided and arrived and arrived >= decided else None
    result["human_management_at"] = _iso(arrived) if arrived else None
    result["management_after_shadow_minutes"] = minutes
    result["management_after_shadow_bucket"] = management_after_shadow_bucket(minutes)
    result["management_after_shadow"] = minutes is not None
    result["eligibility_outcome"] = MANAGEMENT_AFTER_SHADOW_OUTCOME
    result["still_eligible"] = False
    result["last_seen_at"] = _iso(now or arrived)
    result.setdefault("history", []).append({"at": result["last_seen_at"], "eligibility_outcome": MANAGEMENT_AFTER_SHADOW_OUTCOME, "human_management_at": result["human_management_at"], "management_after_shadow_minutes": minutes})
    result["history"] = result["history"][-SHADOW_HISTORY_LIMIT:]
    result["limited_history"] = copy.deepcopy(result["history"])
    return result


def reconstruct_shadow_distribution_state(documents: Any, *, policy_version: str = SHADOW_POLICY_VERSION, cutover_at: Any = None) -> dict[str, Any]:
    """Rebuild prospective R2/J3 state once per counted source cycle."""
    cutover = _utc(cutover_at) if cutover_at is not None else None
    rows = []
    for raw in documents:
        row = dict(raw)
        counted_at = _utc(row.get("shadow_assignment_counted_at"))
        if (
            _text(row.get("policy_version")) == policy_version
            and _text(row.get("shadow_version")) == SHADOW_SCHEMA_VERSION
            and row.get("would_execute") is True
            and (cutover is None or counted_at is not None)
            and (cutover is None or counted_at >= cutover)
        ):
            rows.append(row)
    rows.sort(key=lambda row: (_iso(row.get("shadow_assignment_counted_at") or row.get("evaluated_at")), _text(row.get("_id"))))
    seen: set[str] = set()
    rm_history: list[str] = []
    jpc_counts: dict[str, int] = {}
    counted_ids: list[str] = []
    state_rows: list[dict[str, Any]] = []
    for row in rows:
        cycle_id = _text(row.get("source_cycle_id"))
        if not cycle_id or cycle_id in seen:
            continue
        seen.add(cycle_id)
        counted_ids.append(cycle_id)
        target = _text(row.get("selected_user_id"))
        state_rows.append({"source_cycle_id": cycle_id, "target_user_id": target, "branch": _text(row.get("branch")), "counted_at": _iso(row.get("shadow_assignment_counted_at") or row.get("evaluated_at"))})
        if _text(row.get("branch")) == "RM_GLOBAL_RESCUE":
            if target:
                rm_history.append(target)
        elif _text(row.get("branch")) == "REGION_JPC_MARIA_HERNAN":
            if target:
                jpc_counts[target] = jpc_counts.get(target, 0) + 1
    return {"policy_version": policy_version, "shadow_version": SHADOW_SCHEMA_VERSION, "rm_history": rm_history, "jpc_counts": jpc_counts, "shadow_assignment_counted_cycles": counted_ids, "shadow_assignment_count": len(counted_ids), "distribution_state_version": _hash({"rm_history": rm_history, "jpc_counts": jpc_counts, "cycles": counted_ids}), "state_rows": state_rows}


def shadow_persistence_failure_outcome(error: Any) -> dict[str, Any]:
    return {"status": "SHADOW_STORAGE_FAILURE_FAIL_CLOSED", "error_type": type(error).__name__, "executor_allowed": False, "continue_iteration": False}


async def persist_shadow_evaluation(db: Any, document: Mapping[str, Any], *, dry_run: bool = True, history_limit: int = SHADOW_HISTORY_LIMIT) -> dict[str, Any]:
    """Future adapter.  It cannot write unless the caller explicitly opts in."""
    doc = copy.deepcopy(dict(document))
    if dry_run:
        return {"status": "DRY_RUN", "collection": SHADOW_COLLECTION, "document": doc, "write_performed": False}
    if db is None:
        raise RuntimeError("shadow_db_required")
    collection = db[SHADOW_COLLECTION]
    existing = collection.find_one({"_id": doc.get("_id")})
    if inspect.isawaitable(existing):
        existing = await existing
    if existing:
        merged = InMemoryShadowStore(history_limit=history_limit)
        merged.documents[doc["_id"]] = dict(existing)
        doc = merged.observe(doc)
    op = collection.update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
    if inspect.isawaitable(op):
        await op
    return {"status": "PERSISTED", "collection": SHADOW_COLLECTION, "document": doc, "write_performed": True}


async def persist_shadow_evaluations(
    db: Any,
    documents: Any,
    *,
    dry_run: bool = True,
    history_limit: int = SHADOW_HISTORY_LIMIT,
) -> dict[str, Any]:
    """Persist one worker batch with one read and one bulk write.

    The worker owns a global lease, so the read/merge/write sequence is
    serialized at the deployment level.  The document key is the policy plus
    source cycle, which makes a restart or repeated scan idempotent and keeps
    the hypothetical assignment count at one per source cycle.
    """
    incoming = [copy.deepcopy(dict(row)) for row in (documents or []) if row.get("_id")]
    if dry_run:
        return {"status": "DRY_RUN", "collection": SHADOW_COLLECTION, "documents": incoming, "write_performed": False, "writes": 0}
    if db is None:
        raise RuntimeError("shadow_db_required")
    if not incoming:
        return {"status": "PERSISTED", "collection": SHADOW_COLLECTION, "documents": [], "write_performed": False, "writes": 0}
    collection = db[SHADOW_COLLECTION]
    ids = [row.get("_id") for row in incoming]
    existing_cursor = collection.find({"_id": {"$in": ids}})
    if hasattr(existing_cursor, "to_list"):
        existing_rows = existing_cursor.to_list(length=len(ids))
        if inspect.isawaitable(existing_rows):
            existing_rows = await existing_rows
    else:
        existing_rows = list(existing_cursor or [])
    existing_by_id = {_text(row.get("_id")): row for row in (existing_rows or [])}
    merged_docs: list[dict[str, Any]] = []
    for row in incoming:
        current = existing_by_id.get(_text(row.get("_id")))
        if current:
            store = InMemoryShadowStore(history_limit=history_limit)
            store.documents[_text(row["_id"])] = dict(current)
        else:
            store = InMemoryShadowStore(history_limit=history_limit)
        merged_docs.append(store.observe(row))
    try:
        from pymongo import UpdateOne
    except Exception as exc:  # pragma: no cover - runtime dependency is required
        raise RuntimeError("pymongo_update_one_unavailable") from exc
    operations = [UpdateOne({"_id": row["_id"]}, {"$set": row}, upsert=True) for row in merged_docs]
    outcome = collection.bulk_write(operations, ordered=True)
    if inspect.isawaitable(outcome):
        outcome = await outcome
    return {"status": "PERSISTED", "collection": SHADOW_COLLECTION, "documents": merged_docs, "write_performed": True, "writes": len(operations), "bulk_result": getattr(outcome, "bulk_api_result", None)}


@dataclass(frozen=True)
class WorkerHeartbeat:
    worker_name: str
    instance_id: str
    policy_version: str
    mode: str = "shadow"
    last_started_at: str | None = None
    last_completed_at: str | None = None
    last_success_at: str | None = None
    last_error_at: str | None = None
    duration_ms: float | None = None
    scanned: int = 0
    evaluated: int = 0
    would_execute: int = 0
    cutover: str | None = None
    snapshot_age_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"worker_name": self.worker_name, "instance_id": self.instance_id, "policy_version": self.policy_version, "mode": self.mode, "last_started_at": self.last_started_at, "last_completed_at": self.last_completed_at, "last_success_at": self.last_success_at, "last_error_at": self.last_error_at, "duration_ms": self.duration_ms, "scanned": self.scanned, "evaluated": self.evaluated, "would_execute": self.would_execute, "cutover": self.cutover, "snapshot_age_seconds": self.snapshot_age_seconds}


def build_worker_heartbeat(*, worker_name: str = HEARTBEAT_STATE_KEY, instance_id: str, policy_version: str = SHADOW_POLICY_VERSION, started_at: Any = None, completed_at: Any = None, success_at: Any = None, error_at: Any = None, duration_ms: float | None = None, scanned: int = 0, evaluated: int = 0, would_execute: int = 0, cutover: Any = None, snapshot_age_seconds: float | None = None) -> dict[str, Any]:
    return WorkerHeartbeat(worker_name, instance_id, policy_version, "shadow", _iso(started_at) if started_at else None, _iso(completed_at) if completed_at else None, _iso(success_at) if success_at else None, _iso(error_at) if error_at else None, duration_ms, scanned, evaluated, would_execute, _iso(cutover) if cutover else None, snapshot_age_seconds).to_dict()


def get_sla_reassignment_worker_health(*, worker_enabled: bool, shadow_enabled: bool, heartbeat: Mapping[str, Any] | None = None, now: Any = None, interval_seconds: int = 60, lease_held: bool | None = True, snapshot_age_seconds: float | None = None, snapshot_ttl_seconds: int = 300, config_valid: bool = True, db_error: bool = False) -> dict[str, Any]:
    """Pure health calculation.  It exposes no PII and performs no I/O."""
    current = _utc(now) or datetime.now(timezone.utc)
    if not worker_enabled or not shadow_enabled:
        status = "DISABLED"
    elif not config_valid:
        status = "CONFIG_ERROR"
    elif db_error:
        status = "DB_ERROR"
    elif lease_held is False:
        status = "LEASE_NOT_HELD"
    else:
        completed = _utc((heartbeat or {}).get("last_completed_at") or (heartbeat or {}).get("last_success_at"))
        heartbeat_age = (current - completed).total_seconds() if completed else None
        if heartbeat_age is None or heartbeat_age > max(2 * interval_seconds, 120):
            status = "SHADOW_STALE"
        elif snapshot_age_seconds is not None and snapshot_age_seconds > snapshot_ttl_seconds:
            status = "SNAPSHOT_STALE"
        else:
            status = "SHADOW_HEALTHY"
    return {"status": status, "worker_enabled": bool(worker_enabled), "shadow_enabled": bool(shadow_enabled), "lease_held": lease_held, "snapshot_age_seconds": snapshot_age_seconds, "heartbeat": copy.deepcopy(dict(heartbeat or {})), "pii_included": False}


def kill_switch_truth_table() -> list[dict[str, Any]]:
    return [
        {"reassignment_master": False, "worker": False, "shadow": False, "security": False, "transaction_gate": False, "effect": "all_disabled"},
        {"reassignment_master": False, "worker": True, "shadow": True, "security": False, "transaction_gate": False, "effect": "shadow_only;executor_blocked_by_master"},
        {"reassignment_master": True, "worker": False, "shadow": True, "security": False, "transaction_gate": False, "effect": "worker_disabled;no_iteration"},
        {"reassignment_master": True, "worker": True, "shadow": False, "security": False, "transaction_gate": False, "effect": "shadow_required;no_iteration"},
        {"reassignment_master": True, "worker": True, "shadow": True, "security": False, "transaction_gate": False, "effect": "shadow_only;executor_not_called"},
        {"reassignment_master": True, "worker": True, "shadow": True, "security": True, "transaction_gate": False, "effect": "shadow_only;executor_not_called;security_observation_only"},
        {"reassignment_master": True, "worker": True, "shadow": False, "security": True, "transaction_gate": True, "effect": "executor_gate_still_requires_canary_authorization"},
    ]


def validate_shadow_configuration(
    *,
    worker_enabled: bool,
    shadow_enabled: bool,
    reassignment_enabled: bool,
    cutover_at: Any = None,
    shadow_cutover_at: Any = None,
    production_cutover_at: Any = None,
    policy_version: str = SHADOW_POLICY_VERSION,
    security_enabled: bool = False,
    transaction_gate_enabled: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    configured_shadow_cutover = shadow_cutover_at if shadow_cutover_at not in (None, "") else cutover_at
    try:
        from .crm_sla_reassignment_cutover import parse_explicit_santiago_timestamp
        parsed_cutover = parse_explicit_santiago_timestamp(configured_shadow_cutover)
    except Exception:
        parsed_cutover = None
    if not worker_enabled:
        reasons.append("WORKER_DISABLED")
    if not shadow_enabled:
        reasons.append("SHADOW_DISABLED")
    if not parsed_cutover:
        reasons.append("CUTOVER_MISSING_OR_INVALID")
    if policy_version != SHADOW_POLICY_VERSION:
        reasons.append("POLICY_VERSION_MISMATCH")
    if reassignment_enabled:
        reasons.append("MASTER_REASSIGNMENT_MUST_BE_DISABLED")
    if security_enabled:
        reasons.append("SECURITY_LAYER_MUST_BE_DISABLED")
    if transaction_gate_enabled:
        reasons.append("TRANSACTION_GATE_MUST_BE_DISABLED")
    if production_cutover_at not in (None, ""):
        reasons.append("PRODUCTION_CUTOVER_MUST_BE_EMPTY_IN_SHADOW")
    return {
        "valid": not reasons,
        "reasons": reasons,
        "worker_enabled": worker_enabled,
        "shadow_enabled": shadow_enabled,
        "reassignment_enabled": reassignment_enabled,
        "security_enabled": security_enabled,
        "transaction_gate_enabled": transaction_gate_enabled,
        "cutover_at": parsed_cutover.isoformat() if parsed_cutover else None,
        "shadow_cutover_at": parsed_cutover.isoformat() if parsed_cutover else None,
        "production_cutover_empty": production_cutover_at in (None, ""),
        "executor_must_remain_disabled": True,
    }


def shadow_metrics_plan() -> list[dict[str, Any]]:
    return [
        {"metric": "cycles_scanned", "source": "worker_iteration", "unit": "count", "storage": "heartbeat/metrics", "pii": "no"},
        {"metric": "new_post_cutover_breaches", "source": "canonical_expiration", "unit": "count", "storage": "shadow", "pii": "no"},
        {"metric": "would_execute", "source": "shadow_evaluation", "unit": "count", "storage": "shadow", "pii": "no"},
        {"metric": "protected_before_execution", "source": "management_protection", "unit": "count", "storage": "shadow", "pii": "no"},
        {"metric": "management_after_shadow_minutes", "source": "shadow_then_human_event", "unit": "minutes", "storage": "shadow", "pii": "no"},
        {"metric": "score_margin_bucket", "source": "winner_minus_second", "unit": "bucket", "storage": "shadow", "pii": "no"},
        {"metric": "rm_r2_distribution", "source": "shadow_state", "unit": "counts/share", "storage": "shadow_state", "pii": "no"},
        {"metric": "jpc_j3_share", "source": "shadow_state", "unit": "share", "storage": "shadow_state", "pii": "no"},
        {"metric": "low_winners", "source": "decision", "unit": "count", "storage": "shadow", "pii": "no"},
    ]
