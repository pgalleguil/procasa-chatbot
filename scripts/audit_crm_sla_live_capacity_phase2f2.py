"""Read-only Phase 2F.2 audit for server-side live-capacity variants.

The script captures the productive live capacity once, benchmarks the current
path against exactly two experimental aggregation variants, checks explain
plans and network floor, and writes only local CSV audit artifacts.  It never
executes a reassignment or a MongoDB write.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from pymongo import ReadPreference

from chatbot.crm_metrics import INSTRUMENTATION_CUTOVER, coerce_utc_datetime
from chatbot.crm_sla_live_capacity import (
    A1_NAME,
    A2_NAME,
    _base_pipeline,
    _event_pipeline,
    build_live_capacity_variant,
)
from chatbot.crm_sla_performance_snapshot import (
    build_historical_performance_snapshot,
    build_live_capacity_snapshot,
)
from chatbot.crm_sla_reassignment_worker import _batch_context
from chatbot.crm_sla_snapshot_queries import MongoReadCommandListener, ReadInstrumentation
from config import Config


# Imported lazily below to keep this script's audit helpers independent from
# the previous phase's command-line entry point.
ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "auditoria_sla_data"
OUTPUT_FILES = {
    "comparison": OUTPUT_DIR / "live_capacity_aggregation_comparison.csv",
    "explain": OUTPUT_DIR / "live_capacity_explain.csv",
    "equivalence": OUTPUT_DIR / "live_capacity_equivalence.csv",
    "network": OUTPUT_DIR / "network_latency_floor.csv",
}
WRITE_COMMANDS = {
    "insert", "update", "delete", "findAndModify", "bulkWrite",
    "renameCollection", "drop", "dropDatabase", "create", "createIndexes",
    "dropIndexes",
}
READ_COMMANDS = {"find", "getMore", "aggregate", "count", "distinct", "listCollections", "listIndexes"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _write_csv(path: Path, rows: list[Mapping[str, Any]], fields: list[str]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))


class AuditSession:
    def __init__(self) -> None:
        if not Config.MONGO_URI:
            raise RuntimeError("MONGO_URI no está configurado")
        from motor.motor_asyncio import AsyncIOMotorClient

        self.listener = MongoReadCommandListener()
        self.client = AsyncIOMotorClient(
            Config.MONGO_URI,
            read_preference=ReadPreference.PRIMARY_PREFERRED,
            event_listeners=[self.listener],
            socketTimeoutMS=30000,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=10000,
            maxIdleTimeMS=45000,
            appname="crm-sla-phase2f2-read-audit",
        )
        self.db = self.client[Config.DB_NAME]

    async def ping(self) -> float:
        started = time.perf_counter()
        await self.db.command("ping")
        return (time.perf_counter() - started) * 1000.0

    def close(self) -> None:
        self.client.close()


async def _measure(
    session: AuditSession,
    callback: Callable[[ReadInstrumentation], Awaitable[Any]],
) -> dict[str, Any]:
    instrumentation = ReadInstrumentation(measure_bson_bytes=True)
    command_start = len(session.listener.commands)
    started = time.perf_counter()
    result: Any = None
    error = ""
    try:
        result = await callback(instrumentation)
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
    wall_ms = (time.perf_counter() - started) * 1000.0
    commands = session.listener.commands[command_start:]
    reads = [row for row in commands if row.get("operation") in READ_COMMANDS]
    summary = instrumentation.summary()
    timing = result.get("timing_breakdown") if isinstance(result, Mapping) else {}
    if isinstance(timing, Mapping) and timing.get("python_total_ms") is not None:
        python_ms = _number(timing.get("python_total_ms"))
    elif isinstance(timing, Mapping):
        python_ms = sum(
            _number(value) for key, value in timing.items()
            if str(key).startswith("python_")
            and str(key) != "python_snapshot_construction_ms"
        )
    else:
        python_ms = max(0.0, wall_ms - _number(summary.get("read_wall_span_ms")))
    return {
        "result": result,
        "instrumentation": instrumentation,
        "wall_ms": wall_ms,
        "error": error,
        "logical_reads": summary.get("mongo_round_trips_total", 0),
        "wire_commands": len(reads),
        "wire_cursor_batches": sum(1 for row in reads if row.get("operation") in {"find", "getMore", "aggregate"}),
        "command_roundtrip_ms": sum(_number(row.get("command_roundtrip_ms")) for row in reads),
        "read_wall_span_ms": summary.get("read_wall_span_ms", 0.0),
        "python_ms": python_ms,
        "documents": summary.get("documents_returned", 0),
        "bson_bytes": summary.get("bson_bytes_estimate", 0),
        "all_projections_pii_free": summary.get("all_projections_pii_free", True),
    }


async def _repeat(
    session: AuditSession,
    repetitions: int,
    callback: Callable[[ReadInstrumentation], Awaitable[Any]],
) -> list[dict[str, Any]]:
    rows = []
    for _ in range(repetitions):
        rows.append(await _measure(session, callback))
    return rows


def _benchmark_row(
    name: str,
    samples: list[dict[str, Any]],
    *,
    target_ms: float,
    explain_server_ms: float | None = None,
) -> dict[str, Any]:
    valid = [row for row in samples if not row["error"]]
    walls = [float(row["wall_ms"]) for row in valid]
    first = valid[0] if valid else {}
    p90 = _percentile(walls, 0.90)
    return {
        "path": name,
        "samples": len(walls),
        "p50_ms": round(_percentile(walls, 0.50), 6),
        "p90_ms": round(p90, 6),
        "p95_ms": round(_percentile(walls, 0.95), 6),
        "max_ms": round(max(walls) if walls else 0.0, 6),
        "server_execution_ms_explain": "" if explain_server_ms is None else round(explain_server_ms, 6),
        "command_roundtrip_ms": round(_number(first.get("command_roundtrip_ms")), 6),
        "python_ms": round(_number(first.get("python_ms")), 6),
        "wire_commands": first.get("wire_commands", 0),
        "wire_cursor_batches": first.get("wire_cursor_batches", 0),
        "logical_reads": first.get("logical_reads", 0),
        "documents_transferred": first.get("documents", 0),
        "bson_bytes_estimate": first.get("bson_bytes", 0),
        "pii_free": "PASS" if all(row.get("all_projections_pii_free") for row in valid) else "FAIL",
        "target_ms": target_ms,
        "status": "PASS" if p90 < target_ms else "FAIL",
        "error_samples": len(samples) - len(valid),
    }


def _capacity_pressure(values: Mapping[str, Any]) -> int:
    return (
        3 * int(_number(values.get("expired")))
        + 2 * int(_number(values.get("unmanaged")))
        + int(_number(values.get("open")))
    )


def _capacity_equivalence_rows(reference: Mapping[str, Any], variant: str, candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    owners = sorted(set(reference) | set(candidate))
    rows: list[dict[str, Any]] = []
    for owner in owners:
        ref = reference.get(owner) or {}
        got = candidate.get(owner) or {}
        fields = ("open", "unmanaged", "expired", "load_pressure")
        for field in fields:
            ref_value = _capacity_pressure(ref) if field == "load_pressure" else int(_number(ref.get(field)))
            got_value = _capacity_pressure(got) if field == "load_pressure" else int(_number(got.get(field)))
            difference = got_value - ref_value
            rows.append({
                "scope": "capacity",
                "variant": variant,
                "entity_id": owner,
                "field": field,
                "reference_value": ref_value,
                "variant_value": got_value,
                "difference": difference,
                "status": "PASS" if difference == 0 else "FAIL",
                "notes": "load_pressure=3*expired+2*unmanaged+open" if field == "load_pressure" else "REFERENCE_LIVE_CAPACITY",
            })
    return rows


def _cycle_equivalence_rows(reference: Mapping[str, Any], variant: str, candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields = (
        "current_policy_cycles", "lead_documents", "lead_missing_or_filtered",
        "lead_closed", "owner_missing", "management_stop_cycles",
        "human_event_stop_cycles", "management_result_rows", "legacy_rows",
    )
    rows = []
    for field in fields:
        ref_value = reference.get(field, "")
        got_value = candidate.get(field, "")
        rows.append({
            "scope": "cycle_data",
            "variant": variant,
            "entity_id": "ALL",
            "field": field,
            "reference_value": ref_value,
            "variant_value": got_value,
            "difference": "" if ref_value == "" else _number(got_value) - _number(ref_value),
            "status": "PASS" if ref_value == got_value else "FAIL",
            "notes": "same current-policy source population",
        })
    return rows


def _selection_signature(result: Mapping[str, Any]) -> dict[str, tuple[Any, ...]]:
    output = {}
    for row in result.get("evaluations") or []:
        key = f"{row.get('lead_id', '')}|{row.get('source_cycle_id', '')}"
        output[key] = (
            row.get("branch", ""), row.get("exclusion_reason", ""),
            tuple(row.get("candidate_ids") or []), row.get("selected_user_id"),
            row.get("second_user_id"), row.get("selected_score"),
            row.get("guardrail_reason", row.get("guardrail", "")),
            bool(row.get("would_execute")),
        )
    return output


async def _run_worker_in_audit(
    *,
    db: Any,
    snapshot: Mapping[str, Any],
    as_of: datetime,
    scan_result: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    cutover_at: datetime | None = None,
    instrumentation: ReadInstrumentation | None = None,
) -> dict[str, Any]:
    import chatbot.crm_sla_reassignment_worker as worker

    original_cfg = worker._cfg
    overrides = {
        "CRM_SLA_REASSIGNMENT_WORKER_ENABLED": True,
        "CRM_SLA_REASSIGNMENT_SHADOW_ENABLED": True,
    }
    worker._cfg = lambda name, default: overrides.get(name, original_cfg(name, default))
    try:
        from chatbot.crm_sla_reassignment_worker import run_sla_reassignment_worker_iteration

        return await run_sla_reassignment_worker_iteration(
            db=db,
            now=as_of,
            cutover_at=cutover_at or coerce_utc_datetime(INSTRUMENTATION_CUTOVER),
            batch_size=25,
            scan_result=scan_result,
            context=context,
            performance_snapshot=snapshot,
            committed_events=[],
            read_instrumentation=instrumentation,
        )
    finally:
        worker._cfg = original_cfg


def _combine_snapshot(historical: Mapping[str, Any], live: Mapping[str, Any]) -> dict[str, Any]:
    combined = dict(historical)
    capacity = dict(live.get("capacity_metrics") or {})
    candidates = []
    for raw in historical.get("candidates") or []:
        row = dict(raw)
        current = capacity.get(str(row.get("user_id") or ""), {})
        row["open_current_policy"] = current.get("open", row.get("open_current_policy", 0))
        row["unmanaged_current_policy"] = current.get("unmanaged", row.get("unmanaged_current_policy", 0))
        row["expired_current_policy"] = current.get("expired", row.get("expired_current_policy", 0))
        candidates.append(row)
    combined["candidates"] = candidates
    combined["backlog"] = capacity
    combined["capacity_snapshot_at"] = live.get("generated_at", "")
    combined["live_capacity_snapshot_id"] = live.get("variant", "REFERENCE_LIVE_CAPACITY")
    return combined


def _explain_walk(node: Any, state: dict[str, Any]) -> None:
    if isinstance(node, Mapping):
        stage = node.get("stage")
        if stage:
            state["stages"].add(str(stage))
            if stage == "COLLSCAN":
                state["collscan"] = True
        if "indexName" in node:
            state["indexes"].add(str(node["indexName"]))
        for index in node.get("indexesUsed") or []:
            state["indexes"].add(str(index))
        if node.get("collectionScans"):
            state["collscan"] = True
        if "$lookup" in node and isinstance(node["$lookup"], Mapping):
            lookup = node["$lookup"]
            state["lookups"].append({
                "from": lookup.get("from", ""),
                "indexesUsed": list(lookup.get("indexesUsed") or []),
                "collectionScans": lookup.get("collectionScans", 0),
            })
        if isinstance(node.get("executionStats"), Mapping):
            _explain_walk(node["executionStats"], state)
        for value in node.values():
            if value is not node.get("executionStats"):
                _explain_walk(value, state)
    elif isinstance(node, list):
        for value in node:
            _explain_walk(value, state)


def _extract_explain_metrics(explain: Mapping[str, Any]) -> tuple[float, int, int, int, str, list[dict[str, Any]], bool]:
    state: dict[str, Any] = {"stages": set(), "indexes": set(), "lookups": [], "collscan": False}
    _explain_walk(explain, state)
    execution_values: list[float] = []
    docs_values: list[int] = []
    keys_values: list[int] = []
    returned_values: list[int] = []

    def collect(node: Any) -> None:
        if isinstance(node, Mapping):
            if node.get("executionTimeMillis") is not None:
                execution_values.append(_number(node.get("executionTimeMillis")))
            if node.get("totalDocsExamined") is not None:
                docs_values.append(int(_number(node.get("totalDocsExamined"))))
            if node.get("totalKeysExamined") is not None:
                keys_values.append(int(_number(node.get("totalKeysExamined"))))
            if node.get("nReturned") is not None:
                returned_values.append(int(_number(node.get("nReturned"))))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(explain)
    return (
        max(execution_values) if execution_values else 0.0,
        max(docs_values) if docs_values else 0,
        max(keys_values) if keys_values else 0,
        max(returned_values) if returned_values else 0,
        "|".join(sorted(state["stages"])),
        state["lookups"],
        bool(state["collscan"]),
    )


async def _explain_aggregate(db: Any, collection: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        explain = await db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {"batchSize": 1000}},
            verbosity="executionStats",
        )
        execution_ms, docs, keys, returned, stages, lookups, collscan = _extract_explain_metrics(explain)
        indexes = set()
        state = {"stages": set(), "indexes": indexes, "lookups": [], "collscan": False}
        _explain_walk(explain, state)
        return {
            "execution_time_ms": round(execution_ms, 6),
            "docs_examined": docs,
            "keys_examined": keys,
            "returned": returned,
            "stages": stages,
            "indexes": "|".join(sorted(indexes)),
            "lookups": _json(lookups),
            "collscan": "FAIL" if collscan else "PASS",
            "status": "PASS" if not collscan else "REVIEW_REQUIRED",
            "error": "",
        }
    except Exception as exc:
        return {
            "execution_time_ms": "", "docs_examined": "", "keys_examined": "", "returned": "",
            "stages": "", "indexes": "", "lookups": "", "collscan": "", "status": "ERROR",
            "error": type(exc).__name__,
        }


async def _network_samples(session: AuditSession, repetitions: int) -> tuple[list[float], list[float]]:
    await session.ping()
    await session.db["crm_assignment_cycles"].find_one({}, {"_id": 1})
    pings: list[float] = []
    finds: list[float] = []
    for _ in range(repetitions):
        started = time.perf_counter()
        await session.db.command("ping")
        pings.append((time.perf_counter() - started) * 1000.0)
        started = time.perf_counter()
        await session.db["crm_assignment_cycles"].find_one({}, {"_id": 1})
        finds.append((time.perf_counter() - started) * 1000.0)
    return pings, finds


async def main(repetitions: int) -> int:
    policy_since = coerce_utc_datetime(INSTRUMENTATION_CUTOVER)
    if not policy_since:
        raise RuntimeError("cutover productivo inválido")
    as_of = datetime.now(timezone.utc)
    session = AuditSession()
    try:
        # Warm-up and frozen reference. The reference is captured before the
        # experimental paths and reused for every equivalence comparison.
        await session.ping()
        reference_live = await build_live_capacity_snapshot(session.db, as_of=as_of, policy_since=policy_since)
        historical = await build_historical_performance_snapshot(session.db, as_of=as_of, policy_since=policy_since)
        a1_warm = await build_live_capacity_variant(session.db, variant=A1_NAME, as_of=as_of, policy_since=policy_since)
        a2_warm = await build_live_capacity_variant(session.db, variant=A2_NAME, as_of=as_of, policy_since=policy_since)

        old_samples = await _repeat(
            session, repetitions,
            lambda inst: build_live_capacity_snapshot(session.db, as_of=as_of, policy_since=policy_since, instrumentation=inst),
        )
        a1_samples = await _repeat(
            session, repetitions,
            lambda inst: build_live_capacity_variant(session.db, variant=A1_NAME, as_of=as_of, policy_since=policy_since, instrumentation=inst),
        )
        a2_samples = await _repeat(
            session, repetitions,
            lambda inst: build_live_capacity_variant(session.db, variant=A2_NAME, as_of=as_of, policy_since=policy_since, instrumentation=inst),
        )

        # Explain uses the actual current-policy population for event IDs, but
        # does not retain those IDs in any artifact.
        cycle_rows = await session.db["crm_assignment_cycles"].find(
            {"cycle_status": "active", "unassigned_at": None, "assigned_at": {"$gte": policy_since}},
            {"lead_id": 1, "_id": 0},
        ).to_list(length=1000)
        lead_ids = list({row.get("lead_id") for row in cycle_rows if row.get("lead_id") is not None})
        explain_rows: list[dict[str, Any]] = []
        explain_server_totals: dict[str, float] = {A1_NAME: 0.0, A2_NAME: 0.0}
        for variant, grouped in ((A1_NAME, False), (A2_NAME, True)):
            cycle_explain = await _explain_aggregate(session.db, "crm_assignment_cycles", _base_pipeline(policy_since, grouped=grouped))
            event_explain = await _explain_aggregate(session.db, "crm_events", _event_pipeline(lead_ids, since=policy_since, until=as_of))
            for path, detail in (("cycles_leads_management", cycle_explain), ("human_events_compact", event_explain)):
                explain_server_totals[variant] += _number(detail.get("execution_time_ms"))
                explain_rows.append({"variant": variant, "path": path, **detail})

        # Baseline/variant capacity equivalence and cycle-data equivalence.
        reference_capacity = reference_live.get("capacity_metrics") or {}
        variant_results = {A1_NAME: a1_warm, A2_NAME: a2_warm}
        equivalence_rows: list[dict[str, Any]] = []
        reference_cycle_audit = reference_live.get("cycle_audit") or {}
        for variant, result in variant_results.items():
            equivalence_rows.extend(_capacity_equivalence_rows(reference_capacity, variant, result.get("capacity_metrics") or {}))
            equivalence_rows.extend(_cycle_equivalence_rows(reference_cycle_audit, variant, result.get("cycle_audit") or {}))

        # The selector is run with each snapshot in memory, against the same
        # scan/context. No executor or shadow persistence path is called.
        from scripts.audit_crm_sla_read_path_phase2f1 import _combine_snapshot

        scan_result = await __import__("chatbot.crm_sla_reassignment_worker", fromlist=["scan_sla_reassignment_candidates"]).scan_sla_reassignment_candidates(
            session.db, batch_size=25, current_policy_since=policy_since,
        )
        context = await _batch_context(session.db, list(scan_result.get("cycles") or []))
        baseline_combined = _combine_snapshot(historical, reference_live)
        a1_combined = _combine_snapshot(historical, a1_warm)
        a2_combined = _combine_snapshot(historical, a2_warm)
        baseline_selection = await _run_worker_in_audit(db=session.db, snapshot=baseline_combined, as_of=as_of, scan_result=scan_result, context=context)
        selection_results = {"REFERENCE_CURRENT": baseline_selection}
        for variant, snapshot in ((A1_NAME, a1_combined), (A2_NAME, a2_combined)):
            selection_results[variant] = await _run_worker_in_audit(db=session.db, snapshot=snapshot, as_of=as_of, scan_result=scan_result, context=context)
        reference_signature = _selection_signature(baseline_selection)
        for variant in (A1_NAME, A2_NAME):
            candidate_signature = _selection_signature(selection_results[variant])
            for key in sorted(set(reference_signature) | set(candidate_signature)):
                equivalent = reference_signature.get(key) == candidate_signature.get(key)
                equivalence_rows.append({
                    "scope": "selector",
                    "variant": variant,
                    "entity_id": key,
                    "field": "base_score|winner|second|R2|JPC|J3|L1",
                    "reference_value": _json(reference_signature.get(key)),
                    "variant_value": _json(candidate_signature.get(key)),
                    "difference": 0 if equivalent else 1,
                    "status": "PASS" if equivalent else "FAIL",
                    "notes": "same scan/context/historical snapshot",
                })

        # Choose only the faster variant for a diagnostic worker injection. A
        # productive variant is selected only if the performance gate passes.
        benchmark_rows: list[dict[str, Any]] = []
        old_row = _benchmark_row("OLD_LIVE", old_samples, target_ms=500.0)
        a1_row = _benchmark_row(A1_NAME, a1_samples, target_ms=500.0, explain_server_ms=explain_server_totals[A1_NAME])
        a2_row = _benchmark_row(A2_NAME, a2_samples, target_ms=500.0, explain_server_ms=explain_server_totals[A2_NAME])
        benchmark_rows.extend([old_row, a1_row, a2_row])
        diagnostic_variant = A1_NAME if a1_row["p90_ms"] <= a2_row["p90_ms"] else A2_NAME
        diagnostic_snapshot = {A1_NAME: a1_combined, A2_NAME: a2_combined}[diagnostic_variant]
        worker_samples = await _repeat(
            session, repetitions,
            lambda inst: _run_worker_in_audit(db=session.db, snapshot=diagnostic_snapshot, as_of=as_of, instrumentation=inst),
        )
        worker_row = _benchmark_row("WORKER_WITH_" + diagnostic_variant, worker_samples, target_ms=1000.0)
        benchmark_rows.append(worker_row)

        pings, finds = await _network_samples(session, repetitions)
        network_rows = [
            {"measurement": "ping", "samples": len(pings), "p50_ms": round(_percentile(pings, .50), 6), "p90_ms": round(_percentile(pings, .90), 6), "p95_ms": round(_percentile(pings, .95), 6), "max_ms": round(max(pings), 6), "notes": "same reused client; warm samples"},
            {"measurement": "minimal_find_one", "samples": len(finds), "p50_ms": round(_percentile(finds, .50), 6), "p90_ms": round(_percentile(finds, .90), 6), "p95_ms": round(_percentile(finds, .95), 6), "max_ms": round(max(finds), 6), "notes": "crm_assignment_cycles _id projection only"},
        ]

        # Future-only guard with a cutover after the frozen as_of. The batch
        # remains observable but must not produce an executable decision.
        future = await _run_worker_in_audit(
            db=session.db, snapshot=diagnostic_snapshot, as_of=as_of,
            cutover_at=as_of.replace(microsecond=0) + timedelta(seconds=1),
        )
        current_backlog = int(scan_result.get("scanned", 0) or 0)
        future_would_execute = len(future.get("decisions") or [])

        write_commands = [name for name in session.listener.non_read_commands if name in WRITE_COMMANDS]
        all_equivalent = all(row["status"] == "PASS" for row in equivalence_rows)
        live_pass = all(row["status"] == "PASS" for row in (a1_row, a2_row))
        worker_pass = worker_row["status"] == "PASS"
        future_pass = future_would_execute == 0

        _write_csv(OUTPUT_FILES["comparison"], benchmark_rows, [
            "path", "samples", "p50_ms", "p90_ms", "p95_ms", "max_ms",
            "server_execution_ms_explain", "command_roundtrip_ms", "python_ms",
            "wire_commands", "wire_cursor_batches", "logical_reads",
            "documents_transferred", "bson_bytes_estimate", "pii_free",
            "target_ms", "status", "error_samples",
        ])
        _write_csv(OUTPUT_FILES["explain"], explain_rows, [
            "variant", "path", "execution_time_ms", "docs_examined", "keys_examined",
            "returned", "stages", "indexes", "lookups", "collscan", "status", "error",
        ])
        _write_csv(OUTPUT_FILES["equivalence"], equivalence_rows, [
            "scope", "variant", "entity_id", "field", "reference_value",
            "variant_value", "difference", "status", "notes",
        ])
        _write_csv(OUTPUT_FILES["network"], network_rows, [
            "measurement", "samples", "p50_ms", "p90_ms", "p95_ms", "max_ms", "notes",
        ])

        print({
            "as_of": as_of.isoformat(),
            "baseline": {
                "cycles": (reference_live.get("query_cost_breakdown") or {}).get("cycles", 0),
                "leads": (reference_live.get("query_cost_breakdown") or {}).get("leads", 0),
                "management_results": (reference_live.get("query_cost_breakdown") or {}).get("management_results", 0),
                "events": (reference_live.get("query_cost_breakdown") or {}).get("events", 0),
            },
            "benchmark": benchmark_rows,
            "equivalence": {"pass": all_equivalent, "rows": len(equivalence_rows)},
            "diagnostic_variant": diagnostic_variant,
            "worker": worker_row,
            "network": network_rows,
            "future_only": {"backlog_scanned": current_backlog, "would_execute": future_would_execute, "pass": future_pass},
            "write_commands_observed": write_commands,
            "files": {key: str(path) for key, path in OUTPUT_FILES.items()},
        })
        return 0 if all_equivalent and future_pass and live_pass and worker_pass else 2
    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fase 2F.2 read-only live capacity aggregation audit")
    parser.add_argument("--repetitions", type=int, default=20)
    args = parser.parse_args()
    if args.repetitions < 20:
        parser.error("Fase 2F.2 requiere al menos 20 repeticiones")
    raise SystemExit(asyncio.run(main(args.repetitions)))
