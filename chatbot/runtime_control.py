"""Atomic runtime ownership control for the customer-response pipelines.

The legacy chatbot queue (V1) and the opt-in immutable-turn queue (V2) are
separate implementations, but they must share one ownership decision.  This
module contains only routing and safety primitives.  It deliberately knows
nothing about prompts, property facts, model providers, or WhatsApp payloads.

The control document is a small compare-and-swap state machine.  A producer
stamps an inbound once, and a worker must present that immutable stamp again
at claim time and at the network boundary.  During a drain, the old worker
may finish bookkeeping, but it is never authorised to make a new provider
call.  This is the generation fence that prevents an old worker from sending
after a cutover.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError


CONTROL_COLLECTION = "chatbot_runtime_control"
CONTROL_ID = "customer_response_pipeline"

V1_ACTIVE = "V1_ACTIVE"
DRAINING_TO_V2 = "DRAINING_TO_V2"
V2_ACTIVE = "V2_ACTIVE"
DRAINING_TO_V1 = "DRAINING_TO_V1"

V1 = "v1"
V2 = "v2"

_MODES = {V1_ACTIVE, DRAINING_TO_V2, V2_ACTIVE, DRAINING_TO_V1}
_PIPELINES = {V1, V2}
_DRAIN_TARGET = {
    V1_ACTIVE: None,
    DRAINING_TO_V2: V2,
    V2_ACTIVE: V2,
    DRAINING_TO_V1: V1,
}
_ALLOWED_TRANSITIONS = {
    (V1_ACTIVE, DRAINING_TO_V2),
    (DRAINING_TO_V2, V2_ACTIVE),
    (V2_ACTIVE, DRAINING_TO_V1),
    (DRAINING_TO_V1, V1_ACTIVE),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _initial_document(mode: str, now: datetime) -> dict[str, Any]:
    if mode not in _MODES:
        raise ValueError("invalid_runtime_control_mode")
    pipeline = _DRAIN_TARGET[mode] or (V2 if mode == V2_ACTIVE else V1)
    return {
        "_id": CONTROL_ID,
        "mode": mode,
        "generation": 1,
        "target": pipeline,
        "created_at": now,
        "updated_at": now,
        "last_transition_at": None,
        "previous_mode": None,
        "previous_generation": None,
        "draining_pipeline": None,
        "draining_generation": None,
    }


def ensure_runtime_control(db, *, initial_mode: str = V1_ACTIVE,
                            now: datetime | None = None) -> dict[str, Any]:
    """Create the control record once; never overwrite an existing mode.

    Production starts in V1_ACTIVE.  Tests or an isolated V2 environment may
    create the record with V2_ACTIVE, but an existing control record is always
    authoritative and is never silently changed by startup.
    """
    now = _utc(now) or utc_now()
    existing = db[CONTROL_COLLECTION].find_one({"_id": CONTROL_ID})
    if existing:
        return existing
    document = _initial_document(initial_mode, now)
    try:
        db[CONTROL_COLLECTION].insert_one(document)
    except DuplicateKeyError:
        pass
    return db[CONTROL_COLLECTION].find_one({"_id": CONTROL_ID}) or document


def ensure_runtime_control_indexes(db) -> None:
    try:
        db[CONTROL_COLLECTION].create_index(
            [("mode", ASCENDING), ("generation", ASCENDING)],
            name="runtime_control_mode_generation",
        )
    except (KeyError, AssertionError, AttributeError, TypeError):
        # Queue-only test doubles from the legacy suite expose only the old
        # collection. They still receive the V1 compatibility decision below;
        # real MongoDB always has the canonical control collection.
        return None


def get_runtime_control(db, *, create: bool = True) -> dict[str, Any] | None:
    try:
        document = db[CONTROL_COLLECTION].find_one({"_id": CONTROL_ID})
    except (KeyError, AssertionError, AttributeError, TypeError):
        return _initial_document(V1_ACTIVE, utc_now())
    if document or not create:
        return document
    return ensure_runtime_control(db)


def initialize_runtime_control(db, *, mode: str, now: datetime | None = None) -> dict[str, Any]:
    """Test/bootstrap helper with the same no-overwrite semantics."""
    return ensure_runtime_control(db, initial_mode=mode, now=now)


def route_new_inbound(db, *, now: datetime | None = None) -> dict[str, Any]:
    """Return the immutable route selected at ingest time."""
    control = get_runtime_control(db)
    mode = control["mode"]
    if mode not in _MODES:
        raise RuntimeError("invalid_runtime_control_state")
    pipeline = V1 if mode in {V1_ACTIVE, DRAINING_TO_V1} else V2
    return {
        "pipeline_version": pipeline,
        "runtime_generation": int(control.get("generation", 1)),
        "runtime_mode_at_ingest": mode,
        "stamped_at": _utc(now) or utc_now(),
    }


def stamp_inbound_at_ingest(db, job: Mapping[str, Any], *, now: datetime | None = None,
                            route: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Copy a job and add the immutable pipeline stamp exactly once."""
    result = deepcopy(dict(job))
    if result.get("pipeline_version") and result.get("runtime_generation") is not None:
        return result
    selected = dict(route or route_new_inbound(db, now=now))
    result["pipeline_version"] = selected["pipeline_version"]
    result["runtime_generation"] = int(selected["runtime_generation"])
    result["runtime_mode_at_ingest"] = selected["runtime_mode_at_ingest"]
    result["runtime_stamped_at"] = selected["stamped_at"]
    return result


