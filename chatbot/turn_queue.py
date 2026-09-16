"""Immutable conversational turns for the opt-in chatbot queue v2.

This module is deliberately isolated from :mod:`chatbot.chatbot_queue`.
The legacy worker owns ``response_batch`` documents and remains untouched
until the v2 invariants have been proven.  The v2 ownership boundary is:

* an inbound job is inserted once, keyed by the provider message id;
* a pending turn may collect only jobs while it is pending;
* claiming atomically moves the pending ids into an immutable snapshot;
* later inbounds create a successor pending turn;
* finalization updates only the claimed snapshot ids; and
* provider admission performs the last freshness/ownership check immediately
  before the caller makes the network request.

The functions accept a normal PyMongo database and are also usable with
``mongomock`` for the no-I/O validation suite.  No feature flag is enabled by
this module; callers must explicitly opt in with ``CHATBOT_TURN_QUEUE_V2``.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from . import runtime_control as runtime


JOB_COLLECTION = "chatbot_turn_jobs"
TURN_COLLECTION = "conversation_turns_v2"
STATE_COLLECTION = "chatbot_conversation_state_v2"
COUNTER_COLLECTION = "chatbot_turn_counters_v2"
PRESENTATION_COLLECTION = "chatbot_property_presentations_v2"

KIND_JOB = "immutable_inbound_job"
KIND_TURN = "immutable_conversational_turn"

TURN_PENDING = "pending"
TURN_PROCESSING = "processing"
TURN_RESPONDED = "responded"
TURN_SUPERSEDED = "superseded"
TURN_NO_REPLY = "no_reply_policy"
TURN_HUMAN_SUPPRESSED = "human_suppressed"
TURN_FAILED_TERMINAL = "failed_terminal"
TURN_DELIVERY_UNKNOWN = "delivery_unknown"

JOB_RECEIVED = "received"
JOB_PENDING = "pending"
JOB_PROCESSING = "processing"
JOB_RESPONDED = "responded"
JOB_SUPERSEDED = "superseded_into_newer_turn"
JOB_NO_REPLY = "no_reply_policy"
JOB_HUMAN_SUPPRESSED = "human_suppressed"
JOB_FAILED_TERMINAL = "failed_terminal"
JOB_DELIVERY_UNKNOWN = "delivery_unknown"

TERMINAL_TURN_STATES = {
    TURN_RESPONDED,
    TURN_SUPERSEDED,
    TURN_NO_REPLY,
    TURN_HUMAN_SUPPRESSED,
    TURN_FAILED_TERMINAL,
    TURN_DELIVERY_UNKNOWN,
}
TERMINAL_JOB_STATES = {
    JOB_RESPONDED,
    JOB_SUPERSEDED,
    JOB_NO_REPLY,
    JOB_HUMAN_SUPPRESSED,
    JOB_FAILED_TERMINAL,
    JOB_DELIVERY_UNKNOWN,
}


class TurnSuperseded(RuntimeError):
    """Raised when a response was generated from a no-longer-current turn."""


class TurnOwnershipLost(RuntimeError):
    """Raised when a worker no longer owns the claimed turn."""


@dataclass(frozen=True)
class InboundResult:
    job_id: str
    turn_id: str
    duplicate: bool = False


@dataclass(frozen=True)
class DeliveryResult:
    state: str
    provider_message_id: str | None = None
    provider_status: str | None = None
    sent: bool = False
    superseded: bool = False
    human_suppressed: bool = False
    fenced_out: bool = False


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


def _conversation_key(phone: str, conversation_id: str | None) -> str:
    return f"conversation:{conversation_id}" if conversation_id else f"phone:{phone}"


def turn_queue_v2_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return the explicit opt-in flag without enabling it by default."""
    values = environ if environ is not None else os.environ
    return str(values.get("CHATBOT_TURN_QUEUE_V2", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def ensure_indexes(db) -> None:
    """Create the v2 uniqueness/lookup boundaries."""
    runtime.ensure_runtime_control_indexes(db)
    # Release A replaces the old one-pending-turn-per-conversation constraint
    # with one per pipeline.  Removing only this obsolete index is required
    # for V1/V2 coexistence during rollback; no documents are touched.
    try:
        db[TURN_COLLECTION].drop_index("uniq_v2_pending_turn_per_conversation")
    except Exception:
        pass
    db[JOB_COLLECTION].create_index(
        [("provider_message_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"kind": KIND_JOB},
        name="uniq_v2_provider_message_id",
    )
    db[JOB_COLLECTION].create_index(
        [("conversation_key", ASCENDING), ("arrival_sequence", ASCENDING)],
        name="v2_conversation_arrival",
    )
    db[TURN_COLLECTION].create_index(
        [("conversation_key", ASCENDING), ("pipeline_version", ASCENDING),
         ("state", ASCENDING)],
        unique=True,
        partialFilterExpression={"kind": KIND_TURN, "state": TURN_PENDING,
                                 "pipeline_version": {"$exists": True}},
        name="uniq_v2_pending_turn_per_pipeline",
    )
    db[TURN_COLLECTION].create_index(
        [("conversation_key", ASCENDING), ("turn_sequence", DESCENDING)],
        name="v2_turn_order",
    )
    db[STATE_COLLECTION].create_index(
        [("conversation_key", ASCENDING)], unique=True,
        name="uniq_v2_conversation_state",
    )
    db[PRESENTATION_COLLECTION].create_index(
        [("turn_id", ASCENDING), ("property_id", ASCENDING),
         ("provider_message_id", ASCENDING)],
        unique=True,
        name="uniq_v2_property_presentation",
    )


def _next_arrival_sequence(db, conversation_key: str) -> int:
    for attempt in range(3):
        try:
            counter = db[COUNTER_COLLECTION].find_one_and_update(
                {"_id": conversation_key},
                {"$inc": {"arrival_sequence": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return int(counter.get("arrival_sequence", 1))
        except DuplicateKeyError:
            # A lightweight Mongo fake may not serialize concurrent upserts
            # exactly like a real replica set.  Retry without changing the
            # inbound job; the increment itself remains atomic in Mongo.
            if attempt == 2:
                raise
    raise RuntimeError("v2_sequence_counter_failed")


def _turn_id() -> str:
    return f"turn:{uuid.uuid4()}"


def _job_id() -> str:
    return f"job:{uuid.uuid4()}"


def _window_end(now: datetime, quiet_seconds: float) -> datetime:
    return now + timedelta(seconds=max(float(quiet_seconds), 0.0))


_ATTACH_LOCKS = defaultdict(threading.RLock)


def _latest_turn(db, conversation_key: str) -> dict[str, Any] | None:
    return db[TURN_COLLECTION].find_one(
        {"kind": KIND_TURN, "conversation_key": conversation_key},
        sort=[("turn_sequence", DESCENDING), ("created_at", DESCENDING)],
    )


def _create_pending_turn(
    db,
    *,
    conversation_key: str,
    phone: str,
    conversation_id: str | None,
    lead_id: str | None,
    first_sequence: int,
    pipeline_version: str,
    runtime_generation: int,
    parent_turn_id: str | None,
    now: datetime,
    quiet_seconds: float,
) -> dict[str, Any]:
    turn = {
        "_id": _turn_id(),
        "kind": KIND_TURN,
        "conversation_key": conversation_key,
        "phone": phone,
        "conversation_id": conversation_id,
        "lead_id": lead_id,
        "turn_sequence": int(first_sequence),
        "pipeline_version": pipeline_version,
        "runtime_generation": int(runtime_generation),
        "parent_turn_id": parent_turn_id,
        "state": TURN_PENDING,
        "pending_job_ids": [],
        "last_sequence": int(first_sequence),
        "latest_conversation_sequence": int(first_sequence),
        "window_started_at": now,
        "last_message_at": now,
        "window_end_at": _window_end(now, quiet_seconds),
        "created_at": now,
        "updated_at": now,
        "message_domain": "chatbot",
        "ownership": "turn_queue_v2",
    }
    try:
        db[TURN_COLLECTION].insert_one(turn)
    except DuplicateKeyError:
        # Another producer won the single pending-turn slot.  The caller will
        # retrieve it and attach to it with a state-guarded update.
        pass
    return db[TURN_COLLECTION].find_one(
        {"kind": KIND_TURN, "conversation_key": conversation_key,
         "state": TURN_PENDING, "pipeline_version": pipeline_version},
        sort=[("turn_sequence", DESCENDING)],
    ) or turn


def _attach_job_to_pending_turn_locked(
    db,
    *,
    job_id: str,
    phone: str,
    conversation_id: str | None,
    lead_id: str | None,
    conversation_key: str,
    arrival_sequence: int,
    pipeline_version: str,
    runtime_generation: int,
    now: datetime,
    quiet_seconds: float,
) -> dict[str, Any]:
    """Attach only to a pending turn; processing turns are never appended."""
    for _ in range(8):
        pending = db[TURN_COLLECTION].find_one(
            {"kind": KIND_TURN, "conversation_key": conversation_key,
             "state": TURN_PENDING, "pipeline_version": pipeline_version},
            sort=[("turn_sequence", DESCENDING)],
        )
        if pending:
            current_end = _utc(pending.get("window_end_at")) or now
            new_end = max(current_end, _window_end(now, quiet_seconds))
            result = db[TURN_COLLECTION].update_one(
                {"_id": pending["_id"], "kind": KIND_TURN,
                 "state": TURN_PENDING},
                {"$addToSet": {"pending_job_ids": job_id},
                 "$set": {"last_message_at": now, "updated_at": now,
                           "window_end_at": new_end},
                 "$max": {"last_sequence": int(arrival_sequence),
                           "latest_conversation_sequence": int(arrival_sequence)}},
            )
            if result.matched_count:
                db[JOB_COLLECTION].update_one(
                    {"_id": job_id, "kind": KIND_JOB,
                     "state": {"$in": [JOB_RECEIVED, JOB_PENDING]}},
                    {"$set": {"turn_id": pending["_id"],
                              "state": JOB_PENDING, "updated_at": now}},
                )
                return db[TURN_COLLECTION].find_one({"_id": pending["_id"]})
            # It transitioned to processing between the read and update.  Do
            # not retry the same id against it; the next iteration creates the
            # successor turn.
            continue

        processing = db[TURN_COLLECTION].find_one(
            {"kind": KIND_TURN, "conversation_key": conversation_key,
             "state": TURN_PROCESSING, "pipeline_version": pipeline_version},
            sort=[("turn_sequence", DESCENDING)],
        )
        latest = _latest_turn(db, conversation_key)
        parent = processing or latest
        successor = _create_pending_turn(
            db,
            conversation_key=conversation_key,
            phone=phone,
            conversation_id=conversation_id,
            lead_id=lead_id,
            first_sequence=arrival_sequence,
            pipeline_version=pipeline_version,
            runtime_generation=runtime_generation,
            parent_turn_id=parent.get("_id") if parent else None,
            now=now,
            quiet_seconds=quiet_seconds,
        )
        # This update is only a watermark on the old turn.  It never adds the
        # new job to its snapshot or pending ids.
        if processing:
            db[TURN_COLLECTION].update_one(
                {"_id": processing["_id"], "state": TURN_PROCESSING},
                {"$max": {"latest_conversation_sequence": int(arrival_sequence)},
                 "$set": {"updated_at": now}},
            )
        # If another producer created a pending successor, the next loop will
        # attach through the state guard.  Otherwise attach to this one.
        if successor.get("state") == TURN_PENDING:
            continue
    raise RuntimeError("v2_pending_turn_attach_failed")


def _attach_job_to_pending_turn(
    db,
    *,
    job_id: str,
    phone: str,
    conversation_id: str | None,
    lead_id: str | None,
    conversation_key: str,
    arrival_sequence: int,
    pipeline_version: str,
    runtime_generation: int,
    now: datetime,
    quiet_seconds: float,
) -> dict[str, Any]:
    """Serialize local producers; Mongo's unique index covers other hosts."""
    with _ATTACH_LOCKS[conversation_key]:
        return _attach_job_to_pending_turn_locked(
            db,
            job_id=job_id,
            phone=phone,
            conversation_id=conversation_id,
            lead_id=lead_id,
            conversation_key=conversation_key,
            arrival_sequence=arrival_sequence,
            pipeline_version=pipeline_version,
            runtime_generation=runtime_generation,
            now=now,
            quiet_seconds=quiet_seconds,
        )


def persist_inbound(
    db,
    *,
    provider_message_id: str,
    conversation_id: str | None,
    lead_id: str | None,
    phone: str,
    text: str,
    received_at: datetime | None = None,
    quiet_seconds: float = 2.0,
) -> InboundResult:
    """Persist an inbound exactly once and attach it to pending work."""
    provider_id = str(provider_message_id or "").strip()
    body = str(text or "").strip()
    if not provider_id:
        raise ValueError("provider_message_id_required")
    if not body:
        raise ValueError("inbound_text_required")
    if not str(phone or "").strip():
        raise ValueError("phone_required")
    now = _utc(received_at) or utc_now()
    key = _conversation_key(phone, conversation_id)
    route = runtime.route_new_inbound(db, now=now)
    existing = db[JOB_COLLECTION].find_one(
        {"kind": KIND_JOB, "provider_message_id": provider_id},
    )
    if existing:
        return InboundResult(
            job_id=existing["_id"], turn_id=existing.get("turn_id") or "",
            duplicate=True,
        )
    sequence = _next_arrival_sequence(db, key)
    job_id = _job_id()
    job = {
        "_id": job_id,
        "kind": KIND_JOB,
        "provider_message_id": provider_id,
        "conversation_id": conversation_id,
        "conversation_key": key,
        "lead_id": lead_id,
        "phone": phone,
        "text": body,
        "received_at": now,
        "arrival_sequence": sequence,
        "pipeline_version": route["pipeline_version"],
        "runtime_generation": int(route["runtime_generation"]),
        "runtime_mode_at_ingest": route["runtime_mode_at_ingest"],
        "runtime_stamped_at": route["stamped_at"],
        "state": JOB_RECEIVED,
        "created_at": now,
        "updated_at": now,
        "message_domain": "chatbot",
        "ownership": "turn_queue_v2",
        # These values are intentionally never rewritten after insert.
        "immutable": {
            "provider_message_id": provider_id,
            "text": body,
            "received_at": now,
        },
    }
    try:
        db[JOB_COLLECTION].insert_one(job)
    except DuplicateKeyError:
        existing = db[JOB_COLLECTION].find_one(
            {"kind": KIND_JOB, "provider_message_id": provider_id},
        )
        if not existing:
            raise
        return InboundResult(
            job_id=existing["_id"], turn_id=existing.get("turn_id") or "",
            duplicate=True,
        )
    turn = _attach_job_to_pending_turn(
        db,
        job_id=job_id,
        phone=phone,
        conversation_id=conversation_id,
        lead_id=lead_id,
        conversation_key=key,
        arrival_sequence=sequence,
        pipeline_version=route["pipeline_version"],
        runtime_generation=int(route["runtime_generation"]),
        now=now,
        quiet_seconds=quiet_seconds,
    )
    return InboundResult(job_id=job_id, turn_id=turn["_id"])


def _snapshot_payload(db, job_ids: Iterable[str]) -> list[dict[str, Any]]:
    jobs = list(db[JOB_COLLECTION].find(
        {"kind": KIND_JOB, "_id": {"$in": list(job_ids)}},
    ))
    jobs.sort(key=lambda item: (int(item.get("arrival_sequence", 0)), str(item["_id"])))
    return [
        {
            "job_id": job["_id"],
            "provider_message_id": job.get("provider_message_id"),
            "text": job.get("text"),
            "received_at": job.get("received_at"),
            "arrival_sequence": job.get("arrival_sequence"),
            "pipeline_version": job.get("pipeline_version"),
            "runtime_generation": job.get("runtime_generation"),
        }
        for job in jobs
    ]


def claim_pending_turn(
    db,
    *,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: float = 120.0,
) -> dict[str, Any] | None:
    """Atomically freeze one pending turn into a processing snapshot."""
    now = _utc(now) or utc_now()
    candidates = db[TURN_COLLECTION].find(
        {"kind": KIND_TURN, "state": TURN_PENDING,
         "window_end_at": {"$lte": now},
         "pending_job_ids": {"$exists": True, "$ne": []},
        "$or": [{"lease_until": {"$exists": False}},
                 {"lease_until": {"$lte": now}}]},
        sort=[("window_end_at", ASCENDING), ("turn_sequence", ASCENDING)],
    )
    candidate = next((item for item in candidates
                      if runtime.worker_can_claim(db, item)), None)
    if not candidate:
        return None
    # The pipeline copies the current pending array and clears it in one
    # atomic document update.  A producer racing this operation either lands
    # in this snapshot or observes PROCESSING and creates a successor.
    claimed = db[TURN_COLLECTION].find_one_and_update(
        {"_id": candidate["_id"], "kind": KIND_TURN, "state": TURN_PENDING,
         "window_end_at": {"$lte": now},
         "pending_job_ids": {"$exists": True, "$ne": []},
         "$or": [{"lease_until": {"$exists": False}},
                 {"lease_until": {"$lte": now}}]},
        [{"$set": {
            "state": TURN_PROCESSING,
            "claimed_by": worker_id,
            "claimed_at": now,
            "lease_until": now + timedelta(seconds=float(lease_seconds)),
            "snapshot_job_ids": "$pending_job_ids",
            "snapshot_sequence": "$last_sequence",
            "pending_job_ids": [],
            "updated_at": now,
            "provider_send_admitted_at": None,
        }}],
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        return None
    snapshot = _snapshot_payload(db, claimed.get("snapshot_job_ids") or [])
    if len(snapshot) != len(claimed.get("snapshot_job_ids") or []):
        db[TURN_COLLECTION].update_one(
            {"_id": claimed["_id"], "state": TURN_PROCESSING,
             "claimed_by": worker_id},
            {"$set": {"state": TURN_FAILED_TERMINAL,
                      "last_error": "missing_snapshot_job",
                      "updated_at": now}},
        )
        raise RuntimeError("v2_missing_snapshot_job")
    snapshot_text = "\n".join(item["text"] for item in snapshot)
    update = db[TURN_COLLECTION].update_one(
        {"_id": claimed["_id"], "state": TURN_PROCESSING,
         "claimed_by": worker_id},
        {"$set": {"snapshot": snapshot, "snapshot_text": snapshot_text,
                  "updated_at": now}},
    )
    if not update.matched_count:
        raise TurnOwnershipLost("v2_claim_lease_lost")
    db[JOB_COLLECTION].update_many(
        {"kind": KIND_JOB, "_id": {"$in": claimed["snapshot_job_ids"]},
         "$or": [{"turn_id": {"$exists": False}},
                 {"turn_id": claimed["_id"]}],
         "state": {"$nin": list(TERMINAL_JOB_STATES)}},
        {"$set": {"state": JOB_PROCESSING, "processing_turn_id": claimed["_id"],
                  "turn_id": claimed["_id"],
                  "updated_at": now}},
    )
    return db[TURN_COLLECTION].find_one({"_id": claimed["_id"]})


def latest_conversation_sequence(db, conversation_key: str) -> int:
    counter = db[COUNTER_COLLECTION].find_one({"_id": conversation_key}) or {}
    latest_job = db[JOB_COLLECTION].find_one(
        {"kind": KIND_JOB, "conversation_key": conversation_key},
        sort=[("arrival_sequence", DESCENDING)],
    )
    return max(
        int(counter.get("arrival_sequence", 0) or 0),
        int((latest_job or {}).get("arrival_sequence", 0) or 0),
    )


def _turn_is_fresh(db, turn: Mapping[str, Any]) -> bool:
    snapshot_sequence = int(turn.get("snapshot_sequence", 0) or 0)
    latest = int(turn.get("latest_conversation_sequence", 0) or 0)
    if latest < snapshot_sequence:
        latest = latest_conversation_sequence(db, turn["conversation_key"])
    return latest <= snapshot_sequence


def admit_provider_send(
    db,
    *,
    turn_id: str,
    worker_id: str,
    human_active: bool = False,
    now: datetime | None = None,
) -> bool:
    """Take the final freshness/ownership fence immediately before HTTP."""
    now = _utc(now) or utc_now()
    turn = db[TURN_COLLECTION].find_one(
        {"_id": turn_id, "kind": KIND_TURN, "state": TURN_PROCESSING,
         "claimed_by": worker_id},
    )
    # The runtime control is re-read at the network boundary.  During a
    # drain, even a previously claimed old-generation turn is fenced out.
    if not turn or not runtime.worker_can_send(db, turn):
        return False
    if human_active or not _turn_is_fresh(db, turn):
        return False
    control = runtime.get_runtime_control(db)
    snapshot_sequence = int(turn.get("snapshot_sequence", 0) or 0)
    result = db[TURN_COLLECTION].update_one(
        {"_id": turn_id, "kind": KIND_TURN, "state": TURN_PROCESSING,
         "claimed_by": worker_id,
         "latest_conversation_sequence": snapshot_sequence,
         "provider_send_admitted_at": None},
        {"$set": {"provider_send_admitted_at": now,
                  "provider_send_admitted_sequence": snapshot_sequence,
                  "provider_send_authorized": True,
                  "provider_send_authorized_mode": control.get("mode"),
                  "provider_send_authorized_generation": int(control.get("generation", 1)),
                  "updated_at": now}},
    )
    return bool(result.modified_count)


def _job_state_for_turn_state(turn_state: str) -> str:
    return {
        TURN_RESPONDED: JOB_RESPONDED,
        TURN_SUPERSEDED: JOB_SUPERSEDED,
        TURN_NO_REPLY: JOB_NO_REPLY,
        TURN_HUMAN_SUPPRESSED: JOB_HUMAN_SUPPRESSED,
        TURN_FAILED_TERMINAL: JOB_FAILED_TERMINAL,
        TURN_DELIVERY_UNKNOWN: JOB_DELIVERY_UNKNOWN,
    }[turn_state]


def finalize_turn(
    db,
    *,
    turn_id: str,
    worker_id: str,
    state: str,
    now: datetime | None = None,
    provider_message_id: str | None = None,
    provider_status: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Finalize only the immutable snapshot, never live/successor jobs."""
    if state not in TERMINAL_TURN_STATES:
        raise ValueError("v2_terminal_turn_state_required")
    now = _utc(now) or utc_now()
    turn = db[TURN_COLLECTION].find_one(
        {"_id": turn_id, "kind": KIND_TURN, "state": TURN_PROCESSING,
         "claimed_by": worker_id},
    )
    if not turn:
        raise TurnOwnershipLost("v2_finalize_ownership_lost")
    snapshot_ids = list(turn.get("snapshot_job_ids") or [])
    if not snapshot_ids:
        raise RuntimeError("v2_empty_snapshot_cannot_finalize")
    if state == TURN_RESPONDED:
        if not provider_message_id or not provider_status:
            raise ValueError("responded_requires_provider_acceptance")
        if not turn.get("provider_send_admitted_at"):
            if not _turn_is_fresh(db, turn):
                raise TurnSuperseded("v2_stale_before_provider_admission")
    update = {
        "state": state,
        "updated_at": now,
        "finalized_at": now,
        "last_error": error,
        "snapshot_finalized_job_ids": list(snapshot_ids),
    }
    if provider_message_id:
        update["provider_message_id"] = provider_message_id
    if provider_status:
        update["provider_status"] = provider_status
    result = db[TURN_COLLECTION].find_one_and_update(
        {"_id": turn_id, "kind": KIND_TURN, "state": TURN_PROCESSING,
         "claimed_by": worker_id, "snapshot_job_ids": snapshot_ids},
        {"$set": update,
         "$unset": {"claimed_by": "", "lease_until": "",
                    "provider_send_admitted_at": ""}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise TurnOwnershipLost("v2_finalize_lease_lost")
    job_state = _job_state_for_turn_state(state)
    db[JOB_COLLECTION].update_many(
        {"kind": KIND_JOB, "_id": {"$in": snapshot_ids},
         "turn_id": turn_id, "state": JOB_PROCESSING},
        {"$set": {"state": job_state, "finalized_turn_id": turn_id,
                  "updated_at": now}},
    )
    return result


def deliver_claimed_turn(
    db,
    *,
    turn: Mapping[str, Any],
    worker_id: str,
    response: str,
    sender: Callable[[str, str], Mapping[str, Any]],
    pre_send_wait: Callable[[], None] | None = None,
    human_active: Callable[[], bool] | bool = False,
    now: datetime | None = None,
) -> DeliveryResult:
    """Deliver one claimed turn; freshness is checked after all waits."""
    if pre_send_wait:
        pre_send_wait()
    human_now = human_active() if callable(human_active) else bool(human_active)
    if human_now:
        finalize_turn(db, turn_id=turn["_id"], worker_id=worker_id,
                      state=TURN_HUMAN_SUPPRESSED, now=now,
                      error="human_takeover_before_provider")
        return DeliveryResult(state=TURN_HUMAN_SUPPRESSED, human_suppressed=True)
    # An empty response is the explicit contract used by ACK/closing and
    # other no-reply policies.  It is not a provider-facing message and must
    # be terminalized before the freshness/provider fence, otherwise a caller
    # can accidentally invoke the sender with an empty payload.
    if not str(response or "").strip():
        finalize_turn(db, turn_id=turn["_id"], worker_id=worker_id,
                      state=TURN_NO_REPLY, now=now,
                      error="no_reply_policy_empty_response")
        return DeliveryResult(state=TURN_NO_REPLY)
    if not runtime.worker_can_send(db, turn):
        # A cutover fences the old generation before any provider call.  Keep
        # the inbound un-responded and explicitly mark why this turn stopped;
        # a cutover coordinator may migrate it only when no attempt/token/id
        # exists (see runtime.migration_decision).
        fence = runtime.runtime_fence(db, turn, phase="send")
        db[TURN_COLLECTION].update_one(
            {"_id": turn["_id"], "kind": KIND_TURN, "state": TURN_PROCESSING,
             "claimed_by": worker_id},
            {"$set": {
                "fenced_out": True,
                "fenced_out_at": now or utc_now(),
                "fenced_out_mode": fence.get("current_mode"),
                "fenced_out_generation": fence.get("current_generation"),
                "last_error": "FENCED_OUT_RUNTIME_GENERATION",
            }},
        )
        finalize_turn(db, turn_id=turn["_id"], worker_id=worker_id,
                      state=TURN_SUPERSEDED, now=now,
                      error="FENCED_OUT_RUNTIME_GENERATION")
        return DeliveryResult(state=TURN_SUPERSEDED, superseded=True,
                              fenced_out=True)
    if not admit_provider_send(db, turn_id=turn["_id"], worker_id=worker_id,
                               now=now):
        finalize_turn(db, turn_id=turn["_id"], worker_id=worker_id,
                      state=TURN_SUPERSEDED, now=now,
                      error="new_inbound_before_provider")
        return DeliveryResult(state=TURN_SUPERSEDED, superseded=True)
    receipt = dict(sender(turn["phone"], response) or {})
    provider_id = str(receipt.get("provider_message_id") or "").strip() or None
    if receipt.get("provider_call_uncertain"):
        finalize_turn(db, turn_id=turn["_id"], worker_id=worker_id,
                      state=TURN_DELIVERY_UNKNOWN, now=now,
                      error="provider_call_uncertain")
        return DeliveryResult(state=TURN_DELIVERY_UNKNOWN)
    if receipt.get("success") and provider_id:
        status = str(receipt.get("delivery_status") or "accepted")
        finalize_turn(db, turn_id=turn["_id"], worker_id=worker_id,
                      state=TURN_RESPONDED, now=now,
                      provider_message_id=provider_id,
                      provider_status=status)
        return DeliveryResult(state=TURN_RESPONDED, provider_message_id=provider_id,
                              provider_status=status, sent=True)
    finalize_turn(db, turn_id=turn["_id"], worker_id=worker_id,
                  state=TURN_FAILED_TERMINAL, now=now,
                  error=str(receipt.get("error") or "provider_rejected"))
    return DeliveryResult(state=TURN_FAILED_TERMINAL)


def migrate_legacy_batch_to_v2(db, *, batch_id: str, now: datetime | None = None,
                               fenced_before_provider: bool = False) -> dict[str, Any]:
    """Move only a definitely unsent V1 batch into the V2 pending queue.

    This is intentionally a narrow cutover operation, not a general retry.
    A provider id, accepted token, or uncertain attempt blocks migration.  A
    ``started`` marker written by the V1 bookkeeping path is ignored only when
    the caller proves that the runtime fence ran before the provider call.
    """
    now = _utc(now) or utc_now()
    legacy_collection = "chatbot_inbound_jobs"
    batch = db[legacy_collection].find_one({"_id": batch_id, "kind": "response_batch"})
    if not batch:
        return {"migrated": False, "reason": "batch_not_found", "job_ids": []}
    decision_input = dict(batch)
    if fenced_before_provider:
        decision_input["delivery_attempts"] = [
            attempt for attempt in (batch.get("delivery_attempts") or [])
            if str(attempt.get("status") or "").lower() not in {"started", "fenced_out"}
        ]
    decision = runtime.migration_decision(decision_input)
    control = runtime.get_runtime_control(db)
    if control.get("mode") not in {runtime.DRAINING_TO_V2, runtime.V2_ACTIVE}:
        return {"migrated": False, "reason": "v2_not_target", "job_ids": [],
                "decision": decision}
    if not decision["eligible"]:
        return {"migrated": False, "reason": decision["reason"], "job_ids": [],
                "decision": decision}
    migrated = []
    for legacy_job_id in batch.get("job_ids") or []:
        job = db[legacy_collection].find_one({"_id": legacy_job_id, "kind": "inbound_job"})
        if not job or not job.get("inbound_provider_message_id"):
            continue
        result = persist_inbound(
            db,
            provider_message_id=str(job["inbound_provider_message_id"]),
            conversation_id=job.get("conversation_id"),
            lead_id=job.get("lead_id"),
            phone=job.get("phone") or batch.get("phone"),
            text=job.get("text") or "",
            received_at=job.get("received_at"),
            quiet_seconds=0,
        )
        db[JOB_COLLECTION].update_one(
            {"_id": result.job_id},
            {"$set": {"migrated_from_v1_job_id": legacy_job_id,
                      "migrated_from_v1_batch_id": batch_id,
                      "migration_reason": "runtime_generation_fenced_out",
                      "updated_at": now}},
        )
        db[legacy_collection].update_one(
            {"_id": legacy_job_id, "kind": "inbound_job"},
            {"$set": {"migration_target_job_id": result.job_id,
                      "migration_status": "migrated_to_v2",
                      "updated_at": now}},
        )
        migrated.append(result.job_id)
    return {"migrated": bool(migrated), "reason": "migrated_to_v2" if migrated else "no_jobs",
            "job_ids": migrated, "decision": decision}


def choose_response(
    *,
    deterministic_renderer: Callable[[], str],
    optional_writer: Callable[[], str] | None = None,
) -> tuple[str, str]:
    """Use a writer once when present; an empty writer falls back locally."""
    if optional_writer is not None:
        try:
            candidate = str(optional_writer() or "").strip()
        except Exception:
            candidate = ""
        if candidate:
            return candidate, "optional_writer"
    deterministic = str(deterministic_renderer() or "").strip()
    if not deterministic:
        raise ValueError("deterministic_renderer_returned_empty")
    return deterministic, "deterministic_fallback"


def record_rag_candidates(db, *, conversation_key: str, candidates: Iterable[str], now=None) -> None:
    """Persist search candidates without marking them as customer-presented."""
    now = _utc(now) or utc_now()
    values = [str(value) for value in candidates if str(value).strip()]
    db[STATE_COLLECTION].update_one(
        {"conversation_key": conversation_key},
        {"$set": {"conversation_key": conversation_key,
                  "rag_candidates": values, "updated_at": now},
         "$setOnInsert": {"created_at": now, "presented_properties": []}},
        upsert=True,
    )


def mark_property_presented_after_acceptance(
    db,
    *,
    conversation_key: str,
    turn_id: str,
    property_id: str,
    provider_message_id: str | None,
    provider_status: str | None,
    now=None,
) -> bool:
    """Mark a property only after an accepted outbound has an id."""
    if not provider_message_id or provider_status not in {"accepted", "sent", "delivered", "read"}:
        return False
    now = _utc(now) or utc_now()
    event = {
        "_id": f"presentation:{uuid.uuid4()}",
        "conversation_key": conversation_key,
        "turn_id": turn_id,
        "property_id": str(property_id),
        "provider_message_id": str(provider_message_id),
        "provider_status": provider_status,
        "created_at": now,
        "accepted_evidence": True,
    }
    try:
        db[PRESENTATION_COLLECTION].insert_one(event)
    except DuplicateKeyError:
        return False
    db[STATE_COLLECTION].update_one(
        {"conversation_key": conversation_key},
        {"$setOnInsert": {"conversation_key": conversation_key,
                          "created_at": now, "rag_candidates": []},
         "$addToSet": {"presented_properties": str(property_id)},
         "$set": {"updated_at": now}},
        upsert=True,
    )
    return True


def audit_invariants(db) -> dict[str, int | str]:
    """Return fail-closed v2 invariants for health and test assertions."""
    turns = {
        turn["_id"]: turn
        for turn in db[TURN_COLLECTION].find({"kind": KIND_TURN})
    }
    orphaned = 0
    responded_not_in_snapshot = 0
    active_with_terminal_parent = 0
    stale_sent = 0
    old_generation_provider_send = 0
    for job in db[JOB_COLLECTION].find({"kind": KIND_JOB}):
        turn = turns.get(job.get("turn_id"))
        if not turn:
            orphaned += 1
            continue
        snapshot_ids = set(turn.get("snapshot_job_ids") or [])
        if job.get("state") == JOB_RESPONDED and job["_id"] not in snapshot_ids:
            responded_not_in_snapshot += 1
        if job.get("state") not in TERMINAL_JOB_STATES and turn.get("state") in TERMINAL_TURN_STATES:
            active_with_terminal_parent += 1
    for turn in turns.values():
        if turn.get("provider_message_id"):
            authorized_mode = turn.get("provider_send_authorized_mode")
            authorized_generation = turn.get("provider_send_authorized_generation")
            expected_mode = runtime.V1_ACTIVE if turn.get("pipeline_version") == runtime.V1 else runtime.V2_ACTIVE
            if (
                not turn.get("provider_send_authorized")
                or authorized_mode != expected_mode
                or authorized_generation is None
            ):
                old_generation_provider_send += 1
        if turn.get("provider_message_id") and int(turn.get("latest_conversation_sequence", 0) or 0) > int(turn.get("snapshot_sequence", 0) or 0):
            # A later inbound after provider admission is valid; only count a
            # send that was admitted after the turn was already stale.
            admitted_seq = turn.get("provider_send_admitted_sequence")
            if admitted_seq is None or int(admitted_seq) != int(turn.get("snapshot_sequence", 0) or 0):
                stale_sent += 1
    accepted_presentations = {
        (event.get("turn_id"), event.get("property_id"), event.get("provider_message_id"))
        for event in db[PRESENTATION_COLLECTION].find({"accepted_evidence": True})
        if event.get("provider_status") in {"accepted", "sent", "delivered", "read"}
    }
    false_rag_presented = 0
    for state in db[STATE_COLLECTION].find({}):
        for property_id in state.get("presented_properties") or []:
            if not any(item[1] == str(property_id) for item in accepted_presentations):
                false_rag_presented += 1
    return {
        "ORPHANED_JOBS": orphaned,
        "RESPONDED_NOT_IN_DELIVERED_SNAPSHOT": responded_not_in_snapshot,
        "ACTIVE_JOBS_WITH_TERMINAL_PARENT": active_with_terminal_parent,
        "STALE_RESPONSES_SENT": stale_sent,
        "OLD_GENERATION_PROVIDER_SEND": old_generation_provider_send,
        "DOUBLE_OWNERSHIP_VIOLATIONS": responded_not_in_snapshot + orphaned,
        "FENCED_OUT_TURNS": sum(1 for turn in turns.values() if turn.get("fenced_out")),
        "RAG_FOUND_BUT_FALSELY_MARKED_PRESENTED": false_rag_presented,
        "CHATBOT_HEALTH": "DEGRADED" if any((orphaned, responded_not_in_snapshot,
                                             active_with_terminal_parent, stale_sent,
                                             false_rag_presented)) else "HEALTHY",
    }


def queue_snapshot(db, *, conversation_key: str | None = None) -> dict[str, int]:
    """Small read-only operational snapshot for the v2 validation harness."""
    query = {"kind": KIND_TURN}
    if conversation_key:
        query["conversation_key"] = conversation_key
    return {
        "pending_turns": db[TURN_COLLECTION].count_documents({**query, "state": TURN_PENDING}),
        "processing_turns": db[TURN_COLLECTION].count_documents({**query, "state": TURN_PROCESSING}),
        "responded_turns": db[TURN_COLLECTION].count_documents({**query, "state": TURN_RESPONDED}),
        "superseded_turns": db[TURN_COLLECTION].count_documents({**query, "state": TURN_SUPERSEDED}),
        "inbound_jobs": db[JOB_COLLECTION].count_documents({"kind": KIND_JOB, **({"conversation_key": conversation_key} if conversation_key else {})}),
    }


def timed_call(func: Callable[[], Any]) -> tuple[Any, float]:
    """Measure a local stage without introducing any network dependency."""
    started = time.perf_counter()
    result = func()
    return result, (time.perf_counter() - started) * 1000.0
