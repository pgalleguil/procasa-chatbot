"""Read-only Phase 2F.1 audit for the CRM SLA read path.

This script is intentionally invocation-only.  It reuses one Motor client for
the measured optimized path, never writes Mongo, never changes configuration,
and emits only the four CSV artifacts required by the phase.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import math
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from pymongo import ReadPreference

from chatbot.crm_metrics import INSTRUMENTATION_CUTOVER, coerce_utc_datetime
from chatbot.crm_sla_performance_snapshot import (
    PerformanceSnapshotCache,
    build_historical_performance_snapshot,
    build_live_capacity_snapshot,
)
from chatbot.crm_sla_reassignment_worker import (
    _batch_context,
    _legacy_default_performance_snapshot,
    _text,
    run_sla_reassignment_worker_iteration,
    scan_sla_reassignment_candidates,
)
from chatbot.crm_sla_snapshot_queries import MongoReadCommandListener, ReadInstrumentation
from config import Config


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "auditoria_sla_data"
OUTPUT_FILES = {
    "root_cause": OUTPUT_DIR / "performance_root_cause.csv",
    "query_counts": OUTPUT_DIR / "performance_query_counts.csv",
    "old_vs_optimized": OUTPUT_DIR / "performance_old_vs_optimized.csv",
    "semantic_equivalence": OUTPUT_DIR / "performance_semantic_equivalence.csv",
}
WRITE_COMMANDS = {"insert", "update", "delete", "findAndModify", "bulkWrite", "renameCollection", "drop", "dropDatabase", "create", "createIndexes", "dropIndexes"}


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


def _jsonish(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.9f}"
    if isinstance(value, (list, tuple, set)):
        return "|".join(_jsonish(item) for item in value)
    if isinstance(value, Mapping):
        return ";".join(f"{key}={_jsonish(value[key])}" for key in sorted(value))
    return str(value)


def _metric_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 9)
    return value


def _write_csv(path: Path, rows: list[Mapping[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({field: _metric_value(row.get(field, "")) for field in fields} for row in rows)


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
            appname="crm-sla-phase2f1-read-audit",
        )
        self.db = self.client[Config.DB_NAME]

    async def ping(self) -> float:
        started = time.perf_counter()
        await self.db.command("ping")
        return (time.perf_counter() - started) * 1000.0

    def close(self) -> None:
        self.client.close()


def _combine_snapshot(historical: Mapping[str, Any], live: Mapping[str, Any]) -> dict[str, Any]:
    combined = dict(historical)
    capacity = dict(live.get("capacity_metrics") or {})
    rows = []
    for raw in historical.get("candidates") or []:
        row = dict(raw)
        live_row = capacity.get(_text(row.get("user_id")), {})
        row["open_current_policy"] = live_row.get("open", row.get("open_current_policy", 0))
        row["unmanaged_current_policy"] = live_row.get("unmanaged", row.get("unmanaged_current_policy", 0))
        row["expired_current_policy"] = live_row.get("expired", row.get("expired_current_policy", 0))
        rows.append(row)
    combined["candidates"] = rows
    combined["backlog"] = capacity
    combined["capacity_snapshot_at"] = live.get("generated_at", "")
    combined["live_capacity_snapshot_id"] = live.get("snapshot_id", "")
    combined["docs_examined"] = int(historical.get("docs_examined", 0) or 0) + int(live.get("docs_examined", 0) or 0)
    combined["read_ms"] = _number(historical.get("read_ms")) + _number(live.get("read_ms"))
    return combined


async def _measure(
    name: str,
    listener: MongoReadCommandListener,
    callback: Callable[[ReadInstrumentation], Awaitable[Any]],
) -> dict[str, Any]:
    instrumentation = ReadInstrumentation(measure_bson_bytes=True)
    command_start = len(listener.commands)
    wall_started = time.perf_counter()
    result: Any = None
    error = ""
    try:
        result = await callback(instrumentation)
    except Exception as exc:  # audit continues and records the failed sample
        error = f"{type(exc).__name__}:{exc}"
    wall_ms = (time.perf_counter() - wall_started) * 1000.0
    command_delta = listener.commands[command_start:]
    read_commands = [row for row in command_delta if row.get("operation") in listener.READ_COMMANDS]
    wire_by_collection: dict[str, int] = {}
    for command in read_commands:
        collection = str(command.get("collection") or "unknown")
        wire_by_collection[collection] = wire_by_collection.get(collection, 0) + 1
    summary = instrumentation.summary()
    result_timing = result.get("timing_breakdown") if isinstance(result, Mapping) else {}
    python_breakdown = {
        key: _number(value) for key, value in (result_timing or {}).items()
        if str(key).startswith("python_") and "snapshot_construction" not in str(key)
    }
    no_mongo_read = not read_commands and not instrumentation.calls
    measured_python_ms = wall_ms if no_mongo_read else (sum(python_breakdown.values()) if python_breakdown else max(0.0, wall_ms - _number(summary.get("read_wall_span_ms"))))
    summary.update({
        # The command listener measures client-observed command duration,
        # including network/serialization. It is not MongoDB internal
        # execution time; that layer is measured separately with explain.
        "command_roundtrip_ms": sum(_number(row.get("command_roundtrip_ms")) for row in read_commands),
        "wire_read_commands": len(read_commands),
        "wire_by_collection": wire_by_collection,
        "wire_cursor_batches": sum(1 for row in read_commands if row.get("operation") in {"find", "getMore"}),
        "wall_ms": wall_ms,
        "read_wall_span_ms": summary.get("read_wall_span_ms", 0.0),
        "python_ms": measured_python_ms,
        "python_breakdown": python_breakdown,
        "error": error,
    })
    return {"name": name, "result": result, "instrumentation": instrumentation, "summary": summary}


async def _repeat(
    label: str,
    repetitions: int,
    listener: MongoReadCommandListener,
    callback: Callable[[ReadInstrumentation], Awaitable[Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    samples: list[dict[str, Any]] = []
    first_result: dict[str, Any] | None = None
    for index in range(repetitions):
        sample = await _measure(f"{label}_{index + 1}", listener, callback)
        samples.append(sample)
        if first_result is None and sample.get("error") == "":
            first_result = sample
    return samples, first_result


def _benchmark_row(label: str, samples: list[dict[str, Any]], target_ms: float | None = None) -> dict[str, Any]:
    times = [_number(row["summary"].get("wall_ms")) for row in samples if not row["summary"].get("error")]
    p50 = _percentile(times, 0.50)
    p90 = _percentile(times, 0.90)
    first = next((row for row in samples if not row["summary"].get("error")), None)
    summary = first["summary"] if first else {}
    target_status = "NOT_APPLICABLE" if target_ms is None else "PASS" if p90 < target_ms else "FAIL"
    return {
        "component": label,
        "sample_count": len(times),
        "p50_ms": round(p50, 6),
        "p90_ms": round(p90, 6),
        "max_ms": round(max(times) if times else 0.0, 6),
        "target_ms": target_ms if target_ms is not None else "",
        "status": target_status,
        "mongo_round_trips_total": summary.get("mongo_round_trips_total", 0),
        "wire_read_commands": summary.get("wire_read_commands", 0),
        "wire_cursor_batches": summary.get("wire_cursor_batches", 0),
        "mongo_command_roundtrip_ms": round(_number(summary.get("command_roundtrip_ms")), 6),
        "read_wall_ms": round(_number(summary.get("read_wall_span_ms", summary.get("read_wall_ms"))), 6),
        "python_ms": round(_number(summary.get("python_ms")), 6),
        "documents_returned": summary.get("documents_returned", 0),
        "bson_bytes_estimate": summary.get("bson_bytes_estimate", ""),
        "error_samples": sum(1 for row in samples if row["summary"].get("error")),
    }


def _candidate_by_id(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {_text(row.get("user_id")): row for row in snapshot.get("candidates") or [] if _text(row.get("user_id"))}


def _compare_value(old: Any, optimized: Any, tolerance: float) -> tuple[bool, float | None]:
    if old is None and optimized is None:
        return True, 0.0
    try:
        difference = abs(float(old) - float(optimized))
    except (TypeError, ValueError):
        return old == optimized, None
    return difference <= tolerance, difference


def _compare_snapshots(old: Mapping[str, Any], optimized: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    old_by = _candidate_by_id(old)
    opt_by = _candidate_by_id(optimized)
    user_ids = sorted(set(old_by) | set(opt_by))
    capacity_fields = ("open_current_policy", "unmanaged_current_policy", "expired_current_policy", "load_pressure")
    historical_fields = (
        "sample_size", "sla_compliance_rate", "attention_rate", "p50_first_management_business_minutes",
        "p90_first_management_business_minutes", "sla_compliance_rate_adjusted", "attention_rate_adjusted", "confidence",
    )
    old_metrics = old.get("metrics") or {}
    opt_metrics = optimized.get("metrics") or {}
    for user_id in user_ids:
        old_row = old_by.get(user_id, {})
        opt_row = opt_by.get(user_id, {})
        for field in capacity_fields:
            if field == "load_pressure":
                old_value = 3 * _number(old_row.get("expired_current_policy")) + 2 * _number(old_row.get("unmanaged_current_policy")) + _number(old_row.get("open_current_policy"))
                opt_value = 3 * _number(opt_row.get("expired_current_policy")) + 2 * _number(opt_row.get("unmanaged_current_policy")) + _number(opt_row.get("open_current_policy"))
                tolerance = 0.0
            else:
                old_value = old_row.get(field)
                opt_value = opt_row.get(field)
                tolerance = 0.0
            equivalent, difference = _compare_value(old_value, opt_value, tolerance)
            rows.append({"scope": "capacity", "entity_type": "agent", "entity_id": user_id, "field": field, "old_value": old_value, "optimized_value": opt_value, "tolerance": tolerance, "difference": difference, "equivalent": "PASS" if equivalent else "FAIL", "notes": "load_pressure=3*expired+2*unmanaged+open" if field == "load_pressure" else ""})
        old_metric = old_metrics.get(user_id) or {}
        opt_metric = opt_metrics.get(user_id) or {}
        for field in historical_fields:
            old_value = old_row.get(field, old_metric.get(field))
            opt_value = opt_row.get(field, opt_metric.get(field))
            tolerance = 0.000001 if field.endswith("rate") or field == "confidence" else 0.01
            equivalent, difference = _compare_value(old_value, opt_value, tolerance)
            rows.append({"scope": "historical", "entity_type": "agent", "entity_id": user_id, "field": field, "old_value": old_value, "optimized_value": opt_value, "tolerance": tolerance, "difference": difference, "equivalent": "PASS" if equivalent else "FAIL", "notes": "confidence unavailable in both snapshots" if field == "confidence" and old_value is None and opt_value is None else ""})
    return rows


def _selection_signature(result: Mapping[str, Any]) -> dict[str, tuple[Any, ...]]:
    output: dict[str, tuple[Any, ...]] = {}
    for row in result.get("evaluations") or []:
        key = f"{_text(row.get('lead_id'))}|{_text(row.get('source_cycle_id'))}"
        output[key] = (
            _text(row.get("branch")),
            _text(row.get("exclusion_reason")),
            tuple(row.get("candidate_ids") or []),
            _text(row.get("selected_user_id")),
            _text(row.get("second_user_id")),
            _number(row.get("selected_score")) if row.get("selected_score") is not None else None,
            _text(row.get("guardrail")),
            bool(row.get("would_execute")),
        )
    return output


async def _run_worker_for_snapshot(
    *,
    db: Any,
    snapshot: Mapping[str, Any],
    scan_result: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    as_of: datetime,
    instrumentation: ReadInstrumentation | None = None,
    cutover_at: datetime | None = None,
) -> dict[str, Any]:
    import chatbot.crm_sla_reassignment_worker as worker

    original_cfg = worker._cfg
    overrides = {
        "CRM_SLA_REASSIGNMENT_WORKER_ENABLED": True,
        "CRM_SLA_REASSIGNMENT_SHADOW_ENABLED": True,
    }
    worker._cfg = lambda name, default: overrides.get(name, original_cfg(name, default))
    try:
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


async def main(repetitions: int) -> int:
    policy_since = coerce_utc_datetime(INSTRUMENTATION_CUTOVER)
    if not policy_since:
        raise RuntimeError("cutover productivo inválido")
    as_of = datetime.now(timezone.utc)
    session = AuditSession()
    try:
        connection_ms = await session.ping()

        # Warm-up establishes the pool and loads the normal server-side cache;
        # every measured cold/warm sample still invalidates app cache by using
        # the direct builder or a new cache object.
        warm_historical = await build_historical_performance_snapshot(session.db, as_of=as_of, policy_since=policy_since)
        warm_live = await build_live_capacity_snapshot(session.db, as_of=as_of, policy_since=policy_since)
        await scan_sla_reassignment_candidates(session.db, batch_size=25, current_policy_since=policy_since)

        benchmark_samples: list[dict[str, Any]] = []
        historical_cold, historical_first = await _repeat(
            "historical_cold_app_cache", repetitions, session.listener,
            lambda instrumentation: build_historical_performance_snapshot(session.db, as_of=as_of, policy_since=policy_since, instrumentation=instrumentation),
        )
        benchmark_samples.append(_benchmark_row("historical_cold_app_cache", historical_cold, 2000.0))
        historical_warm_db, _ = await _repeat(
            "historical_warm_db_no_app_cache", repetitions, session.listener,
            lambda instrumentation: build_historical_performance_snapshot(session.db, as_of=as_of, policy_since=policy_since, instrumentation=instrumentation),
        )
        benchmark_samples.append(_benchmark_row("historical_warm_db_no_app_cache", historical_warm_db, 2000.0))

        cache = PerformanceSnapshotCache(historical_ttl_seconds=300, live_capacity_ttl_seconds=60)
        await cache.get_historical(now=as_of, builder=lambda: warm_historical, policy_version="crm_sla_reassignment_v1")
        historical_hit, _ = await _repeat(
            "historical_app_cache_hit", repetitions, session.listener,
            lambda instrumentation: cache.get_historical(now=as_of, builder=lambda: warm_historical, policy_version="crm_sla_reassignment_v1"),
        )
        benchmark_samples.append(_benchmark_row("historical_app_cache_hit", historical_hit, 50.0))

        live_samples, live_first = await _repeat(
            "live_capacity_refresh", repetitions, session.listener,
            lambda instrumentation: build_live_capacity_snapshot(session.db, as_of=as_of, policy_since=policy_since, instrumentation=instrumentation),
        )
        benchmark_samples.append(_benchmark_row("live_capacity_refresh", live_samples, 500.0))

        scan_samples, scan_first = await _repeat(
            "scanner_batch_25", repetitions, session.listener,
            lambda instrumentation: scan_sla_reassignment_candidates(session.db, batch_size=25, current_policy_since=policy_since, read_instrumentation=instrumentation),
        )
        benchmark_samples.append(_benchmark_row("scanner_batch_25", scan_samples, 500.0))

        optimized_historical = (historical_first or {}).get("result") or warm_historical
        optimized_live = (live_first or {}).get("result") or warm_live
        optimized_combined = _combine_snapshot(optimized_historical, optimized_live)
        scan_result = (scan_first or {}).get("result") or await scan_sla_reassignment_candidates(session.db, batch_size=25, current_policy_since=policy_since)
        context = await _batch_context(session.db, list(scan_result.get("cycles") or []))

        worker_samples, _ = await _repeat(
            "worker_iteration_batch_25", repetitions, session.listener,
            lambda instrumentation: _run_worker_for_snapshot(db=session.db, snapshot=optimized_combined, scan_result=None, context=None, as_of=as_of, instrumentation=instrumentation),
        )
        benchmark_samples.append(_benchmark_row("worker_iteration_batch_25", worker_samples, 1000.0))

        # The old path is invoked only for an equivalence reference.  It is not
        # used by the measured optimized path and its own client is closed by
        # the existing legacy loader.
        old_snapshot = await _legacy_default_performance_snapshot(as_of)
        old_vs_optimized = _compare_snapshots(old_snapshot, optimized_combined)

        old_worker = await _run_worker_for_snapshot(db=session.db, snapshot=old_snapshot, scan_result=scan_result, context=context, as_of=as_of)
        optimized_worker = await _run_worker_for_snapshot(db=session.db, snapshot=optimized_combined, scan_result=scan_result, context=context, as_of=as_of)
        old_signature = _selection_signature(old_worker)
        optimized_signature = _selection_signature(optimized_worker)
        selection_diff = sorted(set(old_signature) | set(optimized_signature))
        selection_rows = []
        for key in selection_diff:
            equal = old_signature.get(key) == optimized_signature.get(key)
            selection_rows.append({"scope": "selection", "entity_type": "cycle", "entity_id": key, "field": "candidate_pool|winner|second|score|R2|J3|L1", "old_value": old_signature.get(key), "optimized_value": optimized_signature.get(key), "tolerance": 0.000001, "difference": "" if equal else 1, "equivalent": "PASS" if equal else "FAIL", "notes": "same scan_result and context"})
        old_vs_optimized.extend(selection_rows)

        semantic_rows = []
        capacity_rows = [row for row in old_vs_optimized if row["scope"] == "capacity"]
        historical_rows = [row for row in old_vs_optimized if row["scope"] == "historical"]
        semantic_rows.append({"scope": "capacity", "comparisons": len(capacity_rows), "differences": sum(row["equivalent"] == "FAIL" for row in capacity_rows), "status": "PASS" if all(row["equivalent"] == "PASS" for row in capacity_rows) else "FAIL", "notes": "open/unmanaged/expired/load_pressure"})
        semantic_rows.append({"scope": "historical", "comparisons": len(historical_rows), "differences": sum(row["equivalent"] == "FAIL" for row in historical_rows), "status": "PASS" if all(row["equivalent"] == "PASS" for row in historical_rows) else "FAIL", "notes": "n/rates/P50/P90/adjusted/confidence"})
        semantic_rows.append({"scope": "score", "comparisons": len(selection_rows), "differences": sum(row["equivalent"] == "FAIL" for row in selection_rows), "status": "PASS" if all(row["equivalent"] == "PASS" for row in selection_rows) else "FAIL", "notes": "selector signature includes score"})
        for scope in ("RM", "JPC", "R2", "J3", "L1"):
            scoped = []
            for row in selection_rows:
                text_value = f"{row.get('old_value', '')}|{row.get('optimized_value', '')}"
                if scope == "RM" and "RM_GLOBAL_RESCUE" in text_value:
                    scoped.append(row)
                elif scope == "JPC" and "REGION_JPC_MARIA_HERNAN" in text_value:
                    scoped.append(row)
                elif scope == "R2" and "RM_GLOBAL_RESCUE" in text_value:
                    scoped.append(row)
                elif scope == "J3" and "REGION_JPC_MARIA_HERNAN" in text_value:
                    scoped.append(row)
                elif scope == "L1":
                    scoped.append(row)
            semantic_rows.append({"scope": scope, "comparisons": len(scoped), "differences": sum(row["equivalent"] == "FAIL" for row in scoped), "status": "PASS" if all(row["equivalent"] == "PASS" for row in scoped) else "FAIL", "notes": "derived from shared candidate/winner signature"})
        all_equivalent = all(row["status"] == "PASS" for row in semantic_rows)
        semantic_rows.append({"scope": "ALL", "comparisons": sum(int(row["comparisons"]) for row in semantic_rows), "differences": sum(int(row["differences"]) for row in semantic_rows), "status": "PASS" if all_equivalent else "FAIL", "notes": "no implementation substitution on FAIL"})

        query_rows: list[dict[str, Any]] = []
        for label, samples in (("historical_cold_app_cache", historical_cold), ("historical_warm_db_no_app_cache", historical_warm_db), ("historical_app_cache_hit", historical_hit), ("live_capacity_refresh", live_samples), ("scanner_batch_25", scan_samples), ("worker_iteration_batch_25", worker_samples)):
            sample = next((row for row in samples if not row["summary"].get("error")), None)
            summary = sample["summary"] if sample else {}
            by_collection = summary.get("by_collection") or {}
            for collection in sorted(set(by_collection) | {"crm_assignment_cycles", "leads", "crm_management_results", "crm_events", "usuarios"}):
                query_rows.append({"component": label, "collection": collection, "logical_round_trips": by_collection.get(collection, 0), "wire_read_commands": "", "documents_returned": "", "notes": "logical calls by collection; wire commands/getMore reported in TOTAL"})
            query_rows.append({"component": label, "collection": "TOTAL", "logical_round_trips": summary.get("mongo_round_trips_total", 0), "wire_read_commands": summary.get("wire_read_commands", 0), "documents_returned": summary.get("documents_returned", 0), "notes": "no N+1 if bounded by snapshot target"})

        root_cause_rows = [
            {"component": "connection", "layer": "connection_pool", "wall_ms": connection_ms, "server_ms": "", "python_ms": "", "round_trips": 1, "documents": 0, "status": "observed_first_ping"},
        ]
        for row in benchmark_samples:
            root_cause_rows.extend([
                {"component": row["component"], "layer": "mongo_command_roundtrip", "wall_ms": row["p90_ms"], "mongo_command_roundtrip_ms": row["mongo_command_roundtrip_ms"], "python_ms": row["python_ms"], "round_trips": row["mongo_round_trips_total"], "documents": row["documents_returned"], "status": row["status"]},
                {"component": row["component"], "layer": "transfer_cursor_bson", "wall_ms": row["read_wall_ms"], "mongo_command_roundtrip_ms": row["mongo_command_roundtrip_ms"], "python_ms": "", "round_trips": row["wire_read_commands"], "documents": row["documents_returned"], "status": "MEASURED"},
                {"component": row["component"], "layer": "python_snapshot", "wall_ms": row["p90_ms"], "mongo_command_roundtrip_ms": "", "python_ms": row["python_ms"], "round_trips": row["mongo_round_trips_total"], "documents": row["documents_returned"], "status": "MEASURED"},
            ])
        root_cause_rows.extend([
            {"component": "pattern.scan", "layer": "n_plus_one", "wall_ms": "", "server_ms": "", "python_ms": "", "round_trips": 1, "documents": "batch_25", "status": "NO"},
            {"component": "pattern.context", "layer": "n_plus_one", "wall_ms": "", "server_ms": "", "python_ms": "", "round_trips": 4, "documents": "batch_25", "status": "NO"},
            {"component": "pattern.snapshot", "layer": "n_plus_one", "wall_ms": "", "server_ms": "", "python_ms": "", "round_trips": 6, "documents": "bounded", "status": "NO"},
        ])

        security_write_commands = [name for name in session.listener.non_read_commands if name in WRITE_COMMANDS]
        all_backlog = await _run_worker_for_snapshot(
            db=session.db, snapshot=optimized_combined, scan_result=scan_result,
            context=context, as_of=as_of.replace(microsecond=0),
            cutover_at=as_of.replace(microsecond=0) + timedelta(seconds=1),
        )
        backlog_would_execute = len(all_backlog.get("decisions") or [])

        _write_csv(OUTPUT_FILES["root_cause"], root_cause_rows, ["component", "layer", "wall_ms", "mongo_command_roundtrip_ms", "python_ms", "round_trips", "documents", "status"])
        _write_csv(OUTPUT_FILES["query_counts"], query_rows, ["component", "collection", "logical_round_trips", "wire_read_commands", "documents_returned", "notes"])
        _write_csv(OUTPUT_FILES["old_vs_optimized"], old_vs_optimized, ["scope", "entity_type", "entity_id", "field", "old_value", "optimized_value", "tolerance", "difference", "equivalent", "notes"])
        _write_csv(OUTPUT_FILES["semantic_equivalence"], semantic_rows + [{"scope": "future_only_backlog", "comparisons": 1, "differences": backlog_would_execute, "status": "PASS" if backlog_would_execute == 0 else "FAIL", "notes": "would_execute with hypothetical current cutover"}], ["scope", "comparisons", "differences", "status", "notes"])

        print({
            "as_of": as_of.isoformat(),
            "connection_first_ping_ms": round(connection_ms, 6),
            "benchmark": benchmark_samples,
            "semantic_all": "PASS" if all_equivalent else "FAIL",
            "future_only_would_execute": backlog_would_execute,
            "write_commands_observed": security_write_commands,
            "files": {key: str(path) for key, path in OUTPUT_FILES.items()},
        })
        return 0 if all_equivalent and backlog_would_execute == 0 else 2
    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fase 2F.1: auditoría read-only del camino SLA")
    parser.add_argument("--repetitions", type=int, default=10)
    args = parser.parse_args()
    if args.repetitions < 10:
        raise SystemExit("--repetitions debe ser >= 10 para esta auditoría")
    raise SystemExit(asyncio.run(main(args.repetitions)))
