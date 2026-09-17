"""Single, resumable TOCTOC pipeline orchestrator.

The production entry point is deliberately dependency-injected.  A normal
local run can provide the real scraper, Mongo persistence and distributor once
those capabilities are enabled; tests and dry-runs use the same path with
fakes.  The default options are fail-closed and perform no network, Mongo or
assignment writes.
"""
from __future__ import annotations

import copy
import hashlib
import random
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Protocol

from ai_cost_guard import AICostGuard, budget_from_config
from broker_identity import detect_hard_broker_signal
from captacion_assignment_eligibility import can_assign_property
from classification_cache import ClassificationCache
from classification_service import classify_capture
from extractor_health import evaluate_extractor_health, measure_extractor_health


PIPELINE_VERSION = "toctoc-pipeline-v1"
PIPELINE_RUNS_COLLECTION = "pipeline_runs"
PIPELINE_ITEMS_COLLECTION = "pipeline_run_items"
TERMINAL_RUN_STATUSES = frozenset({
    "SUCCESS",
    "COMPLETED_WITH_ANOMALIES",
    "STOPPED_REQUIRES_REVIEW",
    "ABORTED_FAIL_CLOSED",
    "FAILED",
    "FINISHED",
})

ITEM_STATUSES = frozenset({
    "DISCOVERED",
    "EXTRACTED",
    "NORMALIZED",
    "CLASSIFIED_DETERMINISTIC",
    "CACHE_HIT",
    "PENDING_AI",
    "AI_CLASSIFIED",
    "PERSISTED",
    "ASSIGNABLE",
    "ASSIGNED",
    "BLOCKED",
    "FAILED",
})

