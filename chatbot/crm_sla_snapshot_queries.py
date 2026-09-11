"""Read-only query helpers and diagnostics for CRM SLA snapshots.

The helpers in this module deliberately know nothing about SLA policy.  They
only measure batched reads that callers already define.  Instrumentation is
opt-in so the production read path keeps the same query semantics and cost
when no audit object is supplied.
"""
from __future__ import annotations

import inspect
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping

from pymongo.monitoring import CommandListener


READ_OPERATIONS = {"find", "find_one", "aggregate", "count_documents", "distinct", "other"}
SENSITIVE_FIELD_PARTS = {"phone", "telefono", "email", "correo", "message", "messages", "body", "content"}


def _collection_name(collection: Any) -> str:
    return str(getattr(collection, "name", None) or getattr(collection, "_name", None) or "unknown")


def _projection_fields(projection: Mapping[str, Any] | None) -> list[str]:
    return sorted(str(key) for key, value in (projection or {}).items() if value and str(key) != "_id")


def _contains_sensitive_field(fields: list[str]) -> bool:
    return any(part.lower() in SENSITIVE_FIELD_PARTS for field in fields for part in field.split("."))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def cursor_to_list(cursor: Any, length: int | None = None) -> list[dict[str, Any]]:
    if length is not None and hasattr(cursor, "limit"):
        cursor = cursor.limit(length)
    method = getattr(cursor, "to_list", None)
    if method is not None:
        return list(await _maybe_await(method(length=length)) or [])
    return list(cursor or [])


@dataclass
class ReadCall:
    collection: str
    operation: str
    query_name: str
    documents: int
    wall_ms: float
    cursor_batches_observed: int
    projection_fields: tuple[str, ...] = ()
    projection_contains_pii: bool = False
    bson_bytes_estimate: int | None = None


@dataclass
class ReadInstrumentation:
    """Logical read telemetry, with no query values or document payloads."""

    calls: list[ReadCall] = field(default_factory=list)
    python_timings_ms: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    measure_bson_bytes: bool = False
    _read_started_at: float | None = field(default=None, init=False, repr=False)
    _read_finished_at: float | None = field(default=None, init=False, repr=False)

    def begin_read(self) -> None:
        now = time.perf_counter()
        if self._read_started_at is None:
            self._read_started_at = now
        self._read_finished_at = None

    def end_read(self) -> None:
        self._read_finished_at = time.perf_counter()

    def record_find(
        self,
        *,
        collection: str,
        query_name: str,
        documents: int,
        wall_ms: float,
        projection: Mapping[str, Any] | None,
        cursor_batches_observed: int,
        rows: list[Mapping[str, Any]] | None = None,
    ) -> None:
        bson_bytes: int | None = None
        if self.measure_bson_bytes:
            try:
                from bson import BSON
                bson_bytes = sum(len(BSON.encode(dict(row))) for row in (rows or []))
            except Exception:
                bson_bytes = None
        self.calls.append(ReadCall(
            collection=collection,
            operation="find",
            query_name=query_name,
            documents=int(documents),
            wall_ms=float(wall_ms),
            cursor_batches_observed=int(cursor_batches_observed),
            projection_fields=tuple(_projection_fields(projection)),
            projection_contains_pii=_contains_sensitive_field(_projection_fields(projection)),
            bson_bytes_estimate=bson_bytes,
        ))

    def mark_python(self, name: str, elapsed_ms: float) -> None:
        self.python_timings_ms[str(name)] = float(elapsed_ms)

    @property
    def mongo_round_trips_total(self) -> int:
        return len(self.calls)

    def summary(self) -> dict[str, Any]:
        by_collection: Counter[str] = Counter(call.collection for call in self.calls)
        by_operation: Counter[str] = Counter(call.operation for call in self.calls)
        by_collection_operation: Counter[str] = Counter(f"{call.collection}:{call.operation}" for call in self.calls)
        return {
            "mongo_round_trips_total": self.mongo_round_trips_total,
            "by_collection": dict(sorted(by_collection.items())),
            "by_operation": dict(sorted(by_operation.items())),
            "by_collection_operation": dict(sorted(by_collection_operation.items())),
            "documents_returned": sum(call.documents for call in self.calls),
            "bson_bytes_estimate": sum(call.bson_bytes_estimate or 0 for call in self.calls) if self.measure_bson_bytes else None,
            "cursor_batches_observed": sum(call.cursor_batches_observed for call in self.calls),
            "read_wall_ms": round(sum(call.wall_ms for call in self.calls), 6),
            "read_wall_span_ms": round((self._read_finished_at - self._read_started_at) * 1000.0, 6) if self._read_started_at is not None and self._read_finished_at is not None else 0.0,
            "python_timings_ms": {key: round(value, 6) for key, value in sorted(self.python_timings_ms.items())},
            "all_projections_pii_free": not any(call.projection_contains_pii for call in self.calls),
            "calls": [
                {
                    "collection": call.collection,
                    "operation": call.operation,
                    "query_name": call.query_name,
                    "documents": call.documents,
                    "wall_ms": round(call.wall_ms, 6),
                    "cursor_batches_observed": call.cursor_batches_observed,
                    "projection_fields": list(call.projection_fields),
                    "projection_contains_pii": call.projection_contains_pii,
                    "bson_bytes_estimate": call.bson_bytes_estimate,
                }
                for call in self.calls
            ],
        }


