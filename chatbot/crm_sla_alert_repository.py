"""CRM SLA Alert Repository — exclusive persistence for crm_sla_alerts_v1."""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from .crm_metrics import utc_now
from .crm_sla_alert_settings import (
    CRM_SLA_ALERTS_LEASE_SECONDS, CRM_SLA_ALERTS_MAX_ATTEMPTS,
)
from .crm_sla_alert_templates import MESSAGE_DOMAIN

logger = logging.getLogger(__name__)

COLLECTION = "crm_sla_alerts_v1"

ST_PENDING = "pending"
ST_PROCESSING = "processing"
ST_SENT = "sent"
ST_FAILED_RETRYABLE = "failed_retryable"
ST_FAILED_FINAL = "failed_final"
ST_CANCELLED = "cancelled"
ST_DELIVERY_UNCERTAIN = "delivery_uncertain"

TERMINAL = frozenset({ST_SENT, ST_FAILED_FINAL, ST_CANCELLED, ST_DELIVERY_UNCERTAIN})
CLAIMABLE = frozenset({ST_PENDING, ST_FAILED_RETRYABLE})
NON_RECLAIMABLE = frozenset({ST_SENT, ST_CANCELLED, ST_FAILED_FINAL, ST_DELIVERY_UNCERTAIN})

# Transitions observed in the actual flow (claim happens before cancel,
# so cancel only applies to processing, not pending directly):
#   pending → processing  (claim)
#   failed_retryable → processing  (claim)
#   processing (stale, no delivery) → processing  (recovery claim)
#   processing → cancelled  (revalidation)
#   processing → sent  (confirmed)
#   processing → failed_retryable  (rejected)
#   processing → failed_final  (max attempts)
#   processing → delivery_uncertain  (ambiguous / timeout / crash after start)
#   processing (stale, with delivery) → delivery_uncertain  (quarantine)
TRANSITIONS: dict[str, frozenset[str]] = {
    ST_PENDING: frozenset({ST_PROCESSING}),
    ST_FAILED_RETRYABLE: frozenset({ST_PROCESSING}),
    ST_PROCESSING: frozenset({ST_SENT, ST_FAILED_RETRYABLE, ST_FAILED_FINAL,
                               ST_CANCELLED, ST_DELIVERY_UNCERTAIN}),
    ST_SENT: frozenset(),
    ST_CANCELLED: frozenset(),
    ST_FAILED_FINAL: frozenset(),
    ST_DELIVERY_UNCERTAIN: frozenset(),
}


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

async def ensure_crm_sla_alert_indexes(db):
    collection = db[COLLECTION]
    names = set()
    async for idx in collection.list_indexes():
        names.add(idx["name"])
    if "uq_sla_alert_identity" not in names:
        await collection.create_index(
            [("message_domain", ASCENDING), ("assignment_cycle_id", ASCENDING),
             ("alert_level", ASCENDING), ("recipient_user_id", ASCENDING)],
            unique=True, name="uq_sla_alert_identity",
        )
    if "ix_sla_claim" not in names:
        await collection.create_index(
            [("state", ASCENDING), ("next_attempt_at", ASCENDING), ("created_at", ASCENDING)],
            name="ix_sla_claim",
        )
    if "ix_sla_lease" not in names:
        await collection.create_index(
            [("lease_expires_at", ASCENDING)], name="ix_sla_lease", sparse=True,
        )
    return {"created": [], "collection": COLLECTION}


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------

