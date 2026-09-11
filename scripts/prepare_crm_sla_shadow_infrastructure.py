"""Prepare the read-only SLA shadow infrastructure.

The default mode is a read-only preflight.  ``--apply`` may create only the
approved non-unique indexes and the two empty coordination/shadow collections.
It never updates business documents, owners, cycles, the committed ledger, or
the worker configuration.  Every infrastructure action is recorded locally in
``docs/auditoria_sla_data/shadow_infrastructure_migration.csv``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from pymongo.errors import CollectionInvalid, OperationFailure


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
AUDIT_PATH = ROOT / "docs" / "auditoria_sla_data" / "shadow_infrastructure_migration.csv"
LEDGER_COLLECTION = "crm_sla_reassignment_audit_v1"
LEASE_COLLECTION = "crm_worker_leases"
SHADOW_COLLECTION = "crm_sla_reassignment_shadow_v1"
POLICY_VERSION = "crm_sla_reassignment_v1"
SHADOW_VERSION = "crm_sla_reassignment_shadow_v1"
APPLY_CONFIRMATION = "APPLY_SLA_SHADOW_INFRA"


REQUIRED_INDEXES: tuple[dict[str, Any], ...] = (
    {
        "object": "I1_ACTIVE_OWNER",
        "collection": "crm_assignment_cycles",
        "type": "index",
        "name": "idx_sla_active_owner_v1",
        "keys": (("cycle_status", 1), ("unassigned_at", 1), ("assigned_at", 1), ("assigned_to_user_id", 1)),
        "purpose": "Fase 2E scanner activo y filtros por ejecutivo",
        "required_phase": "REQUIRED_BEFORE_SHADOW",
    },
    {
        "object": "I2_ACTIVE_START",
        "collection": "crm_assignment_cycles",
        "type": "index",
        "name": "idx_sla_active_start_v1",
        "keys": (("cycle_status", 1), ("unassigned_at", 1), ("sla_started_at", 1)),
        "purpose": "Fase 2E capacidad live y ciclos potencialmente vencidos",
        "required_phase": "REQUIRED_BEFORE_SHADOW",
    },
    {
        "object": "I3_MANAGEMENT_CYCLE_TIME",
        "collection": "crm_management_results",
        "type": "index",
        "name": "idx_management_cycle_occurred_v1",
        "keys": (("assignment_cycle_id", 1), ("occurred_at", 1)),
        "purpose": "protección de gestión por ciclo y temporalidad",
        "required_phase": "REQUIRED_BEFORE_SHADOW",
    },
    {
        "object": "I4_EVENTS_LEAD_TIME",
        "collection": "crm_events",
        "type": "index",
        "name": "idx_crm_events_lead_timestamp_v1",
        "keys": (("lead_id", 1), ("timestamp", 1)),
        "purpose": "evidencia humana/eventos por lead y ventana temporal",
        "required_phase": "REQUIRED_BEFORE_SHADOW",
    },
)


SHADOW_INDEXES: tuple[dict[str, Any], ...] = (
    {
        "object": "SHADOW_SOURCE_CYCLE",
        "collection": SHADOW_COLLECTION,
        "type": "index",
        "name": "idx_shadow_source_cycle_v1",
        "keys": (("source_cycle_id", 1), ("policy_version", 1)),
        "purpose": "actualización mutable y lookup de un source cycle",
        "required_phase": "REQUIRED_BEFORE_SHADOW",
    },
    {
        "object": "SHADOW_STATE_COUNTED",
        "collection": SHADOW_COLLECTION,
        "type": "index",
        "name": "idx_shadow_state_counted_v1",
        "keys": (("policy_version", 1), ("shadow_version", 1), ("would_execute", 1), ("shadow_assignment_counted_at", 1)),
        "purpose": "reconstrucción R2/J3 y detección de ciclos ya contados",
        "required_phase": "REQUIRED_BEFORE_SHADOW",
    },
)


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)


def _keys(index_info: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    raw = index_info.get("key") or {}
    return tuple((str(field), int(direction)) for field, direction in raw.items())


def _key_text(keys: Iterable[tuple[str, int]]) -> str:
    return ",".join(f"{field}:{direction}" for field, direction in keys)


def _find_index_info(meta: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    for info in meta.get("indexes", []):
        if str(info.get("name")) == name:
            return info
    return None


def _collection_meta(db: Any, collection_name: str, *, exact_count: bool = False) -> dict[str, Any]:
    names = set(db.list_collection_names())
    if collection_name not in names:
        return {
            "collection": collection_name,
            "exists": False,
            "count": 0,
            "count_kind": "not_created",
            "indexes": [],
            "stats": {},
        }

    collection = db[collection_name]
    if exact_count:
        count = int(collection.count_documents({}))
        count_kind = "exact"
    else:
        count = int(collection.estimated_document_count())
        count_kind = "estimated"
    try:
        stats = db.command("collStats", collection_name)
    except Exception as exc:  # pragma: no cover - server capability dependent
        stats = {"unavailable": type(exc).__name__}
    try:
        indexes = list(collection.list_indexes())
    except Exception as exc:  # pragma: no cover - server capability dependent
        indexes = [{"name": "LIST_INDEXES_ERROR", "error": type(exc).__name__}]
    return {
        "collection": collection_name,
        "exists": True,
        "count": count,
        "count_kind": count_kind,
        "indexes": indexes,
        "stats": {
            "size": stats.get("size"),
            "storageSize": stats.get("storageSize"),
            "totalIndexSize": stats.get("totalIndexSize"),
            "nindexes": stats.get("nindexes"),
        },
    }


def _validate_fields(db: Any, collection_name: str, fields: Iterable[str]) -> dict[str, Any]:
    collection = db[collection_name]
    found: dict[str, bool] = {}
    errors: dict[str, str] = {}
    for field in fields:
        try:
            found[field] = collection.find_one({field: {"$exists": True}}, {"_id": 1}) is not None
        except Exception as exc:
            found[field] = False
            errors[field] = type(exc).__name__
    return {"all_found": all(found.values()) if found else False, "found": found, "errors": errors}


def _classify_index(spec: Mapping[str, Any], meta: Mapping[str, Any], field_validation: Mapping[str, Any]) -> str:
    if not meta.get("exists") or not field_validation.get("all_found"):
        return "FIELD_NOT_VALIDATED"
    requested = tuple(spec["keys"])
    requested_fields = tuple(field for field, _ in requested)
    for info in meta.get("indexes", []):
        existing = _keys(info)
        hidden_or_incomplete = bool(info.get("hidden") or info.get("sparse") or info.get("partialFilterExpression"))
        if existing == requested:
            return "CONFLICTING_INDEX" if hidden_or_incomplete else "EXACT_EXISTING"
        if len(existing) >= len(requested) and existing[: len(requested)] == requested:
            return "CONFLICTING_INDEX" if hidden_or_incomplete else "PREFIX_USABLE"
        if tuple(field for field, _ in existing) == requested_fields:
            return "CONFLICTING_INDEX"
    return "NEEDS_NEW_INDEX"


def _index_definition(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "keys": list(spec["keys"]),
        "purpose": spec["purpose"],
        "required_phase": spec["required_phase"],
        "unique": False,
    }


def _required_preflight(db: Any) -> dict[str, Any]:
    collections = {spec["collection"] for spec in REQUIRED_INDEXES} | {LEDGER_COLLECTION, LEASE_COLLECTION, SHADOW_COLLECTION}
    metadata = {
        name: _collection_meta(db, name, exact_count=name in {LEDGER_COLLECTION, LEASE_COLLECTION, SHADOW_COLLECTION})
        for name in sorted(collections)
    }
    items: list[dict[str, Any]] = []
    for spec in REQUIRED_INDEXES:
        fields = [field for field, _ in spec["keys"]]
        validation = _validate_fields(db, spec["collection"], fields) if metadata[spec["collection"]]["exists"] else {"all_found": False, "found": {}, "errors": {}}
        status = _classify_index(spec, metadata[spec["collection"]], validation)
        items.append({
            **_index_definition(spec),
            "object": spec["object"],
            "collection": spec["collection"],
            "status": status,
            "field_validation": validation,
            "existing_indexes": [{"name": info.get("name"), "keys": list(_keys(info))} for info in metadata[spec["collection"]].get("indexes", [])],
        })
    return {"collections": metadata, "required_indexes": items}


def _shadow_preflight(db: Any) -> list[dict[str, Any]]:
    metadata = _collection_meta(db, SHADOW_COLLECTION, exact_count=True)
    if not metadata["exists"]:
        return [{
            **_index_definition(spec),
            "object": spec["object"],
            "collection": spec["collection"],
            "status": "NEEDS_NEW_INDEX",
            "field_validation": {"all_found": False, "found": {}, "errors": {}},
            "existing_indexes": [],
        } for spec in SHADOW_INDEXES]
    return [{
        **_index_definition(spec),
        "object": spec["object"],
        "collection": spec["collection"],
        "status": _classify_index(spec, metadata, {"all_found": True}),
        "field_validation": {"all_found": True, "found": {}, "errors": {}},
        "existing_indexes": [{"name": info.get("name"), "keys": list(_keys(info))} for info in metadata.get("indexes", [])],
    } for spec in SHADOW_INDEXES]


def _source_ids(db: Any, *, policy_since: datetime, as_of: datetime, limit: int = 25) -> tuple[list[Any], list[Any]]:
    from chatbot.crm_sla_reassignment_worker import _candidate_query_superset

    query = _candidate_query_superset(policy_since)
    rows = list(db["crm_assignment_cycles"].find(query, {"lead_id": 1, "assignment_cycle_id": 1}).sort([
        ("assigned_at", 1), ("lead_id", 1), ("assignment_cycle_id", 1)
    ]).limit(limit))
    lead_ids = list(dict.fromkeys(row.get("lead_id") for row in rows if row.get("lead_id") is not None))
    cycle_ids = list(dict.fromkeys(row.get("assignment_cycle_id") for row in rows if row.get("assignment_cycle_id") is not None))
    return lead_ids, cycle_ids


def _production_query_shapes(db: Any, *, policy_since: datetime, as_of: datetime) -> dict[str, dict[str, Any]]:
    from chatbot.crm_sla_alert_evaluator import SLA_STOP_RESULTS
    from chatbot.crm_sla_reassignment_worker import _candidate_query_superset
    from chatbot.crm_sla_performance_snapshot import MANAGEMENT_EVENT_TYPES

    lead_ids, cycle_ids = _source_ids(db, policy_since=policy_since, as_of=as_of)
    scanner_query = _candidate_query_superset(policy_since)
    capacity_query = {
        "cycle_status": "active",
        "unassigned_at": None,
        "$or": [
            {"assigned_at": {"$gte": policy_since}},
            {"sla_started_at": {"$gte": policy_since}},
        ],
    }
    management_query = {"assignment_cycle_id": {"$in": cycle_ids}}
    management_time_query = {"assignment_cycle_id": {"$in": cycle_ids}, "occurred_at": {"$gte": policy_since}}
    events_worker_query = {"lead_id": {"$in": lead_ids}}
    events_time_query = {
        "lead_id": {"$in": lead_ids},
        "type": {"$in": list(MANAGEMENT_EVENT_TYPES)},
        "timestamp": {"$gte": policy_since, "$lte": as_of},
    }
    return {
        "scanner": {
            "collection": "crm_assignment_cycles",
            "filter": scanner_query,
            "projection": {"_id": 1, "lead_id": 1, "assignment_cycle_id": 1, "assigned_to_user_id": 1, "assigned_at": 1},
            "sort": {"assigned_at": 1, "lead_id": 1, "assignment_cycle_id": 1},
            "limit": 25,
            "sample_size": len(cycle_ids),
        },
        "capacity": {
            "collection": "crm_assignment_cycles",
            "filter": capacity_query,
            "projection": {"_id": 1, "lead_id": 1, "assignment_cycle_id": 1, "assigned_to_user_id": 1, "assigned_at": 1, "sla_started_at": 1},
            "sort": {},
            "limit": 0,
            "sample_size": len(cycle_ids),
        },
        "management_protection": {
            "collection": "crm_management_results",
            "filter": management_query,
            "projection": {"_id": 1, "assignment_cycle_id": 1, "result_type": 1, "occurred_at": 1},
            "sort": {},
            "limit": 0,
            "sample_size": len(cycle_ids),
        },
        "management_historical_time": {
            "collection": "crm_management_results",
            "filter": management_time_query,
            "projection": {"_id": 1, "assignment_cycle_id": 1, "result_type": 1, "occurred_at": 1},
            "sort": {},
            "limit": 0,
            "sample_size": len(cycle_ids),
        },
        "events_worker_context": {
            "collection": "crm_events",
            "filter": events_worker_query,
            "projection": {"_id": 1, "lead_id": 1, "assignment_cycle_id": 1, "type": 1, "timestamp": 1, "occurred_at": 1},
            "sort": {},
            "limit": 0,
            "sample_size": len(lead_ids),
        },
        "events_timestamp_window": {
            "collection": "crm_events",
            "filter": events_time_query,
            "projection": {"_id": 1, "lead_id": 1, "type": 1, "timestamp": 1, "occurred_at": 1},
            "sort": {},
            "limit": 0,
            "sample_size": len(lead_ids),
        },
        "metadata": {
            "policy_since": policy_since.isoformat(),
            "as_of": as_of.isoformat(),
            "sample_lead_ids": len(lead_ids),
            "sample_cycle_ids": len(cycle_ids),
            "sla_stop_results_count": len(SLA_STOP_RESULTS),
        },
    }


def _stage_names(plan: Any) -> list[str]:
    if not isinstance(plan, Mapping):
        return []
    result = []
    stage = plan.get("stage")
    if stage:
        result.append(str(stage))
    if isinstance(plan.get("queryPlan"), Mapping):
        result.extend(_stage_names(plan["queryPlan"]))
    if isinstance(plan.get("inputStage"), Mapping):
        result.extend(_stage_names(plan["inputStage"]))
    for child in plan.get("inputStages", []) or []:
        result.extend(_stage_names(child))
    for key in ("outerStage", "innerStage"):
        if isinstance(plan.get(key), Mapping):
            result.extend(_stage_names(plan[key]))
    for child in plan.get("shards", {}).values() if isinstance(plan.get("shards"), Mapping) else []:
        result.extend(_stage_names(child))
    return result


def _index_names(plan: Any) -> list[str]:
    if not isinstance(plan, Mapping):
        return []
    result = []
    if plan.get("indexName"):
        result.append(str(plan["indexName"]))
    if isinstance(plan.get("queryPlan"), Mapping):
        result.extend(_index_names(plan["queryPlan"]))
    if isinstance(plan.get("inputStage"), Mapping):
        result.extend(_index_names(plan["inputStage"]))
    for child in plan.get("inputStages", []) or []:
        result.extend(_index_names(child))
    for key in ("outerStage", "innerStage"):
        if isinstance(plan.get(key), Mapping):
            result.extend(_index_names(plan[key]))
    for child in plan.get("shards", {}).values() if isinstance(plan.get("shards"), Mapping) else []:
        result.extend(_index_names(child))
    return result


def _explain(db: Any, shape: Mapping[str, Any]) -> dict[str, Any]:
    if not shape.get("sample_size"):
        return {"status": "INSUFFICIENT_SAMPLE", "nReturned": None, "totalDocsExamined": None, "totalKeysExamined": None, "executionTimeMillis": None, "winningPlan": "INSUFFICIENT_SAMPLE", "stages": [], "index_names": []}
    command: dict[str, Any] = {
        "find": shape["collection"],
        "filter": shape["filter"],
        "projection": shape.get("projection") or {},
    }
    if shape.get("sort"):
        command["sort"] = shape["sort"]
    if shape.get("limit"):
        command["limit"] = int(shape["limit"])
    started = time.perf_counter()
    raw = db.command("explain", command, verbosity="executionStats")
    elapsed = (time.perf_counter() - started) * 1000.0
    execution = raw.get("executionStats") or {}
    planner = raw.get("queryPlanner") or {}
    winning = planner.get("winningPlan") or {}
    stages = _stage_names(winning)
    index_names = _index_names(winning)
    return {
        "status": "OK",
        "nReturned": execution.get("nReturned"),
        "totalDocsExamined": execution.get("totalDocsExamined"),
        "totalKeysExamined": execution.get("totalKeysExamined"),
        "executionTimeMillis": execution.get("executionTimeMillis", round(elapsed, 3)),
        "commandTimeMillis": round(elapsed, 3),
        "winningPlan": str(winning.get("stage") or "UNKNOWN"),
        "stages": stages,
        "index_names": index_names,
        "query_shape": {"collection": shape["collection"], "filter": shape["filter"], "sort": shape.get("sort") or {}, "limit": shape.get("limit", 0)},
    }


def explain_real_queries(db: Any, *, policy_since: datetime, as_of: datetime) -> dict[str, Any]:
    shapes = _production_query_shapes(db, policy_since=policy_since, as_of=as_of)
    explains: dict[str, Any] = {}
    for name, shape in shapes.items():
        if name == "metadata":
            continue
        try:
            explains[name] = _explain(db, shape)
        except Exception as exc:
            explains[name] = {
                "status": "ERROR",
                "error": type(exc).__name__,
                "error_message": str(exc)[:300],
                "nReturned": None,
                "totalDocsExamined": None,
                "totalKeysExamined": None,
                "executionTimeMillis": None,
                "winningPlan": "ERROR",
                "stages": [],
                "index_names": [],
            }
    return {"metadata": shapes["metadata"], "queries": explains, "shapes": shapes}


def _post_validate_index(db: Any, spec: Mapping[str, Any], *, explain: Mapping[str, Any] | None = None) -> dict[str, Any]:
    meta = _collection_meta(db, spec["collection"], exact_count=spec["collection"] in {LEDGER_COLLECTION, LEASE_COLLECTION, SHADOW_COLLECTION})
    info = _find_index_info(meta, str(spec["name"]))
    try:
        db.command("ping")
        connection = "PING_OK"
    except Exception as exc:  # pragma: no cover - server capability dependent
        connection = f"PING_ERROR:{type(exc).__name__}"
    return {
        "index_present": info is not None,
        "index_name": info.get("name") if info else None,
        "index_keys": list(_keys(info)) if info else [],
        "all_indexes": [{"name": row.get("name"), "keys": list(_keys(row))} for row in meta.get("indexes", [])],
        "connection": connection,
        "explain_status": (explain or {}).get("status"),
        "explain_stages": (explain or {}).get("stages", []),
        "explain_index_names": (explain or {}).get("index_names", []),
    }


def _query_uses_index(explain: Mapping[str, Any], index_name: str) -> str:
    if explain.get("status") != "OK":
        return str(explain.get("status") or "UNAVAILABLE")
    names = list(explain.get("index_names") or [])
    if index_name in names:
        return "YES"
    if "COLLSCAN" in set(explain.get("stages") or []):
        return "NO_COLL_SCAN"
    return "INDEX_NOT_SELECTED"


def _write_audit(rows: list[dict[str, Any]]) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    columns = ["object", "collection", "type", "definition", "previous_status", "action", "result", "duration_ms", "post_validation", "rollback_required"]
    with AUDIT_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def _audit_row(*, obj: str, collection: str, object_type: str, definition: Any, previous_status: str, action: str, result: str, duration_ms: float | None, post_validation: Any, rollback_required: str = "NO") -> dict[str, Any]:
    return {
        "object": obj,
        "collection": collection,
        "type": object_type,
        "definition": _json(definition),
        "previous_status": previous_status,
        "action": action,
        "result": result,
        "duration_ms": "" if duration_ms is None else f"{duration_ms:.3f}",
        "post_validation": _json(post_validation),
        "rollback_required": rollback_required,
    }


def _integrity_snapshot(db: Any) -> dict[str, Any]:
    names = set(db.list_collection_names())
    values: dict[str, Any] = {}
    for name in ("leads", "crm_assignment_cycles", "crm_management_results", "crm_events", LEDGER_COLLECTION, SHADOW_COLLECTION, LEASE_COLLECTION):
        if name not in names:
            values[name] = {"exists": False, "count": 0}
            continue
        collection = db[name]
        values[name] = {"exists": True, "count": int(collection.estimated_document_count())}
        if name == LEDGER_COLLECTION:
            values[name]["committed_events"] = int(collection.count_documents({"event_type": "SLA_REASSIGNMENT_COMMITTED"}))
        if name == SHADOW_COLLECTION:
            values[name]["documents"] = int(collection.count_documents({}))
        if name == LEASE_COLLECTION:
            values[name]["active_leases"] = int(collection.count_documents({"expires_at": {"$gt": datetime.now(timezone.utc)}}))
    return values


def _connect_db() -> Any:
    from chatbot.storage import get_db
    return get_db()


def run(*, db: Any, apply: bool, confirm: str, as_of: datetime | None = None) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc)
    from chatbot.crm_metrics import INSTRUMENTATION_CUTOVER, coerce_utc_datetime

    policy_since = coerce_utc_datetime(INSTRUMENTATION_CUTOVER)
    if not policy_since:
        raise RuntimeError("INSTRUMENTATION_CUTOVER_UNAVAILABLE")

    preflight = _required_preflight(db)
    shadow_items = _shadow_preflight(db)
    baseline_integrity = _integrity_snapshot(db)
    baseline_explain = explain_real_queries(db, policy_since=policy_since, as_of=as_of)
    rows: list[dict[str, Any]] = []

    for item in preflight["required_indexes"] + shadow_items:
        if item["status"] in {"CONFLICTING_INDEX", "FIELD_NOT_VALIDATED"}:
            rows.append(_audit_row(
                obj=item["object"], collection=item["collection"], object_type="index",
                definition={"name": item["name"], "keys": item["keys"]},
                previous_status=item["status"], action="BLOCKED", result=item["status"],
                duration_ms=None, post_validation={"field_validation": item["field_validation"]}, rollback_required="NO",
            ))

    blockers = [item for item in preflight["required_indexes"] + shadow_items if item["status"] in {"CONFLICTING_INDEX", "FIELD_NOT_VALIDATED"}]
    if apply and confirm != APPLY_CONFIRMATION:
        raise RuntimeError(f"--apply requiere --confirm {APPLY_CONFIRMATION}")
    if apply and blockers:
        _write_audit(rows)
        return {
            "mode": "blocked_before_apply", "preflight": preflight, "shadow_indexes": shadow_items,
            "baseline_explain": baseline_explain, "baseline_integrity": baseline_integrity,
            "post_integrity": baseline_integrity, "audit_path": str(AUDIT_PATH), "audit_rows": rows,
        }

    if apply:
        for spec, item in [(spec, next(x for x in preflight["required_indexes"] if x["object"] == spec["object"])) for spec in REQUIRED_INDEXES] + [(spec, next(x for x in shadow_items if x["object"] == spec["object"])) for spec in SHADOW_INDEXES]:
            if item["status"] in {"EXACT_EXISTING", "PREFIX_USABLE"}:
                rows.append(_audit_row(
                    obj=spec["object"], collection=spec["collection"], object_type="index",
                    definition=_index_definition(spec), previous_status=item["status"], action="SKIPPED",
                    result="SKIPPED", duration_ms=0.0, post_validation={"status": item["status"]}, rollback_required="NO",
                ))
                continue
            started = time.perf_counter()
            try:
                db[spec["collection"]].create_index(list(spec["keys"]), name=spec["name"], unique=False)
                duration = (time.perf_counter() - started) * 1000.0
                rows.append(_audit_row(
                    obj=spec["object"], collection=spec["collection"], object_type="index",
                    definition=_index_definition(spec), previous_status=item["status"], action="CREATE",
                    result="CREATED", duration_ms=duration, post_validation={"created": True}, rollback_required="DECISION_REQUIRED",
                ))
                latest_explain = explain_real_queries(db, policy_since=policy_since, as_of=as_of)
                relevant = _relevant_explain(spec, latest_explain)
                validation = _post_validate_index(db, spec, explain=relevant)
                rows[-1]["post_validation"] = _json(validation)
                if not validation["index_present"] or validation["connection"] != "PING_OK":
                    raise RuntimeError(f"POST_VALIDATION_FAILED:{spec['name']}")
                smoke = list(db[spec["collection"]].find({}, {"_id": 1}).limit(1))
                rows[-1]["post_validation"] = _json({**validation, "smoke_read": "OK", "smoke_count": len(smoke)})
            except Exception as exc:
                duration = (time.perf_counter() - started) * 1000.0
                rows.append(_audit_row(
                    obj=spec["object"], collection=spec["collection"], object_type="index",
                    definition=_index_definition(spec), previous_status=item["status"], action="ERROR",
                    result=f"ERROR:{type(exc).__name__}", duration_ms=duration,
                    post_validation={"error": str(exc)[:300]}, rollback_required="DECISION_REQUIRED",
                ))
                _write_audit(rows)
                return {
                    "mode": "apply_stopped_on_error", "preflight": preflight, "shadow_indexes": shadow_items,
                    "baseline_explain": baseline_explain, "post_explain": explain_real_queries(db, policy_since=policy_since, as_of=as_of),
                    "baseline_integrity": baseline_integrity, "post_integrity": _integrity_snapshot(db),
                    "audit_path": str(AUDIT_PATH), "audit_rows": rows,
                }

        for collection_name, obj in ((LEASE_COLLECTION, "LEASE_COLLECTION"), (SHADOW_COLLECTION, "SHADOW_COLLECTION")):
            started = time.perf_counter()
            before = _collection_meta(db, collection_name, exact_count=True)
            if before["exists"]:
                rows.append(_audit_row(
                    obj=obj, collection=collection_name, object_type="collection",
                    definition={"empty": True}, previous_status="EXACT_EXISTING", action="SKIPPED",
                    result="SKIPPED", duration_ms=0.0, post_validation={"exists": True, "count": before["count"]}, rollback_required="NO",
                ))
                continue
            try:
                db.create_collection(collection_name)
                duration = (time.perf_counter() - started) * 1000.0
                after = _collection_meta(db, collection_name, exact_count=True)
                rows.append(_audit_row(
                    obj=obj, collection=collection_name, object_type="collection",
                    definition={"empty": True}, previous_status="NOT_CREATED", action="CREATE",
                    result="CREATED", duration_ms=duration,
                    post_validation={"exists": after["exists"], "count": after["count"], "indexes": [{"name": i.get("name"), "keys": list(_keys(i))} for i in after.get("indexes", [])]},
                    rollback_required="DECISION_REQUIRED",
                ))
            except CollectionInvalid:
                after = _collection_meta(db, collection_name, exact_count=True)
                rows.append(_audit_row(
                    obj=obj, collection=collection_name, object_type="collection",
                    definition={"empty": True}, previous_status="NOT_CREATED", action="SKIPPED",
                    result="SKIPPED_RACE", duration_ms=(time.perf_counter() - started) * 1000.0,
                    post_validation={"exists": after["exists"], "count": after["count"]}, rollback_required="NO",
                ))
            except Exception as exc:
                rows.append(_audit_row(
                    obj=obj, collection=collection_name, object_type="collection",
                    definition={"empty": True}, previous_status="NOT_CREATED", action="ERROR",
                    result=f"ERROR:{type(exc).__name__}", duration_ms=(time.perf_counter() - started) * 1000.0,
                    post_validation={"error": str(exc)[:300]}, rollback_required="DECISION_REQUIRED",
                ))
                _write_audit(rows)
                return {
                    "mode": "apply_stopped_on_error", "preflight": preflight, "shadow_indexes": shadow_items,
                    "baseline_explain": baseline_explain, "post_explain": explain_real_queries(db, policy_since=policy_since, as_of=as_of),
                    "baseline_integrity": baseline_integrity, "post_integrity": _integrity_snapshot(db),
                    "audit_path": str(AUDIT_PATH), "audit_rows": rows,
                }

    post_explain = explain_real_queries(db, policy_since=policy_since, as_of=as_of)
    post_integrity = _integrity_snapshot(db)
    if not apply:
        for item in preflight["required_indexes"] + shadow_items:
            action = "SKIPPED" if item["status"] in {"EXACT_EXISTING", "PREFIX_USABLE"} else "DRY_RUN"
            result = item["status"] if action == "SKIPPED" else ("BLOCKED" if item["status"] in {"CONFLICTING_INDEX", "FIELD_NOT_VALIDATED"} else "WOULD_CREATE")
            rows.append(_audit_row(
                obj=item["object"], collection=item["collection"], object_type="index",
                definition={"name": item["name"], "keys": item["keys"]}, previous_status=item["status"], action=action,
                result=result, duration_ms=0.0, post_validation={"baseline_explain": _relevant_explain_by_object(item["object"], baseline_explain)}, rollback_required="NO",
            ))
        for collection_name, obj in ((LEASE_COLLECTION, "LEASE_COLLECTION"), (SHADOW_COLLECTION, "SHADOW_COLLECTION")):
            meta = preflight["collections"].get(collection_name, {})
            rows.append(_audit_row(
                obj=obj, collection=collection_name, object_type="collection", definition={"empty": True},
                previous_status="EXACT_EXISTING" if meta.get("exists") else "NOT_CREATED", action="SKIPPED" if meta.get("exists") else "DRY_RUN",
                result="SKIPPED" if meta.get("exists") else "WOULD_CREATE_EMPTY", duration_ms=0.0,
                post_validation={"exists": meta.get("exists", False), "count": meta.get("count", 0)}, rollback_required="NO",
            ))

    _write_audit(rows)
    return {
        "mode": "apply_complete" if apply else "dry_run",
        "preflight": preflight,
        "shadow_indexes": shadow_items,
        "baseline_explain": baseline_explain,
        "post_explain": post_explain,
        "baseline_integrity": baseline_integrity,
        "post_integrity": post_integrity,
        "audit_path": str(AUDIT_PATH),
        "audit_rows": rows,
    }


def _relevant_explain(spec: Mapping[str, Any], explain: Mapping[str, Any]) -> Mapping[str, Any]:
    return _relevant_explain_by_object(str(spec["object"]), explain)


def _relevant_explain_by_object(obj: str, explain: Mapping[str, Any]) -> Mapping[str, Any]:
    mapping = {
        "I1_ACTIVE_OWNER": "scanner",
        "I2_ACTIVE_START": "capacity",
        "I3_MANAGEMENT_CYCLE_TIME": "management_historical_time",
        "I4_EVENTS_LEAD_TIME": "events_timestamp_window",
        "SHADOW_SOURCE_CYCLE": None,
        "SHADOW_STATE_COUNTED": None,
    }
    query = mapping.get(obj)
    return (explain.get("queries") or {}).get(query, {"status": "NOT_APPLICABLE"}) if query else {"status": "NOT_APPLICABLE"}


def _print_human_summary(result: Mapping[str, Any]) -> None:
    for row in result.get("audit_rows", []):
        action = str(row.get("action") or "")
        previous = str(row.get("previous_status") or "")
        raw_result = str(row.get("result") or "")
        if action == "CREATE" and raw_result == "CREATED":
            status = "CREATED"
        elif action == "ERROR" or raw_result.startswith("ERROR"):
            status = "ERROR"
        elif action == "BLOCKED" or raw_result in {"CONFLICTING_INDEX", "FIELD_NOT_VALIDATED"}:
            status = "BLOCKED"
        elif previous == "EXACT_EXISTING":
            status = "EXISTS"
        else:
            status = "SKIPPED"
        print(f"{status} object={row.get('object')} collection={row.get('collection')} action={action} result={raw_result}")
    print(json.dumps({
        "mode": result.get("mode"),
        "audit_path": result.get("audit_path"),
        "integrity": {"baseline": result.get("baseline_integrity"), "post": result.get("post_integrity")},
    }, default=str, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare CRM SLA shadow infrastructure")
    parser.add_argument("--dry-run", action="store_true", help="read-only preflight; this is the default")
    parser.add_argument("--apply", action="store_true", help="create only approved indexes and empty auxiliary collections")
    parser.add_argument("--confirm", default="", help=f"must equal {APPLY_CONFIRMATION} with --apply")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply y --dry-run son mutuamente excluyentes")
    try:
        result = run(db=_connect_db(), apply=args.apply, confirm=args.confirm)
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    _print_human_summary(result)
    return 0 if result.get("mode") not in {"blocked_before_apply", "apply_stopped_on_error"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
