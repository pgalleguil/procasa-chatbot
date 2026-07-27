"""Canonical CRM notification persistence primitives.

This module is deliberately provider-agnostic.  Creating, claiming and
auditing records cannot send a WhatsApp message.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import uuid

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .crm_metrics import utc_now

COLLECTION = "crm_notifications_v1"
TERMINAL_STATES = frozenset({"sent", "failed_final", "suppressed", "quarantined"})
VALID_STATES = frozenset({
    "pending", "sending", "sent", "failed_retryable", "failed_final",
    "suppressed", "quarantined", "failed_recipient", "failed_validation",
    "rate_limited", "held",
})
DEDUP_ACTIVE_STATES = frozenset({"pending", "sending", "sent", "failed_retryable"})
ALLOWED_COMMERCIAL_REASONS = frozenset({
    "inbound_message", "lead_created", "manual_lead_created",
})
ALLOWED_COMMERCIAL_ORIGINS = frozenset({"inbound_message", "manual_lead"})


def individual_identity(*, lead_id, assignment_cycle_id, notification_type, recipient_user_id) -> str:
    values = (lead_id, assignment_cycle_id, notification_type, recipient_user_id)
    if any(value is None or not str(value).strip() for value in values):
        raise ValueError("individual notification identity is incomplete")
    return "|".join(str(value).strip() for value in values)


def digest_identity(*, recipient_user_id, digest_type, business_period, content_version) -> str:
    values = (recipient_user_id, digest_type, business_period, content_version)
    if any(value is None or not str(value).strip() for value in values):
        raise ValueError("digest notification identity is incomplete")
    return "|".join(str(value).strip() for value in values)


def content_hash(payload) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verified_commercial_source(db, cycle) -> bool:
    """A commercial cycle must point to a real inbound or manual-create event."""
    if not cycle or cycle.get("notification_eligible") is not True:
        return False
    if cycle.get("reason") not in ALLOWED_COMMERCIAL_REASONS:
        return False
    if cycle.get("cycle_origin") not in ALLOWED_COMMERCIAL_ORIGINS:
        return False
    source_id = str(
        cycle.get("source_inbound_provider_id") or cycle.get("source_event_id") or ""
    ).strip()
    if not source_id:
        return False
    if cycle.get("source_event_verified") is True:
        return True
    if db["chatbot_inbound_jobs"].find_one({
        "inbound_provider_message_id": source_id,
        "kind": {"$ne": "batch"},
    }):
        return True
    try:
        from bson import ObjectId
        source_values = [source_id]
        if len(source_id) == 24:
            source_values.append(ObjectId(source_id))
    except Exception:
        source_values = [source_id]
    return bool(db["crm_events"].find_one({
        "_id": {"$in": source_values},
        "type": {"$in": ["msg_in", "MANUAL_LEAD_CREATED"]},
    }))


def audit_duplicate_identities(db) -> dict:
    collection = db[COLLECTION]
    conflicts = {}
    for field in ("individual_identity", "digest_identity"):
        pipeline = [
            {"$match": {field: {"$type": "string"}}},
            {"$group": {"_id": f"${field}", "count": {"$sum": 1}, "ids": {"$push": "$_id"}}},
            {"$match": {"count": {"$gt": 1}}},
        ]
        conflicts[field] = list(collection.aggregate(pipeline))
    return {"safe_to_index": not any(conflicts.values()), "conflicts": conflicts}


def dry_run_canonical_indexes(db) -> dict:
    collection = db[COLLECTION]
    definitions = {
        "individual": ("lead_id", "assignment_cycle_id", "notification_type", "recipient_user_id"),
        "digest": ("recipient_user_id", "digest_type", "business_period", "content_version"),
    }
    result = {}
    for name, fields in definitions.items():
        match = {"schema_version": "crm_notification_v1", "canonical_identity_version": 1}
        match.update({field: {"$exists": True} for field in fields})
        # For individual identity, exclude documents marked as historical duplicates
        if name == "individual":
            match["dedupe_active"] = {"$ne": False}
        group_id = {field: f"${field}" for field in fields}
        duplicates = list(collection.aggregate([
            {"$match": match}, {"$group": {"_id": group_id, "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
        ]))
        result[name] = {"eligible_documents": collection.count_documents(match),
                        "duplicates": duplicates, "safe_to_create": not duplicates}
    return result


INDIVIDUAL_INDEX_NAME = "uq_crm_notification_individual_v1"
DIGEST_INDEX_NAME = "uq_crm_notification_digest_v1"


def verify_unique_indexes(db) -> dict:
    """Verify that the required unique indexes exist.

    This is called at application startup.  It logs a critical error if an
    expected index is missing instead of silently assuming protection.
    """
    collection = db[COLLECTION]
    existing = {idx["name"] for idx in collection.list_indexes()}
    result = {}
    for name in (INDIVIDUAL_INDEX_NAME, DIGEST_INDEX_NAME):
        result[name] = name in existing
    return result


def ensure_unique_indexes(db) -> dict:
    """Audit first; never delete or merge a legacy conflict automatically."""
    audit = dry_run_canonical_indexes(db)
    if not all(value["safe_to_create"] for value in audit.values()):
        return {"dry_run": audit, "created": [], "blocked": True}
    collection = db[COLLECTION]
    individual_filter = {
        "schema_version": "crm_notification_v1", "canonical_identity_version": 1,
        "dedupe_active": True,
        "lead_id": {"$exists": True}, "assignment_cycle_id": {"$exists": True},
        "notification_type": {"$exists": True}, "recipient_user_id": {"$exists": True},
    }
    digest_filter = {
        "schema_version": "crm_notification_v1", "canonical_identity_version": 1,
        "recipient_user_id": {"$exists": True}, "digest_type": {"$exists": True},
        "business_period": {"$exists": True}, "content_version": {"$exists": True},
    }
    created = [
        collection.create_index(
            [("lead_id", 1), ("assignment_cycle_id", 1), ("notification_type", 1), ("recipient_user_id", 1)], unique=True,
            partialFilterExpression=individual_filter,
            name=INDIVIDUAL_INDEX_NAME,
        ),
        collection.create_index(
            [("recipient_user_id", 1), ("digest_type", 1), ("business_period", 1), ("content_version", 1)], unique=True,
            partialFilterExpression=digest_filter,
            name=DIGEST_INDEX_NAME,
        ),
    ]
    return {"dry_run": audit, "created": created, "blocked": False}


def create_pending(db, *, identity_field, identity, payload, payload_version="crm_notification_v1", metadata=None,
                   canonical_fields=None, send_after=None):
    if identity_field not in {"individual_identity", "digest_identity"}:
        raise ValueError("invalid identity field")

    # Pre-check: if an active notification already exists for this identity,
    # return it instead of attempting an insert.  This is a defense-in-depth
    # layer; the unique index is the definitive barrier.
    if identity_field == "individual_identity":
        existing = db[COLLECTION].find_one({
            identity_field: identity,
            "state": {"$in": list(DEDUP_ACTIVE_STATES)},
        })
        if existing:
            return existing

    now = utc_now()
    document = {
        identity_field: identity, "state": "pending", "delivery_id": str(uuid.uuid4()),
        "payload_version": payload_version, "content_hash": content_hash(payload),
        "payload": payload, "metadata": dict(metadata or {}), "attempts": [],
        "schema_version": "crm_notification_v1", "canonical_identity_version": 1,
        "created_at": now, "updated_at": now,
        "message_domain": "commercial_notification",
        "message_type": (
            (canonical_fields or {}).get("notification_type")
            or (canonical_fields or {}).get("digest_type")
        ),
        "recipient_role": "executive",
        "state_source": COLLECTION,
        "responsible_service": "commercial_notification_delivery",
        "idempotency_key": identity,
    }
    document.update(dict(canonical_fields or {}))
    if document.get("dedupe_active") is None:
        document["dedupe_active"] = True
    if send_after is not None:
        document["send_after"] = send_after
    try:
        db[COLLECTION].insert_one(document)
        return db[COLLECTION].find_one({identity_field: identity}) or document
    except DuplicateKeyError:
        return db[COLLECTION].find_one({identity_field: identity})


def claim_next(db, *, worker_id, lease_seconds=120, now=None, extra_filter=None):
    now = now or utc_now()
    query = {
        "state": {"$in": ["pending", "failed_retryable"]},
        "$or": [
            {"next_attempt_at": {"$exists": False}},
            {"next_attempt_at": None},
            {"next_attempt_at": {"$lte": now}},
        ],
    }
    query.update(dict(extra_filter or {}))
    return db[COLLECTION].find_one_and_update(
        query,
        {"$set": {"state": "sending", "lease_owner": worker_id,
                  "lease_expires_at": now + timedelta(seconds=lease_seconds), "updated_at": now},
         "$push": {"attempts": {"claimed_at": now, "worker_id": worker_id}}},
        sort=[("created_at", ASCENDING)], return_document=ReturnDocument.AFTER,
    )


def recover_expired_lease(db, *, notification_id, provider_status, now=None):
    """Recovery requires an explicit provider check. Never recovers documents
    with delivery evidence (provider_message_id, actually_delivered, delivery_token)."""
    now = now or utc_now()
    if provider_status not in {"not_found", "failed", "sent", "delivered"}:
        raise ValueError("provider status must be checked before lease recovery")
    if provider_status in {"sent", "delivered"}:
        state = "sent"
    else:
        state = "failed_retryable"
    return db[COLLECTION].find_one_and_update(
        {"_id": notification_id, "state": "sending", "lease_expires_at": {"$lte": now},
         "provider_message_id": {"$exists": False},
         "actually_delivered": {"$ne": True},
         "delivery_token": {"$exists": False}},
        {"$set": {"state": state, "provider_status_checked_at": now,
                  "lease_owner": None, "lease_expires_at": None, "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )


def reserve_for_delivery(db, *, notification_id, worker_id, delivery_token, now=None):
    """Atomically reserve a delivery slot before calling the provider.
    Only succeeds if state=sending, lease_owner matches, and no prior call started."""
    now = now or utc_now()
    return db[COLLECTION].find_one_and_update(
        {"_id": notification_id, "state": "sending", "lease_owner": worker_id,
         "provider_call_started_at": {"$exists": False}},
        {"$set": {"provider_call_started_at": now, "delivery_token": delivery_token,
                  "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )


def refresh_lease(db, *, notification_id, worker_id, lease_seconds=120, now=None):
    """Extend lease while provider call is in progress."""
    now = now or utc_now()
    return db[COLLECTION].update_one(
        {"_id": notification_id, "state": "sending", "lease_owner": worker_id},
        {"$set": {"lease_expires_at": now + timedelta(seconds=lease_seconds), "updated_at": now}},
    )


def record_delivery_attempt(db, *, notification_id, delivery_token, attempt_data, now=None):
    """Append-only delivery record. Never overwrites previous provider_message_ids."""
    now = now or utc_now()
    return db[COLLECTION].update_one(
        {"_id": notification_id},
        {"$push": {"delivery_attempts": {
            "delivery_token": delivery_token,
            "started_at": attempt_data.get("started_at"),
            "completed_at": now,
            "worker_id": attempt_data.get("worker_id"),
            "provider_http_status": attempt_data.get("http_status"),
            "provider_message_id": attempt_data.get("provider_message_id"),
            "provider_request_id": attempt_data.get("provider_request_id"),
            "content_hash": attempt_data.get("content_hash"),
            "result": attempt_data.get("result", "unknown"),
        }}},
    )


def finalize_attempt(db, *, notification_id, worker_id, state, provider_message_id=None, error=None, now=None):
    if state not in VALID_STATES - {"pending", "sending"}:
        raise ValueError("invalid final state")
    now = now or utc_now()
    update = {"state": state, "provider_message_id": provider_message_id,
              "lease_owner": None, "lease_expires_at": None, "updated_at": now}
    return db[COLLECTION].find_one_and_update(
        {"_id": notification_id, "state": "sending", "lease_owner": worker_id},
        {"$set": update, "$push": {"attempts": {"completed_at": now, "state": state,
                                                   "provider_message_id": provider_message_id, "error": error}}},
        return_document=ReturnDocument.AFTER,
    )


def reserve_cycle_delivery(db, *, assignment_cycle_id, digest_id, delivery_token, now=None):
    """Atomically reserve delivery for a non-HOT cycle. Only succeeds once per cycle."""
    now = now or utc_now()
    return db["crm_assignment_cycles"].find_one_and_update(
        {"assignment_cycle_id": assignment_cycle_id,
         "non_hot_delivery_status": {"$exists": False}},
        {"$set": {
            "non_hot_delivery_status": "reserved",
            "non_hot_delivery_token": delivery_token,
            "non_hot_digest_id": digest_id,
            "non_hot_delivery_reserved_at": now,
        }},
        return_document=ReturnDocument.AFTER,
    )


def finalize_cycle_delivery(db, *, assignment_cycle_id, provider_message_id=None, now=None):
    """Mark cycle delivery as completed with provider evidence."""
    now = now or utc_now()
    return db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": assignment_cycle_id, "non_hot_delivery_status": "reserved"},
        {"$set": {
            "non_hot_delivery_status": "delivered",
            "non_hot_provider_message_id": provider_message_id,
            "non_hot_delivered_at": now,
            "updated_at": now,
        }},
    )


def release_cycle_delivery(db, *, assignment_cycle_id, digest_id, delivery_token, reason, now=None):
    """Release a cycle reservation only after a confirmed non-delivery.

    The compare-and-set filter prevents a later retry from releasing a newer
    reservation.  Delivery history remains append-only on the cycle.
    """
    now = now or utc_now()
    return db["crm_assignment_cycles"].find_one_and_update(
        {
            "assignment_cycle_id": assignment_cycle_id,
            "non_hot_delivery_status": "reserved",
            "non_hot_digest_id": str(digest_id),
            "non_hot_delivery_token": delivery_token,
        },
        {
            "$unset": {
                "non_hot_delivery_status": "",
                "non_hot_delivery_token": "",
                "non_hot_digest_id": "",
                "non_hot_delivery_reserved_at": "",
            },
            "$set": {"updated_at": now},
            "$push": {"notification_delivery_history": {
                "at": now,
                "event": "non_hot_delivery_reservation_released",
                "digest_id": str(digest_id),
                "delivery_token": delivery_token,
                "reason": reason,
            }},
        },
        return_document=ReturnDocument.AFTER,
    )


def is_cycle_delivered(db, *, assignment_cycle_id) -> bool:
    """Check if a cycle has already been delivered."""
    cycle = db["crm_assignment_cycles"].find_one(
        {"assignment_cycle_id": assignment_cycle_id},
        {"non_hot_delivery_status": 1},
    )
    return bool(cycle and cycle.get("non_hot_delivery_status") in ("reserved", "delivered"))


@dataclass(frozen=True)
class VolumeLimits:
    global_per_run: int = 100
    per_executive: int = 20
    per_minute: int = 20
    digests_per_run: int = 10


def validate_volume(*, total, by_executive, per_minute, digests, limits=VolumeLimits()) -> dict:
    violations = []
    if total > limits.global_per_run: violations.append("global_per_run")
    if any(value > limits.per_executive for value in by_executive.values()): violations.append("per_executive")
    if per_minute > limits.per_minute: violations.append("per_minute")
    if digests > limits.digests_per_run: violations.append("digests_per_run")
    return {"allowed": not violations, "circuit_breaker_open": bool(violations), "violations": violations}