async def persist_candidate(db, candidate: dict) -> dict:
    now = utc_now()
    deadline_at = candidate.get("deadline_dt")
    if deadline_at is None or not hasattr(deadline_at, "tzinfo") or deadline_at.tzinfo is None:
        raise ValueError("deadline_dt must be a timezone-aware UTC datetime")

    doc = {
        "_id": candidate.get("idempotency_dedup_key"),
        "message_domain": MESSAGE_DOMAIN,
        "assignment_cycle_id": candidate["assignment_cycle_id"],
        "lead_id": candidate["lead_id"],
        "recipient_user_id": candidate["recipient_user_id"],
        "recipient_phone_snapshot": candidate.get("executive_phone"),
        "alert_level": candidate["alert_level"],
        "sla_profile": candidate["sla_profile"],
        "outreach_state": candidate.get("outreach_state"),
        "elapsed_business_minutes": candidate["elapsed_business_minutes"],
        "deadline_at": deadline_at,
        "rendered_message": candidate["message"],
        "lead_url": candidate.get("lead_url"),
        "state": ST_PENDING,
        "attempt_count": 0,
        "lease_owner": None, "lease_expires_at": None,
        "delivery_attempt_id": None, "delivery_started_at": None,
        "delivery_completed_at": None, "delivery_outcome": None,
        "provider_message_id": None,
        "next_attempt_at": now,
        "created_at": now, "updated_at": now,
        "claimed_at": None, "sent_at": None,
        "cancelled_at": None, "cancellation_reason": None,
        "last_error": None,
    }
    try:
        await db[COLLECTION].insert_one(doc)
        return {"status": "created", "doc": doc}
    except DuplicateKeyError:
        return {"status": "already_exists", "doc": None}


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------

async def claim_next_alert(db, *, worker_id: str, now=None) -> dict | None:
    current = now or utc_now()
    lease_end = current + timedelta(seconds=CRM_SLA_ALERTS_LEASE_SECONDS)

    # Phase 1: pending / failed_retryable with expired next_attempt_at
    query = {
        "message_domain": MESSAGE_DOMAIN,
        "state": {"$in": list(CLAIMABLE)},
        "next_attempt_at": {"$lte": current},
    }
    doc = await db[COLLECTION].find_one_and_update(
        query,
        {"$set": {"state": ST_PROCESSING, "lease_owner": worker_id,
                   "lease_expires_at": lease_end, "claimed_at": current, "updated_at": current},
         "$inc": {"attempt_count": 1}},
        sort=[("deadline_at", ASCENDING), ("created_at", ASCENDING)],
    )
    if doc:
        return doc

    # Phase 2: stale processing, no delivery started → recoverable
    query_stale = {
        "message_domain": MESSAGE_DOMAIN,
        "state": ST_PROCESSING,
        "lease_expires_at": {"$lte": current},
        "delivery_started_at": None,
    }
    doc = await db[COLLECTION].find_one_and_update(
        query_stale,
        {"$set": {"lease_owner": worker_id, "lease_expires_at": lease_end,
                   "claimed_at": current, "updated_at": current},
         "$inc": {"attempt_count": 1}},
        sort=[("deadline_at", ASCENDING), ("created_at", ASCENDING)],
    )
    return doc


# ---------------------------------------------------------------------------
# Quarantine — stale processing WITH delivery started (never re-send)
# ---------------------------------------------------------------------------

async def quarantine_stale_started_deliveries(db, *, now=None, limit: int = 20) -> dict:
    """Atomically move at most `limit` stale processing documents that have
    delivery_started_at set to delivery_uncertain.  Each document is updated
    via a compare-and-set filter so a concurrent change is never overwritten.

    Returns {"selected": int, "updated": int, "skipped_race": int}.
    """
    current = now or utc_now()
    base_filter = {
        "message_domain": MESSAGE_DOMAIN,
        "state": ST_PROCESSING,
        "lease_expires_at": {"$lte": current},
        "delivery_started_at": {"$ne": None},
        "delivery_completed_at": None,
    }

    # Select candidates ordered by oldest lease first
    candidates_cursor = db[COLLECTION].find(
        base_filter,
        {"_id": 1},
    ).sort([("lease_expires_at", ASCENDING), ("created_at", ASCENDING)]).limit(limit)
    candidates = await candidates_cursor.to_list(length=limit)

    selected = len(candidates)
    updated = 0
    skipped_race = 0

    for candidate in candidates:
        doc_id = candidate["_id"]
        result = await db[COLLECTION].find_one_and_update(
            {"_id": doc_id, **base_filter},
            {"$set": {
                "state": ST_DELIVERY_UNCERTAIN,
                "delivery_outcome": "crash_after_delivery_start",
                "last_error": "lease_expired_after_delivery_start",
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": current,
            }},
        )
        if result:
            updated += 1
        else:
            skipped_race += 1  # document changed state between select and update

    if updated:
        logger.warning("[SLA_REPO] Quarantined %d/%d stale started deliveries (%d race-skips)",
                       updated, selected, skipped_race)
    return {"selected": selected, "updated": updated, "skipped_race": skipped_race}


