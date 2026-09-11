"""Experimental server-side live-capacity variants for Phase 2F.2.

The current productive snapshot remains the reference implementation.  These
builders are read-only audit paths.  They deliberately keep canonical SLA
math and human-event interpretation in Python, while MongoDB reduces the
number of remote reads to one cycle aggregation plus one compact event batch.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .crm_metrics import (
    event_evidence,
    normalize_result,
    calculate_sla,
)
from .crm_sla_alert_evaluator import (
    CLOSED_STAGES,
    EXCLUDED_ORIGINS,
    SLA_STOP_RESULTS,
    SYNTHETIC_PHONES,
)
from .crm_sla_performance_snapshot import _utc
from .crm_sla_snapshot_queries import ReadInstrumentation, aggregate_many
from .crm_sla_alert_settings import CUTOVER_AT


A1_NAME = "A1_AGGREGATION_PER_CYCLE_MINIMAL"
A2_NAME = "A2_AGGREGATION_GROUPED"
MANAGEMENT_EVENT_TYPES = (
    "GESTION_LOG", "HUMAN_NOTE", "CONTACT_RESULT", "MANUAL_ENTRY",
    "gestion_log", "human_note", "contact_result", "manual_entry",
)
HUMAN_ACTOR_EXCLUSIONS = ("system", "bot", "sistema", "none")
HUMAN_ACTOR_TYPES = ("human", "agent", "administrator", "supervisor")


def _live_cycle_query(policy_since: datetime) -> dict[str, Any]:
    """Match the productive current-policy live-capacity population."""
    return {
        "cycle_status": "active",
        "unassigned_at": None,
        "assignment_cycle_id": {"$exists": True, "$ne": None},
        "lead_id": {"$exists": True, "$ne": None},
        "assigned_at": {"$exists": True, "$ne": None, "$gte": policy_since},
        "schema_version": "crm_assignment_cycle_v1",
        "reason": {"$nin": list(EXCLUDED_ORIGINS)},
        "cycle_origin": {"$nin": list(EXCLUDED_ORIGINS)},
        "$or": [
            {"sla_started_at": {"$gte": CUTOVER_AT}},
            {"sla_started_at": {"$exists": False}, "assigned_at": {"$gte": CUTOVER_AT}},
        ],
    }


def _lead_lookup() -> dict[str, Any]:
    return {
        "$lookup": {
            "from": "leads",
            "let": {"lead_key": "$lead_id"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$_id", "$$lead_key"]},
                    # Filter only. Phone is never projected or returned.
                    "phone": {"$nin": list(SYNTHETIC_PHONES)},
                    "lead_origin": {"$nin": list(EXCLUDED_ORIGINS)},
                    "origin": {"$nin": list(EXCLUDED_ORIGINS)},
                    "prospecto.origen": {"$nin": list(EXCLUDED_ORIGINS)},
                }},
                {"$project": {
                    "_id": 0,
                    "pipeline_stage": 1,
                    "stage": 1,
                    "crm_estado": 1,
                    "lead_temperature_effective": 1,
                    "lifecycle.hot_since": 1,
                }},
            ],
            "as": "lead_doc",
        }
    }


def _management_lookup() -> dict[str, Any]:
    return {
        "$lookup": {
            "from": "crm_management_results",
            "let": {"cycle_key": "$assignment_cycle_id"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$assignment_cycle_id", "$$cycle_key"]}}},
                {"$project": {"_id": 0, "result_type": 1, "occurred_at": 1}},
            ],
            "as": "management_results",
        }
    }


def _cycle_projection() -> dict[str, Any]:
    return {
        "_id": 0,
        "lead_id": 1,
        "assignment_cycle_id": 1,
        "assigned_to_user_id": 1,
        "assigned_at": 1,
        "sla_started_at": 1,
        "hot_started_at": 1,
        "temperature_at_assignment": 1,
        "schema_version": 1,
        "lead_found": {"$gt": [{"$size": "$lead_doc"}, 0]},
        "lead": {"$arrayElemAt": ["$lead_doc", 0]},
        "management_results": 1,
    }


def _base_pipeline(policy_since: datetime, *, grouped: bool) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": _live_cycle_query(policy_since)},
        # current_policy_active() keeps the latest active cycle per lead.
        {"$sort": {"assigned_at": -1}},
        {"$group": {"_id": "$lead_id", "cycle": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$cycle"}},
        _lead_lookup(),
        _management_lookup(),
        {"$project": _cycle_projection()},
    ]
    if grouped:
        pipeline.extend([
            {"$group": {
                "_id": "$assigned_to_user_id",
                "cycles": {"$push": {
                    "lead_id": "$lead_id",
                    "assignment_cycle_id": "$assignment_cycle_id",
                    "assigned_to_user_id": "$assigned_to_user_id",
                    "assigned_at": "$assigned_at",
                    "sla_started_at": "$sla_started_at",
                    "hot_started_at": "$hot_started_at",
                    "temperature_at_assignment": "$temperature_at_assignment",
                    "schema_version": "$schema_version",
                    "lead_found": "$lead_found",
                    "lead": "$lead",
                    "management_results": "$management_results",
                }},
            }},
            {"$project": {"_id": 0, "assigned_to_user_id": "$_id", "cycles": 1}},
        ])
    return pipeline


def _string_expression(field: str) -> dict[str, Any]:
    return {"$convert": {"input": field, "to": "string", "onError": "", "onNull": ""}}


def _fallback_string_expression(primary: str, first_fallback: str, second_fallback: str) -> dict[str, Any]:
    return {
        "$let": {
            "vars": {
                "primary_value": _string_expression(primary),
                "first_value": _string_expression(first_fallback),
                "second_value": _string_expression(second_fallback),
            },
            "in": {"$cond": [
                {"$ne": ["$$primary_value", ""]}, "$$primary_value",
                {"$cond": [
                    {"$ne": ["$$first_value", ""]}, "$$first_value", "$$second_value",
                ]},
            ]},
        }
    }


def _actor_type_expression() -> dict[str, Any]:
    return {
        "$let": {
            "vars": {
                "direct_type": _string_expression("$actor_type"),
                "meta_type": _string_expression("$meta.actor_type"),
            },
            "in": {"$toLower": {"$cond": [
                {"$ne": ["$$direct_type", ""]}, "$$direct_type", "$$meta_type",
            ]}},
        }
    }


def _event_compact_projection() -> dict[str, Any]:
    actor_type = _actor_type_expression()
    return {
        "_id": 0,
        "lead_id": 1,
        "type": 1,
        "timestamp": 1,
        "occurred_at": 1,
        # These compact booleans preserve event_evidence() semantics without
        # transferring actor IDs, metadata, messages, emails or phone data.
        "actor_human": {
            "$let": {
                "vars": {
                    "actor_text": {"$toLower": _string_expression("$actor")},
                    "actor_type_text": actor_type,
                },
                "in": {"$and": [
                    {"$ne": ["$$actor_text", ""]},
                    {"$not": [{"$in": ["$$actor_text", list(HUMAN_ACTOR_EXCLUSIONS)]}]},
                    {"$or": [
                        {"$eq": ["$$actor_type_text", ""]},
                        {"$in": ["$$actor_type_text", list(HUMAN_ACTOR_TYPES)]},
                    ]},
                ]},
            }
        },
        "confirmed_effective": {
            "$cond": [
                {"$eq": [{"$type": "$confirmed"}, "missing"]},
                {"$convert": {"input": "$meta.confirmed", "to": "bool", "onError": False, "onNull": False}},
                {"$convert": {"input": "$confirmed", "to": "bool", "onError": False, "onNull": False}},
            ]
        },
        "result_effective": _fallback_string_expression("$result", "$meta.result", "$meta.contact_result"),
    }


def _event_pipeline(lead_ids: list[Any], *, since: datetime, until: datetime) -> list[dict[str, Any]]:
    if not lead_ids:
        return []
    return [
        {"$match": {
            "lead_id": {"$in": lead_ids},
            "type": {"$in": list(MANAGEMENT_EVENT_TYPES)},
            "$or": [
                {"timestamp": {"$gte": since, "$lte": until}},
                {"timestamp": {"$exists": False}, "occurred_at": {"$gte": since, "$lte": until}},
            ],
        }},
        {"$project": _event_compact_projection()},
    ]


def _compact_event(row: Mapping[str, Any]) -> dict[str, Any]:
    human = bool(row.get("actor_human"))
    return {
        "lead_id": row.get("lead_id"),
        "type": row.get("type"),
        "actor": "human" if human else None,
        "actor_type": "human" if human else "system",
        "confirmed": bool(row.get("confirmed_effective")),
        "result": row.get("result_effective") or None,
        "timestamp": row.get("timestamp"),
        "occurred_at": row.get("occurred_at"),
    }


def _flatten_rows(rows: list[Mapping[str, Any]], *, grouped: bool) -> list[dict[str, Any]]:
    if not grouped:
        return [dict(row) for row in rows]
    output: list[dict[str, Any]] = []
    for group in rows:
        output.extend(dict(row) for row in (group.get("cycles") or []))
    return output


def _capacity_from_rows(
    rows: list[Mapping[str, Any]],
    events: list[Mapping[str, Any]],
    *,
    as_of: datetime,
    policy_since: datetime,
) -> tuple[dict[str, dict[str, int]], dict[str, Any]]:
    events_by_lead: dict[str, list[dict[str, Any]]] = {}
    for raw in events:
        compact = _compact_event(raw)
        events_by_lead.setdefault(str(compact.get("lead_id") or ""), []).append(compact)

    capacity: dict[str, dict[str, int]] = {}
    audit = {
        "aggregation_rows": len(rows),
        "lead_documents": sum(1 for row in rows if row.get("lead_found")),
        "lead_missing_or_filtered": 0,
        "lead_closed": 0,
        "owner_missing": 0,
        "management_stop_cycles": 0,
        "human_event_stop_cycles": 0,
        "current_policy_cycles": 0,
        "management_result_rows": sum(len(row.get("management_results") or []) for row in rows),
        "legacy_rows": sum(1 for row in rows if row.get("schema_version") != "crm_assignment_cycle_v1"),
    }
    sla_started = time.perf_counter()
    for cycle in rows:
        audit["current_policy_cycles"] += 1
        if not cycle.get("lead_found") or not isinstance(cycle.get("lead"), Mapping):
            audit["lead_missing_or_filtered"] += 1
            continue
        lead = cycle["lead"]
        stage = str(lead.get("pipeline_stage") or lead.get("stage") or lead.get("crm_estado") or "").upper()
        if stage in CLOSED_STAGES:
            audit["lead_closed"] += 1
            continue
        owner = str(cycle.get("assigned_to_user_id") or "")
        if not owner:
            audit["owner_missing"] += 1
            continue
        row = capacity.setdefault(owner, {"open": 0, "unmanaged": 0, "expired": 0})
        row["open"] += 1

        mgmt_stop = any(
            normalize_result(item.get("result_type")) in SLA_STOP_RESULTS
            for item in (cycle.get("management_results") or [])
        )
        factual_assigned_at = _utc(cycle.get("assigned_at")) or _utc(cycle.get("sla_started_at")) or policy_since
        event_stop = any(
            event_evidence(item).get("management")
            and _utc(item.get("timestamp") or item.get("occurred_at"))
            and _utc(item.get("timestamp") or item.get("occurred_at")) >= factual_assigned_at
            for item in events_by_lead.get(str(cycle.get("lead_id") or ""), [])
        )
        if mgmt_stop:
            audit["management_stop_cycles"] += 1
        if event_stop:
            audit["human_event_stop_cycles"] += 1
        if mgmt_stop or event_stop:
            continue

        row["unmanaged"] += 1
        assigned_at = cycle.get("sla_started_at") or cycle.get("assigned_at")
        if not assigned_at:
            continue
        temperature = str(cycle.get("temperature_at_assignment") or lead.get("lead_temperature_effective") or "NORMAL").upper()
        lifecycle = lead.get("lifecycle") or {}
        hot_start = cycle.get("hot_started_at") or lifecycle.get("hot_since")
        sla = calculate_sla(
            assigned_at=assigned_at,
            now=as_of,
            temperature=temperature,
            hot_started_at=hot_start,
        )
        if sla.get("status") == "critical":
            row["expired"] += 1
    audit["python_sla_ms"] = round((time.perf_counter() - sla_started) * 1000.0, 6)
    return capacity, audit


async def _build_variant(
    db: Any,
    *,
    as_of: datetime,
    policy_since: datetime,
    variant: str,
    instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    grouped = variant == A2_NAME
    cycle_pipeline = _base_pipeline(policy_since, grouped=grouped)
    aggregate_rows = await aggregate_many(
        db["crm_assignment_cycles"], cycle_pipeline,
        instrumentation=instrumentation,
        query_name=f"{variant}.cycles_leads_management",
        batch_size=1000,
    )
    rows = _flatten_rows(aggregate_rows, grouped=grouped)
    lead_ids = list({row.get("lead_id") for row in rows if row.get("lead_id") is not None})
    event_rows = await aggregate_many(
        db["crm_events"],
        _event_pipeline(lead_ids, since=policy_since, until=as_of),
        instrumentation=instrumentation,
        query_name=f"{variant}.human_events_compact",
        batch_size=1000,
    ) if lead_ids else []
    capacity, audit = _capacity_from_rows(rows, event_rows, as_of=as_of, policy_since=policy_since)
    elapsed = (time.perf_counter() - started) * 1000.0
    if instrumentation:
        instrumentation.mark_python(f"{variant}.capacity_sla", audit["python_sla_ms"])
        instrumentation.metadata[f"{variant}.audit"] = audit
    return {
        "variant": variant,
        "policy_version": "crm_sla_reassignment_v1",
        "capacity_metrics": capacity,
        "backlog": capacity,
        "docs_examined": len(rows) + len(event_rows),
        "read_ms": elapsed,
        "query_cost_breakdown": {
            "aggregation_output_rows": len(aggregate_rows),
            "cycle_rows": len(rows),
            "event_rows": len(event_rows),
        },
        "cycle_audit": audit,
        "timing_breakdown": {
            "python_sla_calculation_and_aggregation_ms": audit["python_sla_ms"],
            "python_total_ms": round(max(0.0, elapsed - sum(call.wall_ms for call in instrumentation.calls)) if instrumentation else elapsed, 6),
        },
        "query_shape": {
            "cycle_path": "active current-policy cycles + $lookup leads + $lookup management_results",
            "event_path": "one compact event aggregation; actor/meta reduced to human/result/confirmed flags",
            "variant": variant,
        },
    }


async def build_live_capacity_a1(
    db: Any,
    *,
    as_of: datetime,
    policy_since: datetime,
    instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    return await _build_variant(
        db, as_of=as_of, policy_since=policy_since, variant=A1_NAME,
        instrumentation=instrumentation,
    )


async def build_live_capacity_a2(
    db: Any,
    *,
    as_of: datetime,
    policy_since: datetime,
    instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    return await _build_variant(
        db, as_of=as_of, policy_since=policy_since, variant=A2_NAME,
        instrumentation=instrumentation,
    )


async def build_live_capacity_variant(
    db: Any,
    *,
    variant: str,
    as_of: datetime,
    policy_since: datetime,
    instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    if variant == A1_NAME:
        return await build_live_capacity_a1(db, as_of=as_of, policy_since=policy_since, instrumentation=instrumentation)
    if variant == A2_NAME:
        return await build_live_capacity_a2(db, as_of=as_of, policy_since=policy_since, instrumentation=instrumentation)
    raise ValueError(f"unsupported live capacity variant: {variant}")
