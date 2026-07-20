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
    "suppressed", "quarantined",
})


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
        group_id = {field: f"${field}" for field in fields}
        duplicates = list(collection.aggregate([
            {"$match": match}, {"$group": {"_id": group_id, "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
        ]))
        result[name] = {"eligible_documents": collection.count_documents(match),
                        "duplicates": duplicates, "safe_to_create": not duplicates}
    return result


def ensure_unique_indexes(db) -> dict:
    """Audit first; never delete or merge a legacy conflict automatically."""
    audit = dry_run_canonical_indexes(db)
    if not all(value["safe_to_create"] for value in audit.values()):
        return {"dry_run": audit, "created": [], "blocked": True}
    collection = db[COLLECTION]
    individual_filter = {
        "schema_version": "crm_notification_v1", "canonical_identity_version": 1,
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
            name="uq_crm_notification_individual_v1",
        ),
        collection.create_index(
            [("recipient_user_id", 1), ("digest_type", 1), ("business_period", 1), ("content_version", 1)], unique=True,
            partialFilterExpression=digest_filter,
            name="uq_crm_notification_digest_v1",
        ),
    ]
    return {"dry_run": audit, "created": created, "blocked": False}


def create_pending(db, *, identity_field, identity, payload, payload_version="crm_notification_v1", metadata=None,
                   canonical_fields=None, send_after=None):
    if identity_field not in {"individual_identity", "digest_identity"}:
        raise ValueError("invalid identity field")
    now = utc_now()
    document = {
        identity_field: identity, "state": "pending", "delivery_id": str(uuid.uuid4()),
        "payload_version": payload_version, "content_hash": content_hash(payload),
        "payload": payload, "metadata": dict(metadata or {}), "attempts": [],
        "schema_version": "crm_notification_v1", "canonical_identity_version": 1,
        "created_at": now, "updated_at": now,
    }
    document.update(dict(canonical_fields or {}))
    if send_after is not None:
        document["send_after"] = send_after
    try:
        db[COLLECTION].insert_one(document)
        return db[COLLECTION].find_one({identity_field: identity}) or document
    except DuplicateKeyError:
        return db[COLLECTION].find_one({identity_field: identity})


def claim_next(db, *, worker_id, lease_seconds=120, now=None, extra_filter=None):
    now = now or utc_now()
    query = {"state": {"$in": ["pending", "failed_retryable"]}}
    query.update(dict(extra_filter or {}))
    return db[COLLECTION].find_one_and_update(
        query,
        {"$set": {"state": "sending", "lease_owner": worker_id,
                  "lease_expires_at": now + timedelta(seconds=lease_seconds), "updated_at": now},
         "$push": {"attempts": {"claimed_at": now, "worker_id": worker_id}}},
        sort=[("created_at", ASCENDING)], return_document=ReturnDocument.AFTER,
    )


def recover_expired_lease(db, *, notification_id, provider_status, now=None):
    """Recovery requires an explicit provider check and never creates a record."""
    now = now or utc_now()
    if provider_status not in {"not_found", "failed", "sent", "delivered"}:
        raise ValueError("provider status must be checked before lease recovery")
    if provider_status in {"sent", "delivered"}:
        state = "sent"
    else:
        state = "failed_retryable"
    return db[COLLECTION].find_one_and_update(
        {"_id": notification_id, "state": "sending", "lease_expires_at": {"$lte": now}},
        {"$set": {"state": state, "provider_status_checked_at": now,
                  "lease_owner": None, "lease_expires_at": None, "updated_at": now}},
        return_document=ReturnDocument.AFTER,
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