# ---------------------------------------------------------------------------
# Delivery start (lease-gated)
# ---------------------------------------------------------------------------

async def mark_delivery_started(db, *, alert_id: str, worker_id: str, now=None) -> dict | None:
    current = now or utc_now()
    attempt_id = str(uuid.uuid4())
    return await db[COLLECTION].find_one_and_update(
        {"_id": alert_id, "state": ST_PROCESSING, "lease_owner": worker_id,
         "lease_expires_at": {"$gt": current}},
        {"$set": {
            "delivery_attempt_id": attempt_id,
            "delivery_started_at": current,
            "updated_at": current,
        }},
    )


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------

async def finalize_alert(
    db, *, alert_id: str, state: str, provider_message_id=None,
    error=None, delivery_outcome=None, delivery_attempt_id=None, now=None,
) -> dict | None:
    current = now or utc_now()
    base = {"_id": alert_id, "state": ST_PROCESSING}
    if delivery_attempt_id:
        base["delivery_attempt_id"] = delivery_attempt_id

    if state == ST_SENT:
        return await db[COLLECTION].find_one_and_update(base, {"$set": {
            "state": ST_SENT, "sent_at": current,
            "provider_message_id": provider_message_id,
            "delivery_completed_at": current, "delivery_outcome": "confirmed",
            "lease_owner": None, "lease_expires_at": None, "updated_at": current,
        }})

    if state == ST_DELIVERY_UNCERTAIN:
        return await db[COLLECTION].find_one_and_update(base, {"$set": {
            "state": ST_DELIVERY_UNCERTAIN, "delivery_completed_at": current,
            "delivery_outcome": delivery_outcome or "unknown",
            "last_error": str(error)[:500] if error else None,
            "lease_owner": None, "lease_expires_at": None, "updated_at": current,
        }})

    if state == ST_FAILED_RETRYABLE:
        alert = await db[COLLECTION].find_one({"_id": alert_id})
        if not alert:
            return None
        if alert.get("attempt_count", 0) >= CRM_SLA_ALERTS_MAX_ATTEMPTS:
            state = ST_FAILED_FINAL

    if state == ST_FAILED_FINAL:
        return await db[COLLECTION].find_one_and_update(base, {"$set": {
            "state": ST_FAILED_FINAL,
            "last_error": str(error)[:500] if error else None,
            "lease_owner": None, "lease_expires_at": None, "updated_at": current,
        }})

    # failed_retryable with exponential backoff
    att = alert.get("attempt_count", 1) if alert else 1
    backoff = min(2 ** (att - 1), 60)
    return await db[COLLECTION].find_one_and_update(base, {"$set": {
        "state": ST_FAILED_RETRYABLE,
        "last_error": str(error)[:500] if error else None,
        "next_attempt_at": current + timedelta(minutes=backoff),
        "lease_owner": None, "lease_expires_at": None, "updated_at": current,
    }})


async def cancel_alert(db, *, alert_id: str, reason: str, now=None) -> dict | None:
    current = now or utc_now()
    return await db[COLLECTION].find_one_and_update(
        {"_id": alert_id, "state": {"$in": [ST_PENDING, ST_PROCESSING]}},
        {"$set": {"state": ST_CANCELLED, "cancelled_at": current,
                   "cancellation_reason": reason,
                   "lease_owner": None, "lease_expires_at": None, "updated_at": current}},
    )


async def cancel_alerts_for_cycle(
    db, *, assignment_cycle_id: str, reason: str, except_level: str | None = None,
) -> int:
    query: dict = {
        "assignment_cycle_id": assignment_cycle_id,
        "state": {"$in": [ST_PENDING, ST_PROCESSING]},
    }
    if except_level:
        query["alert_level"] = {"$ne": except_level}
    now = utc_now()
    result = await db[COLLECTION].update_many(query, {"$set": {
        "state": ST_CANCELLED, "cancelled_at": now, "cancellation_reason": reason,
        "lease_owner": None, "lease_expires_at": None, "updated_at": now,
    }})
    return result.modified_count