CLASSIFICATION_STATUSES = frozenset({
    "CLASSIFIED_DETERMINISTIC",
    "CACHE_HIT",
    "PENDING_AI",
    "AI_CLASSIFIED",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(value: Any) -> str:
    return str(value or "").strip()


def _canonical_final(classification: dict[str, Any]) -> str:
    return str(
        classification.get("final")
        or classification.get("canonical_final")
        or ""
    ).strip().upper()


def _assigned_executive(record: dict[str, Any]) -> str:
    return _key(
        record.get("assigned_executive")
        or record.get("assigned_to")
        or (record.get("gestion") or {}).get("ejecutivo_asignado")
        or (record.get("assignment") or {}).get("executive_id")
    )


def _item_key(record: dict[str, Any]) -> str:
    for field in ("listing_id", "publication_id", "codigo"):
        value = _key(record.get(field))
        if value and value.casefold() not in {"0", "none", "null", "nan"}:
            return value
    url = _key(record.get("url") or record.get("source_url"))
    if url:
        return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    return ""


def _normalise_record(record: dict[str, Any]) -> dict[str, Any]:
    """Preserve raw fields while adding stable pipeline identity fields."""
    item = copy.deepcopy(record or {})
    listing_id = _item_key(item)
    item["listing_id"] = listing_id
    item["portal"] = str(
        item.get("portal") or item.get("source_portal") or item.get("origen") or "TOCTOC"
    ).strip().upper()
    item["source_portal"] = item["portal"].lower()
    item["title"] = item.get("title") or item.get("titulo") or ""
    item["description"] = item.get("description") or item.get("descripcion") or ""
    item["url"] = item.get("url") or item.get("source_url") or ""
    item["pipeline_version"] = PIPELINE_VERSION
    item["pipeline_item_key"] = listing_id
    return item


def _run_is_closed(run: dict[str, Any] | None) -> bool:
    if not run:
        return False
    if run.get("finished_at"):
        return True
    status = _key(run.get("run_status") or run.get("status")).upper()
    return status in TERMINAL_RUN_STATUSES


def _cap_records_for_run(
    records: list[dict[str, Any]],
    *,
    existing_item_keys: set[str],
    max_new_items: int | None,
) -> tuple[list[dict[str, Any]], int]:
    """Bound unique ledger additions before the first ledger write.

    Duplicate inputs are retained only after their first key has been admitted;
    they cannot consume the new-item budget.  A missing/sentinel listing id is
    keyed by a URL hash, so ``listing_id=0`` can never collapse unrelated
    pages into one ledger item.
    """
    if max_new_items is None:
        return list(records), 0
    limit = max(0, int(max_new_items))
    selected: list[dict[str, Any]] = []
    admitted: set[str] = set(existing_item_keys)
    new_keys = 0
    skipped = 0
    for raw in records:
        key = _item_key(raw)
        if not key:
            skipped += 1
            continue
        if key in admitted:
            selected.append(raw)
            continue
        if new_keys >= limit:
            skipped += 1
            continue
        admitted.add(key)
        new_keys += 1
        selected.append(raw)
    return selected, skipped


def _default_config(options: "PipelineOptions") -> Any:
    return SimpleNamespace(
        deepseek_enabled=bool(options.allow_real_ai or options.test_mode),
        deepseek_api_key="pipeline-test-key" if options.test_mode else "",
        deepseek_max_tokens=500,
        max_ai_calls_per_run=options.max_ai_calls_per_run,
        max_input_tokens_per_run=options.max_input_tokens_per_run,
        max_output_tokens_per_run=options.max_output_tokens_per_run,
        max_estimated_cost_per_run=options.max_estimated_cost_per_run,
        estimated_input_cost_per_million=0.14,
        estimated_output_cost_per_million=0.28,
    )


@dataclass(slots=True)
class PipelineOptions:
    """Runtime switches. Defaults are safe dry-run settings."""

    run_id: str | None = None
    resume_existing_run: bool = False
    max_new_items: int | None = None
    test_mode: bool = False
    dry_run: bool = True
    allow_real_scraping: bool = False
    allow_mongo_writes: bool = False
    allow_assignments: bool = False
    allow_distribution: bool = False
    allow_real_ai: bool = False
    qa_sample_size: int = 5
    health_min_sample: int = 20
    health_drop_ratio: float = 0.35
    max_ai_calls_per_run: int = 50
    max_input_tokens_per_run: int = 100_000
    max_output_tokens_per_run: int = 20_000
    max_estimated_cost_per_run: float = 5.0


class PipelineLedger(Protocol):
    def start_run(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    def list_item_keys(self, run_id: str) -> set[str]: ...
    def get_item(self, run_id: str, item_key: str) -> dict[str, Any] | None: ...
    def set_item(self, run_id: str, item_key: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def finish_run(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def acquire(self, run_id: str) -> bool: ...
    def release(self, run_id: str) -> None: ...


class InMemoryPipelineLedger:
    """Deterministic ledger used by tests and dry-runs."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self._locks: set[str] = set()
        self._lock = threading.RLock()

    def start_run(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.runs.get(run_id)
            if current:
                current.update({"resumed_at": _now(), "resume_count": int(current.get("resume_count", 0)) + 1})
                return copy.deepcopy(current)
            row = {"run_id": run_id, "created_at": _now(), "resume_count": 0, **payload}
            self.runs[run_id] = row
            return copy.deepcopy(row)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.runs.get(run_id)
            return copy.deepcopy(row) if row else None

    def list_item_keys(self, run_id: str) -> set[str]:
        with self._lock:
            return {item_key for (stored_run_id, item_key) in self.items if stored_run_id == run_id}

    def get_item(self, run_id: str, item_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.items.get((run_id, item_key))
            return copy.deepcopy(row) if row else None

    def set_item(self, run_id: str, item_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.items.get((run_id, item_key), {"run_id": run_id, "item_key": item_key})
            current.update(copy.deepcopy(payload), updated_at=_now())
            self.items[(run_id, item_key)] = current
            return copy.deepcopy(current)

    def finish_run(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.runs.setdefault(run_id, {"run_id": run_id})
            current.update(copy.deepcopy(payload), status="FINISHED", finished_at=_now())
            return copy.deepcopy(current)

    def acquire(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._locks:
                return False
            self._locks.add(run_id)
            return True

    def release(self, run_id: str) -> None:
        with self._lock:
            self._locks.discard(run_id)


class MongoPipelineLedger:
    """Mongo-backed ledger; construction is side-effect free.

    Index creation is intentionally explicit and is not invoked by the
    orchestrator. Production provisioning can create the two unique indexes
    during a controlled deployment.
    """

    def __init__(self, db: Any) -> None:
        self.db = db
        self.runs = db[PIPELINE_RUNS_COLLECTION]
        self.items = db[PIPELINE_ITEMS_COLLECTION]
        self._locks: set[str] = set()
        self._lock = threading.RLock()
        self._lock_token = uuid.uuid4().hex

    def ensure_indexes(self) -> None:
        """Provision only the two idempotency indexes when explicitly called."""
        self.runs.create_index("run_id", unique=True)
        self.items.create_index([("run_id", 1), ("item_key", 1)], unique=True)

    def start_run(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.runs.update_one(
            {"run_id": run_id},
            {"$setOnInsert": {"run_id": run_id, "created_at": _now(), "resume_count": 0}, "$set": payload},
            upsert=True,
        )
        row = self.runs.find_one({"run_id": run_id}) or {"run_id": run_id, **payload}
        return dict(row)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.runs.find_one({"run_id": run_id})
        return dict(row) if row else None

    def list_item_keys(self, run_id: str) -> set[str]:
        return {
            str(row.get("item_key"))
            for row in self.items.find({"run_id": run_id}, {"item_key": 1})
            if row.get("item_key") not in (None, "")
        }

    def get_item(self, run_id: str, item_key: str) -> dict[str, Any] | None:
        row = self.items.find_one({"run_id": run_id, "item_key": item_key})
        return dict(row) if row else None

    def set_item(self, run_id: str, item_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.items.update_one(
            {"run_id": run_id, "item_key": item_key},
            {"$set": {**payload, "run_id": run_id, "item_key": item_key, "updated_at": _now()}},
            upsert=True,
        )
        return dict(self.items.find_one({"run_id": run_id, "item_key": item_key}) or payload)

    def finish_run(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.runs.update_one(
            {"run_id": run_id},
            {"$set": {**payload, "status": "FINISHED", "finished_at": _now()}},
            upsert=True,
        )
        return dict(self.runs.find_one({"run_id": run_id}) or payload)

    def acquire(self, run_id: str) -> bool:
        now = datetime.now(timezone.utc)
        lock_until = now + timedelta(minutes=30)
        result = self.runs.update_one(
            {
                "run_id": run_id,
                "$or": [
                    {"lock_until": {"$exists": False}},
                    {"lock_until": {"$lt": now}},
                    {"lock_token": self._lock_token},
                ],
            },
            {"$set": {"lock_token": self._lock_token, "lock_until": lock_until, "status": "RUNNING"}},
            upsert=True,
        )
        return bool(result.matched_count or result.upserted_id)

    def release(self, run_id: str) -> None:
        self.runs.update_one(
            {"run_id": run_id, "lock_token": self._lock_token},
            {"$unset": {"lock_token": "", "lock_until": ""}},
        )


def _qa_sample(items: list[dict[str, Any]], size: int, seed: int) -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(seed)
    groups: dict[str, list[dict[str, Any]]] = {
        "BROKER": [], "OWNER_ASSIGNABLE": [], "UNCERTAIN": [],
        "CACHE_HIT": [], "AI_CANDIDATE": [],
    }
    for item in items:
        classification = item.get("classification") or {}
        final = _canonical_final(classification)
        if final in {"BROKER_CONFIRMED", "BROKER_PROBABLE"}:
            groups["BROKER"].append(item)
        elif final in {"OWNER_CONFIRMED", "OWNER_PROBABLE"} and item.get("assignment_ready"):
            groups["OWNER_ASSIGNABLE"].append(item)
        elif final == "UNCERTAIN" or classification.get("status") == "UNCERTAIN_PENDING_AI":
            groups["UNCERTAIN"].append(item)
        if item.get("cache_hit"):
            groups["CACHE_HIT"].append(item)
        if item.get("ai_candidate"):
            groups["AI_CANDIDATE"].append(item)
    sampled: dict[str, list[dict[str, Any]]] = {}
    for name, values in groups.items():
        copy_values = list(values)
        rng.shuffle(copy_values)
        sampled[name] = [
            {
                "listing_id": value.get("listing_id"),
                "url": value.get("url"),
                "portal": value.get("portal"),
                "classification": (value.get("classification") or {}).get("final"),
                "reason": (value.get("classification") or {}).get("final_reason") or (value.get("classification") or {}).get("reason"),
                "publisher": value.get("publicador_visible"),
                "title": value.get("title"),
                "description": value.get("description"),
            }
            for value in copy_values[: max(0, int(size))]
        ]
    return sampled


def _invariants(items: list[dict[str, Any]]) -> dict[str, int]:
    assigned = [item for item in items if item.get("assigned_executive") or _assigned_executive(item)]
    broker_assigned = sum(
        1 for item in assigned
        if _canonical_final(item.get("classification") or {}) in {"BROKER_CONFIRMED", "BROKER_PROBABLE"}
        or (item.get("classification") or {}).get("hard_broker_veto")
    )
    without_classification = sum(1 for item in assigned if not _canonical_final(item.get("classification") or {}))
    without_pipeline = sum(
        1 for item in assigned
        if (item.get("classification") or {}).get("pipeline_complete") is not True
    )
    active_keys = [(_item_key(item), _assigned_executive(item)) for item in assigned]
    duplicate_active = len(active_keys) - len(set(active_keys))
    invalid_cycles = sum(
        1 for item in assigned
        if item.get("cycle_valid") is False
        or isinstance(item.get("cycle"), dict) and item["cycle"].get("valid") is False
    )
    return {
        "BROKER_ASSIGNED": broker_assigned,
        "ASSIGNED_WITHOUT_VALID_CLASSIFICATION": without_classification,
        "ASSIGNED_WITHOUT_PIPELINE_COMPLETE": without_pipeline,
        "DUPLICATE_ACTIVE_ASSIGNMENTS": max(0, duplicate_active),
        "INVALID_ASSIGNMENT_CYCLES": invalid_cycles,
    }


def run_toctoc_pipeline(
    discovered_records: Iterable[dict[str, Any]] | None = None,
    *,
    options: PipelineOptions | None = None,
    config: Any | None = None,
    ledger: PipelineLedger | None = None,
    cache: ClassificationCache | None = None,
    budget: AICostGuard | None = None,
    scrape_fn: Callable[[], Iterable[dict[str, Any]]] | None = None,
    preflight_fn: Callable[[], dict[str, Any]] | None = None,
    normalize_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    persist_fn: Callable[[dict[str, Any]], Any] | None = None,
    assign_fn: Callable[[dict[str, Any]], Any] | None = None,
    distribute_fn: Callable[[list[dict[str, Any]]], Any] | None = None,
    reconcile_fn: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    deepseek_callable: Callable[..., Any] | None = None,
    health_baseline: dict[str, float] | None = None,
    qa_seed: int | None = None,
) -> dict[str, Any]:
    """Run the complete TOCTOC workflow through one official entry point."""
    options = options or PipelineOptions()
    ledger = ledger or InMemoryPipelineLedger()
    cache = cache or ClassificationCache()
    config = config or _default_config(options)
    budget = budget or AICostGuard(budget=budget_from_config(config))
    run_id = options.run_id or f"toctoc-{uuid.uuid4().hex}"
    seed = int(qa_seed if qa_seed is not None else int.from_bytes(hashlib.sha256(run_id.encode("utf-8")).digest()[:4], "big"))

    existing_run = ledger.get_run(run_id)
    if _run_is_closed(existing_run) and not options.resume_existing_run:
        return {
            "run_id": run_id,
            "run_status": "STOPPED_REQUIRES_REVIEW",
            "reason": "RUN_ID_CLOSED_OR_ABORTED",
            "existing_run_status": existing_run.get("run_status") or existing_run.get("status"),
            "invariants": {},
        }

    ledger.start_run(run_id, {"pipeline_version": PIPELINE_VERSION, "status": "RUNNING", "dry_run": options.dry_run})
    if not ledger.acquire(run_id):
        return {
            "run_id": run_id,
            "run_status": "STOPPED_REQUIRES_REVIEW",
            "reason": "RUN_LOCKED",
            "invariants": {},
        }

    records: list[dict[str, Any]] = []
    processed_items: list[dict[str, Any]] = []
    anomalies: list[str] = []
    try:
        preflight = preflight_fn() if preflight_fn else {"ok": True}
        if not preflight.get("ok", False):
            report = {"run_id": run_id, "run_status": "STOPPED_REQUIRES_REVIEW", "reason": preflight.get("reason", "PREFLIGHT_FAILED"), "preflight": preflight}
            ledger.finish_run(run_id, report)
            return report

        if discovered_records is not None:
            records = [dict(item) for item in discovered_records]
        elif scrape_fn and (options.allow_real_scraping or options.test_mode):
            records = [dict(item) for item in scrape_fn()]
        else:
            report = {"run_id": run_id, "run_status": "STOPPED_REQUIRES_REVIEW", "reason": "REAL_SCRAPING_DISABLED_OR_NO_INPUT", "real_scraping": False}
            ledger.finish_run(run_id, report)
            return report

        preexisting_item_keys = ledger.list_item_keys(run_id)
        requested_record_count = len(records)
        records, records_skipped_max_items = _cap_records_for_run(
            records,
            existing_item_keys=preexisting_item_keys,
            max_new_items=options.max_new_items,
        )
        for raw in records:
            item_key = _item_key(raw)
            prior_item = ledger.get_item(run_id, item_key) or {}
            if prior_item.get("status") not in {"PERSISTED", "ASSIGNED"}:
                ledger.set_item(run_id, item_key, {"status": "DISCOVERED", "record": raw})

        normalizer = normalize_fn or _normalise_record
        normalized: list[dict[str, Any]] = []
        seen: dict[str, str] = {}
        duplicate_count = 0
        for raw in records:
            try:
                item = normalizer(raw)
                item = _normalise_record(item)
                item_key = _item_key(item)
                prior_item = ledger.get_item(run_id, item_key) or {}
                if prior_item.get("status") not in {"PERSISTED", "ASSIGNED"}:
                    ledger.set_item(run_id, item_key, {"status": "EXTRACTED", "record": item})
                    ledger.set_item(run_id, item_key, {"status": "NORMALIZED", "record": item})
                if item_key in seen:
                    duplicate_count += 1
                    ledger.set_item(run_id, item_key, {"status": "BLOCKED", "reason": "DUPLICATE", "duplicate_of": seen[item_key], "record": item})
                    continue
                seen[item_key] = item_key
                normalized.append(item)
            except Exception as exc:
                item_key = _item_key(raw) or f"invalid-{len(normalized)}"
                ledger.set_item(run_id, item_key, {"status": "FAILED", "reason": f"NORMALIZATION_ERROR:{type(exc).__name__}", "record": raw})
                anomalies.append(f"NORMALIZATION_ERROR:{item_key}")

        health_metrics = measure_extractor_health(normalized)
        health = evaluate_extractor_health(
            health_metrics,
            baseline=health_baseline,
            min_sample_size=options.health_min_sample,
            max_drop_ratio=options.health_drop_ratio,
        )
        if health.get("degraded"):
            anomalies.append("EXTRACTOR_DEGRADED")

        for item in normalized:
            item_key = _item_key(item)
            previous = ledger.get_item(run_id, item_key) or {}
            previous_status = previous.get("status")
            resumable_statuses = CLASSIFICATION_STATUSES | {"ASSIGNABLE", "PERSISTED", "ASSIGNED"}
            classification = previous.get("classification") if previous_status in resumable_statuses else None
            resumed_terminal = previous_status in {"PERSISTED", "ASSIGNED"}
            result: dict[str, Any]
            if classification:
                result = {"classification": classification, "reason": "RESUMED_FROM_LEDGER", "cache_hit": previous_status == "CACHE_HIT", "ai_called": previous_status == "AI_CLASSIFIED", "metrics": budget.report()}
            else:
                hint = item.get("classification_hint")
                if hint is None and isinstance(item.get("classification"), dict):
                    hint = item.get("classification")
                result = classify_capture(
                    item,
                    rule_context=item.get("rule_context") or {},
                    config=config,
                    health=health,
                    cache=cache,
                    budget=budget,
                    classification_hint=hint,
                    human_broker_match=bool(item.get("human_broker_match")),
                    cross_portal_match=bool(item.get("cross_portal_match")),
                    strong_text_broker=bool(item.get("strong_text_broker")),
                    deepseek_callable=deepseek_callable,
                    allow_real_ai=bool(options.allow_real_ai or (options.test_mode and deepseek_callable is not None)),
                )
                classification = result["classification"]

            item["classification"] = classification
            item["cache_hit"] = bool(result.get("cache_hit"))
            item["ai_candidate"] = result.get("reason") in {"deepseek_disabled", "EXTRACTOR_HEALTH_UNAVAILABLE", "AI_BUDGET_EXCEEDED"} or classification.get("status") == "UNCERTAIN_PENDING_AI"
            if result.get("cache_hit"):
                status = "CACHE_HIT"
            elif result.get("ai_called"):
                status = "AI_CLASSIFIED"
            elif classification.get("status") == "UNCERTAIN_PENDING_AI":
                status = "PENDING_AI"
            elif classification.get("pipeline_complete") is True:
                status = "CLASSIFIED_DETERMINISTIC"
            else:
                status = "FAILED"
            ledger.set_item(run_id, item_key, {"status": status, "classification": classification, "record": item, "cache_hit": item["cache_hit"], "ai_candidate": item["ai_candidate"]})

            gate_doc = {**item, "classification": classification, "pipeline_complete": classification.get("pipeline_complete"), "pipeline_state": classification.get("pipeline_state")}
            gate = can_assign_property(gate_doc, context={"broker_identity_match": result.get("registry_match") or {}, "contact_identity": item.get("contact_identity")})
            item["assignment_decision"] = gate
            item["assignment_ready"] = bool(gate.get("assignment_ready"))
            conflict = bool(
                item.get("classification_conflict")
                or classification.get("classification_conflict")
                or classification.get("conflict")
                or (result.get("registry_match") or {}).get("conflict")
            )
            if health.get("degraded") or budget.budget_exceeded or conflict:
                item["assignment_ready"] = False
                gate["assignment_ready"] = False
                gate.setdefault("assignment_block_reasons", []).append(
                    "CLASSIFICATION_CONFLICT" if conflict else "PIPELINE_FAIL_CLOSED"
                )
            if not item["assignment_ready"]:
                ledger.set_item(run_id, item_key, {"status": "BLOCKED", "classification": classification, "assignment_decision": gate, "record": item})
                processed_items.append(item)
                continue

            ledger.set_item(run_id, item_key, {"status": "ASSIGNABLE", "classification": classification, "assignment_decision": gate, "record": item})
            writes_enabled = options.allow_mongo_writes and (not options.dry_run or options.test_mode)
            if writes_enabled and persist_fn and not resumed_terminal:
                try:
                    persist_fn(item)
                    ledger.set_item(run_id, item_key, {"status": "PERSISTED", "record": item})
                except Exception as exc:
                    ledger.set_item(run_id, item_key, {"status": "FAILED", "reason": f"PERSIST_ERROR:{type(exc).__name__}", "record": item})
                    anomalies.append(f"PERSIST_ERROR:{item_key}")
                    processed_items.append(item)
                    continue
            if options.allow_assignments and assign_fn and writes_enabled and not resumed_terminal:
                try:
                    assigned = assign_fn(item)
                    if isinstance(assigned, dict):
                        item.update(assigned)
                    ledger.set_item(run_id, item_key, {"status": "ASSIGNED", "record": item})
                except Exception as exc:
                    ledger.set_item(run_id, item_key, {"status": "FAILED", "reason": f"ASSIGN_ERROR:{type(exc).__name__}", "record": item})
                    anomalies.append(f"ASSIGN_ERROR:{item_key}")
                    processed_items.append(item)
                    continue
            processed_items.append(item)

        if options.allow_distribution and distribute_fn and options.allow_mongo_writes and (not options.dry_run or options.test_mode):
            distribute_fn([item for item in processed_items if item.get("assignment_ready")])

        invariants = _invariants(processed_items)
        if reconcile_fn:
            external = reconcile_fn(processed_items) or {}
            invariants.update({key: int(value or 0) for key, value in external.items() if key in invariants})
        if any(value for value in invariants.values()):
            anomalies.append("INVARIANT_VIOLATION")
        if budget.budget_exceeded:
            anomalies.append("AI_BUDGET_EXCEEDED")

        qa = _qa_sample(processed_items, options.qa_sample_size, seed)
        report = {
            "run_status": "COMPLETED_WITH_ANOMALIES" if anomalies else "SUCCESS",
            "run_id": run_id,
            "pipeline_version": PIPELINE_VERSION,
            "total_discovered": len(records),
            "requested_records": requested_record_count,
            "max_new_items": options.max_new_items,
            "records_skipped_max_items": records_skipped_max_items,
            "preexisting_ledger_items": len(preexisting_item_keys),
            "ledger_items_after_run": len(ledger.list_item_keys(run_id)),
            "duplicates": duplicate_count,
            "total_processed": len(processed_items),
            "new_properties": len(processed_items),
            "health": health,
            "extractor_health": "DEGRADED" if health.get("degraded") else "HEALTHY",
            "invariants": invariants,
            "anomalies": sorted(set(anomalies)),
            "ai": budget.report(),
            "qa_sample_seed": seed,
            "qa_sample": qa,
            "assigned": sum(1 for item in processed_items if _assigned_executive(item)),
            "assignable": sum(1 for item in processed_items if item.get("assignment_ready")),
            "not_assigned": sum(1 for item in processed_items if not _assigned_executive(item)),
            "real_deepseek_calls": 0 if options.test_mode or deepseek_callable is not None else budget.calls_executed,
            "real_scraping": bool(options.allow_real_scraping and not options.test_mode),
            "migration_executed": False,
        }
        ledger.finish_run(run_id, report)
        return report
    except Exception as exc:
        report = {"run_id": run_id, "run_status": "COMPLETED_WITH_ANOMALIES", "reason": f"PIPELINE_ERROR:{type(exc).__name__}", "error": str(exc), "real_deepseek_calls": 0}
        ledger.finish_run(run_id, report)
        return report
    finally:
        ledger.release(run_id)