def stamp_persisted_job(db, collection: str, job_id: Any, *, now: datetime | None = None,
                        route: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Stamp one just-created job without ever rewriting an existing stamp.

    The producer should prefer stamping before insertion.  This helper is a
    safe compatibility boundary for the legacy producer and uses a guarded
    update so two producers cannot disagree about the route.
    """
    selected = dict(route or route_new_inbound(db, now=now))
    updated = db[collection].find_one_and_update(
        {"_id": job_id, "$or": [
            {"pipeline_version": {"$exists": False}},
            {"runtime_generation": {"$exists": False}},
        ]},
        {"$set": {
            "pipeline_version": selected["pipeline_version"],
            "runtime_generation": int(selected["runtime_generation"]),
            "runtime_mode_at_ingest": selected["runtime_mode_at_ingest"],
            "runtime_stamped_at": selected["stamped_at"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    return updated or db[collection].find_one({"_id": job_id})


def transition_runtime(db, *, to_mode: str, expected_mode: str | None = None,
                       now: datetime | None = None) -> dict[str, Any] | None:
    """Atomically move the runtime state and increment its generation.

    ``None`` means another process won the compare-and-swap or the transition
    is invalid.  Callers must treat that as a failed transition, never retry a
    provider send under stale assumptions.
    """
    if to_mode not in _MODES:
        raise ValueError("invalid_runtime_control_mode")
    now = _utc(now) or utc_now()
    current = get_runtime_control(db)
    current_mode = current["mode"]
    if expected_mode is not None and current_mode != expected_mode:
        return None
    if (current_mode, to_mode) not in _ALLOWED_TRANSITIONS:
        return None
    generation = int(current.get("generation", 1))
    target = _DRAIN_TARGET[to_mode] or (V2 if to_mode == V2_ACTIVE else V1)
    update = {
        "$set": {
            "mode": to_mode,
            "target": target,
            "updated_at": now,
            "last_transition_at": now,
            "previous_mode": current_mode,
            "previous_generation": generation,
            "draining_pipeline": (
                V1 if current_mode in {V1_ACTIVE, DRAINING_TO_V2} and to_mode == DRAINING_TO_V2
                else V2 if current_mode in {V2_ACTIVE, DRAINING_TO_V1} and to_mode == DRAINING_TO_V1
                else None
            ),
            "draining_generation": (
                generation if to_mode in {DRAINING_TO_V2, DRAINING_TO_V1} else None
            ),
        },
        "$inc": {"generation": 1},
    }
    return db[CONTROL_COLLECTION].find_one_and_update(
        {"_id": CONTROL_ID, "mode": current_mode, "generation": generation},
        update,
        return_document=ReturnDocument.AFTER,
    )


def _version_generation(job_or_turn: Mapping[str, Any]) -> tuple[str | None, int | None]:
    version = job_or_turn.get("pipeline_version")
    generation = job_or_turn.get("runtime_generation")
    try:
        generation = int(generation) if generation is not None else None
    except (TypeError, ValueError):
        generation = None
    return (str(version) if version else None), generation


def _draining_stamp_matches(control: Mapping[str, Any], version: str, generation: int) -> bool:
    return (
        control.get("draining_pipeline") == version
        and int(control.get("draining_generation", -1)) == int(generation)
    )


def _active_stamp_matches(control: Mapping[str, Any], version: str, generation: int) -> bool:
    current_generation = int(control.get("generation", 1))
    if generation == current_generation:
        return True
    # Inbound work accepted during a drain is stamped with the drain
    # generation. Once the target becomes active the activation increments
    # the generation, so that pending work must remain admissible under the
    # immediately preceding target generation.
    prior_mode = control.get("previous_mode")
    prior_generation = control.get("previous_generation")
    target = control.get("target")
    return (
        target == version
        and prior_mode in {DRAINING_TO_V2, DRAINING_TO_V1}
        and prior_generation is not None
        and int(prior_generation) == int(generation)
    )


def worker_can_claim(db, worker_or_job: Mapping[str, Any], *, pipeline_version: str | None = None,
                     runtime_generation: int | None = None) -> bool:
    """Whether a stamped job may be claimed by its pipeline worker."""
    control = get_runtime_control(db)
    version, generation = _version_generation(worker_or_job)
    version = pipeline_version or version
    generation = runtime_generation if runtime_generation is not None else generation
    if version not in _PIPELINES or generation is None:
        return False
    mode = control["mode"]
    current_generation = int(control.get("generation", 1))
    if mode == V1_ACTIVE:
        return version == V1 and _active_stamp_matches(control, version, generation)
    if mode == V2_ACTIVE:
        return version == V2 and _active_stamp_matches(control, version, generation)
    # During either drain, old work can be accounted for and claimed, but its
    # provider send is still fenced.  New work is stamped for the target.
    return _draining_stamp_matches(control, version, generation)


def worker_can_send(db, worker_or_job: Mapping[str, Any], *, pipeline_version: str | None = None,
                    runtime_generation: int | None = None) -> bool:
    """Final network-boundary fence; false means no provider call is allowed."""
    control = get_runtime_control(db)
    version, generation = _version_generation(worker_or_job)
    version = pipeline_version or version
    generation = runtime_generation if runtime_generation is not None else generation
    if version not in _PIPELINES or generation is None:
        return False
    mode = control["mode"]
    current_generation = int(control.get("generation", 1))
    if mode == V1_ACTIVE:
        return version == V1 and _active_stamp_matches(control, version, generation)
    if mode == V2_ACTIVE:
        return version == V2 and _active_stamp_matches(control, version, generation)
    # Entering a drain fences the previous generation immediately.  The old
    # worker may mark its turn fenced/superseded, but never invokes Wasender.
    return False


def runtime_fence(db, worker_or_job: Mapping[str, Any], *, phase: str,
                  pipeline_version: str | None = None,
                  runtime_generation: int | None = None) -> dict[str, Any]:
    control = get_runtime_control(db)
    allowed = worker_can_send(
        db, worker_or_job, pipeline_version=pipeline_version,
        runtime_generation=runtime_generation,
    ) if phase == "send" else worker_can_claim(
        db, worker_or_job, pipeline_version=pipeline_version,
        runtime_generation=runtime_generation,
    )
    version, generation = _version_generation(worker_or_job)
    return {
        "allowed": bool(allowed),
        "phase": phase,
        "reason": "authorized" if allowed else "FENCED_OUT",
        "pipeline_version": pipeline_version or version,
        "runtime_generation": runtime_generation if runtime_generation is not None else generation,
        "current_mode": control.get("mode"),
        "current_generation": int(control.get("generation", 1)),
    }


def provider_attempt_is_uncertain(job_or_batch: Mapping[str, Any]) -> bool:
    """True if retry/migration could duplicate an already-started send."""
    if job_or_batch.get("outbound_provider_message_id"):
        return True
    if job_or_batch.get("provider_message_id"):
        return True
    if job_or_batch.get("accepted_delivery_token"):
        return True
    for attempt in job_or_batch.get("delivery_attempts") or []:
        if str(attempt.get("status") or "").lower() in {
            "started", "provider_attempt", "accepted", "sent", "delivered", "read",
        }:
            return True
        if attempt.get("provider_message_id"):
            return True
    return False


def migration_decision(job_or_batch: Mapping[str, Any]) -> dict[str, Any]:
    uncertain = provider_attempt_is_uncertain(job_or_batch)
    return {
        "eligible": not uncertain,
        "decision": "MIGRATE_TO_TARGET" if not uncertain else "PRESERVE_TERMINAL_OR_UNKNOWN",
        "reason": "no_provider_attempt" if not uncertain else "provider_attempt_or_acceptance_exists",
    }


def pipeline_drain_snapshot(db, *, pipeline_version: str,
                            legacy_collection: str = "chatbot_inbound_jobs",
                            v2_job_collection: str = "chatbot_turn_jobs",
                            v2_turn_collection: str = "conversation_turns_v2") -> dict[str, int | bool]:
    """Count only active work for one pipeline; terminal historical debt is ignored."""
    active_states = {"received", "batching", "pending", "processing", "failed_retryable"}
    result = {"batches": 0, "jobs": 0, "turns": 0, "processing": 0,
              "pending": 0, "failed_retryable": 0}
    if pipeline_version == V1:
        query = {"kind": "response_batch", "state": {"$in": list(active_states)},
                 "$or": [{"pipeline_version": V1}, {"pipeline_version": {"$exists": False}}]}
        result["batches"] = db[legacy_collection].count_documents(query)
        job_query = {"kind": "inbound_job", "state": {"$in": list(active_states)},
                     "$or": [{"pipeline_version": V1}, {"pipeline_version": {"$exists": False}}]}
        result["jobs"] = db[legacy_collection].count_documents(job_query)
    else:
        result["turns"] = db[v2_turn_collection].count_documents(
            {"kind": "immutable_conversational_turn", "state": {"$in": ["pending", "processing"]},
             "pipeline_version": V2},
        )
        result["jobs"] = db[v2_job_collection].count_documents(
            {"kind": "immutable_inbound_job", "state": {"$in": ["received", "pending", "processing"]},
             "pipeline_version": V2},
        )
    result["processing"] = db[legacy_collection if pipeline_version == V1 else v2_turn_collection].count_documents(
        {"kind": "response_batch" if pipeline_version == V1 else "immutable_conversational_turn",
         "state": "processing", **({"pipeline_version": pipeline_version} if pipeline_version == V2 else {})},
    ) if result["batches"] or result["turns"] else 0
    result["pending"] = int(result["batches"] + result["turns"] - result["processing"])
    result["drained"] = not any(int(result[key]) for key in ("batches", "jobs", "turns"))
    return result


def activate_after_drain(db, *, expected_drain_mode: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Complete a drain only when the old pipeline has no active work."""
    if expected_drain_mode not in {DRAINING_TO_V2, DRAINING_TO_V1}:
        raise ValueError("invalid_drain_mode")
    old = V1 if expected_drain_mode == DRAINING_TO_V2 else V2
    snapshot = pipeline_drain_snapshot(db, pipeline_version=old)
    if not snapshot["drained"]:
        return None
    target = V2_ACTIVE if expected_drain_mode == DRAINING_TO_V2 else V1_ACTIVE
    return transition_runtime(db, to_mode=target, expected_mode=expected_drain_mode, now=now)


@dataclass(frozen=True)
class RuntimeStamp:
    pipeline_version: str
    runtime_generation: int
    mode_at_ingest: str