async def find_many(
    collection: Any,
    query: Mapping[str, Any],
    projection: Mapping[str, Any] | None = None,
    *,
    limit: int | None = None,
    sort: list[tuple[str, int]] | None = None,
    instrumentation: ReadInstrumentation | None = None,
    query_name: str = "find",
) -> list[dict[str, Any]]:
    started = time.perf_counter()
    if instrumentation is not None:
        instrumentation.begin_read()
    try:
        cursor = collection.find(dict(query), dict(projection) if projection is not None else None)
    except TypeError:
        cursor = collection.find(dict(query))
    if sort and hasattr(cursor, "sort"):
        cursor = cursor.sort(sort)
    if limit is not None and hasattr(cursor, "limit"):
        cursor = cursor.limit(limit)
    rows = await cursor_to_list(cursor, limit)
    if instrumentation is not None:
        instrumentation.end_read()
    if instrumentation is not None:
        instrumentation.record_find(
            collection=_collection_name(collection),
            query_name=query_name,
            documents=len(rows),
            wall_ms=(time.perf_counter() - started) * 1000.0,
            projection=projection,
            # This is the number of materialized cursor consumptions.  Exact
            # wire-level getMore counts come from the command listener used by
            # the real-client benchmark.
            cursor_batches_observed=1 if rows else 0,
            rows=rows,
        )
    return rows


def _pipeline_projection_fields(pipeline: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Return the final projection shape for PII telemetry only."""
    for stage in reversed(pipeline):
        if "$project" in stage and isinstance(stage["$project"], Mapping):
            return stage["$project"]
    return None


async def aggregate_many(
    collection: Any,
    pipeline: list[Mapping[str, Any]],
    *,
    instrumentation: ReadInstrumentation | None = None,
    query_name: str = "aggregate",
    batch_size: int | None = 1000,
) -> list[dict[str, Any]]:
    """Consume one aggregation cursor as one logical read.

    The helper intentionally records only shape/timing metadata.  It never
    stores pipeline predicates or returned values, which keeps the audit
    telemetry free of lead content and PII.
    """
    started = time.perf_counter()
    if instrumentation is not None:
        instrumentation.begin_read()
    try:
        options: dict[str, Any] = {}
        if batch_size is not None:
            options["batchSize"] = int(batch_size)
        try:
            cursor = collection.aggregate([dict(stage) for stage in pipeline], **options)
        except TypeError:
            cursor = collection.aggregate([dict(stage) for stage in pipeline])
        rows = await cursor_to_list(cursor)
    finally:
        if instrumentation is not None:
            instrumentation.end_read()
    if instrumentation is not None:
        instrumentation.record_find(
            collection=_collection_name(collection),
            query_name=query_name,
            documents=len(rows),
            wall_ms=(time.perf_counter() - started) * 1000.0,
            projection=_pipeline_projection_fields([dict(stage) for stage in pipeline]),
            cursor_batches_observed=1 if rows else 0,
            rows=rows,
        )
        instrumentation.calls[-1].operation = "aggregate"
    return rows


class MongoReadCommandListener(CommandListener):
    """Optional PyMongo command listener for server-vs-wall diagnostics."""

    READ_COMMANDS = {"find", "getMore", "aggregate", "count", "distinct", "listCollections", "listIndexes"}

    def __init__(self) -> None:
        self._started: dict[int, tuple[str, str, float]] = {}
        self._cursor_collections: dict[int, str] = {}
        self.commands: list[dict[str, Any]] = []
        self.non_read_commands: list[str] = []

    def started(self, event: Any) -> None:
        command_name = str(getattr(event, "command_name", ""))
        if command_name not in self.READ_COMMANDS:
            self.non_read_commands.append(command_name)
            return
        command = getattr(event, "command", {}) or {}
        if command_name == "getMore":
            cursor_id = int(command.get("getMore") or 0)
            collection = self._cursor_collections.get(cursor_id, "unknown")
        else:
            collection = str(command.get(command_name) or command.get("find") or command.get("aggregate") or "unknown")
        request_id = int(getattr(event, "request_id", id(event)))
        self._started[request_id] = (command_name, collection, time.perf_counter())

    def succeeded(self, event: Any) -> None:
        self._finish(event, failed=False)

    def failed(self, event: Any) -> None:
        self._finish(event, failed=True)

    def _finish(self, event: Any, *, failed: bool) -> None:
        request_id = int(getattr(event, "request_id", -1))
        started = self._started.pop(request_id, None)
        if started is None:
            return
        command_name, collection, started_at = started
        reply = getattr(event, "reply", {}) or {}
        cursor = reply.get("cursor") if isinstance(reply, Mapping) else None
        if isinstance(cursor, Mapping):
            cursor_id = int(cursor.get("id") or 0)
            if cursor_id:
                self._cursor_collections[cursor_id] = collection
            first_batch = cursor.get("firstBatch") or cursor.get("nextBatch") or []
            documents = len(first_batch) if isinstance(first_batch, list) else 0
        else:
            documents = int(reply.get("n", 0) or 0) if isinstance(reply, Mapping) else 0
        self.commands.append({
            "collection": collection,
            "operation": command_name,
            # PyMongo exposes duration_micros as elapsed command time from
            # the client listener. It is not MongoDB's internal execution
            # time; explain("executionStats") is required for that layer.
            "command_roundtrip_ms": float(getattr(event, "duration_micros", 0) or 0) / 1000.0,
            "wall_ms_listener": (time.perf_counter() - started_at) * 1000.0,
            "documents": documents,
            "failed": bool(failed),
        })

    def summary(self) -> dict[str, Any]:
        reads = [row for row in self.commands if row["operation"] in self.READ_COMMANDS]
        by_collection: Counter[str] = Counter(row["collection"] for row in reads)
        by_operation: Counter[str] = Counter(row["operation"] for row in reads)
        return {
            "mongo_commands_total": len(reads),
            "by_collection": dict(sorted(by_collection.items())),
            "by_operation": dict(sorted(by_operation.items())),
            "command_roundtrip_ms": round(sum(row["command_roundtrip_ms"] for row in reads), 6),
            "documents_returned": sum(row["documents"] for row in reads),
            "cursor_batches_wire_observed": sum(1 for row in reads if row["operation"] in {"find", "getMore"}),
            "failed_commands": sum(1 for row in reads if row["failed"]),
            "non_read_commands": list(self.non_read_commands),
            "commands": [{key: value for key, value in row.items() if key != "wall_ms_listener"} for row in reads],
        }


def query_pattern_summary(instrumentation: ReadInstrumentation) -> list[dict[str, Any]]:
    """Classify read patterns without retaining filters or document values."""
    grouped: defaultdict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "documents": 0, "wall_ms": 0.0})
    for call in instrumentation.calls:
        key = call.query_name
        grouped[key]["calls"] += 1
        grouped[key]["documents"] += call.documents
        grouped[key]["wall_ms"] += call.wall_ms
    output = []
    for pattern, values in sorted(grouped.items()):
        calls = int(values["calls"])
        lowered = pattern.lower()
        # The event path intentionally has two bounded set reads: one for
        # indexed timestamps and one compatibility read for legacy rows that
        # lack ``timestamp``.  That is not N+1.  N+1 evidence must name a
        # per-document access pattern, never merely a fixed two-query pair.
        n_plus_one = calls > 1 and any(token in lowered for token in ("per_lead", "per_cycle", "for_lead", "for_cycle", "one_document"))
        output.append({
            "pattern": pattern,
            "calls": calls,
            "documents": int(values["documents"]),
            "time_total_ms": round(values["wall_ms"], 6),
            "n_plus_one": "YES" if n_plus_one else "NO",
            "evidence": "per_document_access_detected" if n_plus_one else "bounded_set_read",
        })
    return output
