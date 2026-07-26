"""Durable, idempotent inbound queue for chatbot responses.

The webhook is only a producer.  This module owns batching, claiming, LLM
execution and provider delivery so an inbound message can never enter two
response pipelines.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

JOB_COLLECTION = "chatbot_inbound_jobs"
KIND_JOB = "inbound_job"
KIND_BATCH = "response_batch"
ST_RECEIVED = "received"
ST_BATCHING = "batching"
ST_PENDING = "pending"
ST_PROCESSING = "processing"
ST_RESPONDED = "responded"
ST_FAILED_RETRYABLE = "failed_retryable"
ST_FAILED_TERMINAL = "failed_terminal"
ST_DELIVERY_UNKNOWN = "delivery_unknown"
TERMINAL_STATES = (ST_RESPONDED, ST_FAILED_TERMINAL, ST_DELIVERY_UNKNOWN)
ID_LIKE_RE = re.compile(r"^(?:[0-9a-f]{24}|[0-9a-f-]{32,36}|[A-Za-z0-9_-]{40,})$", re.I)


def utc_now():
    return datetime.now(timezone.utc)


def _valid_text(value):
    text = str(value or "").strip()
    return text if text and not ID_LIKE_RE.fullmatch(text) else None


def ensure_queue_indexes(db):
    coll = db[JOB_COLLECTION]
    coll.create_index(
        [("inbound_provider_message_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"kind": KIND_JOB},
        name="uniq_inbound_provider_message",
    )
    coll.create_index(
        [("kind", ASCENDING), ("state", ASCENDING), ("window_end_at", ASCENDING)],
        name="batch_claim",
    )


def create_inbound_job(
    db,
    *,
    inbound_provider_message_id,
    conversation_id=None,
    lead_id=None,
    phone,
    text,
    received_at=None,
    is_from_me=False,
    max_wait_seconds=15,
):
    """Idempotently persist one inbound and attach it to one durable batch."""
    provider_id = str(inbound_provider_message_id or "").strip()
    clean_text = _valid_text(text)
    if not provider_id:
        raise ValueError("inbound_provider_message_id_required")
    if not clean_text:
        raise ValueError("invalid_inbound_text")
    if is_from_me:
        raise ValueError("outbound_message_not_queueable")

    now = received_at or utc_now()
    coll = db[JOB_COLLECTION]
    job_id = str(uuid.uuid4())
    job = {
        "_id": job_id,
        "kind": KIND_JOB,
        "inbound_provider_message_id": provider_id,
        "conversation_id": conversation_id,
        "lead_id": lead_id,
        "phone": phone,
        "received_at": now,
        "text": clean_text,
        "state": ST_RECEIVED,
        "attempts": 0,
        "is_from_me": False,
        "created_at": now,
        "updated_at": now,
        "message_domain": "chatbot",
        "message_type": "inbound_message",
        "recipient_role": "client",
        "state_source": JOB_COLLECTION,
        "responsible_service": "chatbot_response_delivery",
        "idempotency_key": f"chatbot:inbound:{provider_id}",
    }
    try:
        coll.insert_one(job)
    except DuplicateKeyError:
        existing = coll.find_one({
            "kind": KIND_JOB,
            "inbound_provider_message_id": provider_id,
        })
        if not existing:
            raise
        return existing["_id"]

    # Reuse the open batch for this conversation. If two producers race, only
    # one job can be attached because the job update below is conditional.
    batch = coll.find_one({
        "kind": KIND_BATCH,
        "phone": phone,
        "state": ST_BATCHING,
        "window_end_at": {"$gt": now},
    }, sort=[("created_at", ASCENDING)])
    if batch is None:
        batch_id = f"batch:{uuid.uuid4()}"
        window_end = now + timedelta(seconds=max_wait_seconds)
        coll.insert_one({
            "_id": batch_id,
            "kind": KIND_BATCH,
            "phone": phone,
            "conversation_id": conversation_id,
            "lead_id": lead_id,
            "job_ids": [],
            "state": ST_BATCHING,
            "attempts": 0,
            "window_end_at": window_end,
            "created_at": now,
            "updated_at": now,
            "delivery_attempts": [],
            "message_domain": "chatbot",
            "message_type": "chatbot_response",
            "recipient_role": "client",
            "state_source": JOB_COLLECTION,
            "responsible_service": "chatbot_response_delivery",
            "idempotency_key": f"chatbot:batch:{batch_id}",
        })
    else:
        batch_id = batch["_id"]

    attached = coll.update_one(
        {"_id": job_id, "kind": KIND_JOB, "state": ST_RECEIVED},
        {"$set": {"state": ST_BATCHING, "batch_id": batch_id, "updated_at": now}},
    )
    if attached.modified_count != 1:
        raise RuntimeError("inbound_job_attach_failed")
    coll.update_one(
        {"_id": batch_id, "kind": KIND_BATCH, "state": ST_BATCHING},
        {"$addToSet": {"job_ids": job_id}, "$set": {"updated_at": now}},
    )
    return job_id


def batch_inbound_jobs(db, *, phone, batch_id=None, max_wait_seconds=15, now=None):
    """Compatibility entry point; attach legacy received jobs without closing early."""
    now = now or utc_now()
    coll = db[JOB_COLLECTION]
    jobs = list(coll.find({
        "kind": {"$in": [KIND_JOB, None]},
        "phone": phone,
        "state": ST_RECEIVED,
        "is_from_me": False,
    }).sort("received_at", ASCENDING))
    if not jobs:
        return None
    first = jobs[0]
    batch_id = batch_id or f"batch:{uuid.uuid4()}"
    window_end = first.get("received_at", now) + timedelta(seconds=max_wait_seconds)
    batch = {
        "_id": batch_id,
        "kind": KIND_BATCH,
        "phone": phone,
        "conversation_id": first.get("conversation_id"),
        "lead_id": first.get("lead_id"),
        "job_ids": [],
        "state": ST_BATCHING,
        "attempts": 0,
        "window_end_at": window_end,
        "created_at": now,
        "updated_at": now,
        "delivery_attempts": [],
        "message_domain": "chatbot",
        "message_type": "chatbot_response",
        "recipient_role": "client",
        "state_source": JOB_COLLECTION,
        "responsible_service": "chatbot_response_delivery",
        "idempotency_key": f"chatbot:batch:{batch_id}",
    }
    coll.insert_one(batch)
    for job in jobs:
        updated = coll.update_one(
            {"_id": job["_id"], "state": ST_RECEIVED},
            {"$set": {"kind": KIND_JOB, "state": ST_BATCHING,
                      "batch_id": batch_id, "updated_at": now}},
        )
        if updated.modified_count:
            coll.update_one({"_id": batch_id}, {"$addToSet": {"job_ids": job["_id"]}})
    return coll.find_one({"_id": batch_id})


def claim_pending_batch(db, *, worker_id, lease_seconds=120, now=None):
    """Atomically claim a due, undelivered batch and freeze its message snapshot."""
    now = now or utc_now()
    coll = db[JOB_COLLECTION]
    query = {
        "kind": KIND_BATCH,
        "message_domain": "chatbot",
        "state": {"$in": [ST_BATCHING, ST_PENDING, ST_FAILED_RETRYABLE]},
        "window_end_at": {"$lte": now},
        "outbound_provider_message_id": {"$exists": False},
        "accepted_delivery_token": {"$exists": False},
        "$and": [
            {"$or": [{"lease_until": {"$exists": False}}, {"lease_until": {"$lte": now}}]},
            {"$or": [{"next_attempt_at": {"$exists": False}}, {"next_attempt_at": {"$lte": now}}]},
        ],
    }
    candidate = coll.find_one(query, sort=[("window_end_at", ASCENDING)])
    if not candidate:
        return None
    jobs = list(coll.find({
        "_id": {"$in": candidate.get("job_ids", [])},
        "kind": KIND_JOB,
        "message_domain": "chatbot",
        "state": {"$in": [ST_BATCHING, ST_PENDING, ST_FAILED_RETRYABLE]},
    }).sort("received_at", ASCENDING))
    messages = [
        {"job_id": job["_id"], "provider_id": job.get("inbound_provider_message_id"),
         "text": _valid_text(job.get("text")), "received_at": job.get("received_at")}
        for job in jobs
    ]
    if not messages or any(not item["text"] for item in messages):
        coll.update_one(
            {"_id": candidate["_id"], "state": candidate["state"]},
            {"$set": {"state": ST_FAILED_TERMINAL, "last_error": "invalid_or_empty_snapshot",
                      "updated_at": now}},
        )
        return None
    delivery_token = candidate.get("delivery_token") or str(uuid.uuid4())
    return coll.find_one_and_update(
        {"_id": candidate["_id"], "state": candidate["state"],
         "$or": [{"lease_until": {"$exists": False}}, {"lease_until": {"$lte": now}}]},
        {"$set": {
            "state": ST_PROCESSING,
            "lease_owner": worker_id,
            "lease_until": now + timedelta(seconds=lease_seconds),
            "processing_started_at": now,
            "updated_at": now,
            "snapshot": messages,
            "combined_text": "\n".join(item["text"] for item in messages),
            "delivery_token": delivery_token,
        }, "$inc": {"claim_count": 1}},
        return_document=ReturnDocument.AFTER,
    )


def record_delivery_attempt(db, *, batch_id, worker_id, delivery_token, status,
                            provider_message_id=None, http_status=None, error=None, now=None):
    now = now or utc_now()
    attempt = {
        "at": now,
        "worker_id": worker_id,
        "delivery_token": delivery_token,
        "status": status,
        "provider_message_id": provider_message_id,
        "http_status": http_status,
        "error": error,
    }
    result = db[JOB_COLLECTION].update_one(
        {"_id": batch_id, "kind": KIND_BATCH, "lease_owner": worker_id,
         "delivery_token": delivery_token},
        {"$push": {"delivery_attempts": attempt}, "$set": {"updated_at": now}},
    )
    if result.matched_count != 1:
        raise RuntimeError("delivery_attempt_lease_lost")


def finalize_batch(db, *, batch_id, state, outbound_provider_message_id=None,
                   error=None, worker_id=None, delivery_token=None,
                   next_attempt_at=None, now=None):
    now = now or utc_now()
    selector = {"_id": batch_id, "kind": KIND_BATCH}
    if worker_id:
        selector["lease_owner"] = worker_id
    if delivery_token:
        selector["delivery_token"] = delivery_token
    update = {
        "state": state,
        "updated_at": now,
        "last_error": error,
        "next_attempt_at": next_attempt_at,
    }
    if outbound_provider_message_id:
        update["outbound_provider_message_id"] = outbound_provider_message_id
        update["accepted_delivery_token"] = delivery_token
    if state == ST_RESPONDED:
        update["responded_at"] = now
    result = db[JOB_COLLECTION].find_one_and_update(
        selector,
        {"$set": update, "$unset": {"lease_owner": "", "lease_until": ""}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise RuntimeError("finalize_batch_lease_lost")
    db[JOB_COLLECTION].update_many(
        {"_id": {"$in": result.get("job_ids", [])}, "kind": KIND_JOB},
        {"$set": {"state": state, "batch_id": batch_id, "updated_at": now}},
    )
    return result


def recover_expired_batches(db, *, worker_id=None, max_age_seconds=300, now=None):
    now = now or utc_now()
    return list(db[JOB_COLLECTION].find({
        "kind": KIND_BATCH,
        "state": ST_PROCESSING,
        "lease_until": {"$lte": now},
    }))


def reconcile_expired_leases(db, *, now=None):
    """Make expired claims explicit; never blindly resend an uncertain delivery."""
    now = now or utc_now()
    coll = db[JOB_COLLECTION]
    recovered = {"retryable": 0, "delivery_unknown": 0}
    expired = list(coll.find({
        "kind": KIND_BATCH,
        "state": ST_PROCESSING,
        "lease_until": {"$lte": now},
    }))
    for batch in expired:
        attempts = batch.get("delivery_attempts") or []
        last_status = attempts[-1].get("status") if attempts else None
        delivery_started_without_outcome = last_status == "started"
        state = ST_DELIVERY_UNKNOWN if delivery_started_without_outcome else ST_FAILED_RETRYABLE
        error = (
            "lease_expired_after_delivery_started"
            if delivery_started_without_outcome
            else "lease_expired_before_delivery"
        )
        result = coll.update_one(
            {"_id": batch["_id"], "state": ST_PROCESSING,
             "lease_until": {"$lte": now}},
            {"$set": {"state": state, "last_error": error, "updated_at": now,
                      "next_attempt_at": now},
             "$unset": {"lease_owner": "", "lease_until": ""}},
        )
        if result.modified_count:
            recovered["delivery_unknown" if state == ST_DELIVERY_UNKNOWN else "retryable"] += 1
    return recovered


def get_queue_health(db, *, heartbeat=None, now=None, heartbeat_max_age_seconds=30):
    now = now or utc_now()
    coll = db[JOB_COLLECTION]
    states = {}
    for state in (ST_RECEIVED, ST_BATCHING, ST_PENDING, ST_PROCESSING, ST_RESPONDED,
                  ST_FAILED_RETRYABLE, ST_FAILED_TERMINAL, ST_DELIVERY_UNKNOWN):
        states[state] = coll.count_documents({"kind": KIND_BATCH, "state": state})
    oldest = coll.find_one(
        {"kind": KIND_BATCH, "state": {"$in": [ST_BATCHING, ST_PENDING, ST_FAILED_RETRYABLE]}},
        sort=[("window_end_at", ASCENDING)],
    )
    expired = coll.count_documents({
        "kind": KIND_BATCH, "state": ST_PROCESSING, "lease_until": {"$lte": now},
    })
    jobs_without_batch = coll.count_documents({
        "kind": KIND_JOB, "batch_id": {"$exists": False},
    })
    terminal_invalid_batches = coll.count_documents({
        "kind": KIND_BATCH, "state": ST_FAILED_TERMINAL,
        "last_error": "invalid_or_empty_snapshot",
    })
    empty_batches = coll.count_documents({
        "kind": KIND_BATCH, "state": {"$nin": list(TERMINAL_STATES)},
        "$or": [{"job_ids": {"$size": 0}}, {"job_ids": {"$exists": False}}],
    })
    stuck_due_batches = coll.count_documents({
        "kind": KIND_BATCH,
        "state": {"$in": [ST_BATCHING, ST_PENDING, ST_FAILED_RETRYABLE]},
        "window_end_at": {"$lte": now - timedelta(seconds=30)},
        "$or": [{"next_attempt_at": {"$exists": False}}, {"next_attempt_at": {"$lte": now}}],
    })
    recent_errors = list(coll.find(
        {"kind": KIND_BATCH, "last_error": {"$nin": [None, ""]}},
        {"_id": 1, "state": 1, "last_error": 1, "updated_at": 1},
    ).sort("updated_at", -1).limit(5))
    heartbeat_at = (heartbeat or {}).get("last_heartbeat")
    if isinstance(heartbeat_at, str):
        heartbeat_at = datetime.fromisoformat(heartbeat_at)
    if heartbeat_at and heartbeat_at.tzinfo is None:
        heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
    heartbeat_age = (now - heartbeat_at.astimezone(timezone.utc)).total_seconds() if heartbeat_at else None
    degraded_reasons = []
    if heartbeat_age is None or heartbeat_age > heartbeat_max_age_seconds:
        degraded_reasons.append("worker_heartbeat_stale_or_missing")
    if expired:
        degraded_reasons.append("expired_processing_leases")
    if jobs_without_batch:
        degraded_reasons.append("jobs_without_batch")
    if empty_batches:
        degraded_reasons.append("empty_batches")
    if stuck_due_batches:
        degraded_reasons.append("stuck_due_batches")
    if states[ST_FAILED_RETRYABLE]:
        degraded_reasons.append("failed_retryable_present")
    if states[ST_DELIVERY_UNKNOWN]:
        degraded_reasons.append("delivery_unknown_present")
    return {
        "metrics_available": True,
        "worker_heartbeat": heartbeat_at,
        "worker_heartbeat_age_seconds": heartbeat_age,
        "jobs_by_state": states,
        "oldest_pending_at": oldest.get("window_end_at") if oldest else None,
        "processing_with_expired_lease": expired,
        "jobs_without_batch": jobs_without_batch,
        "empty_batches": empty_batches,
        "terminal_invalid_batches": terminal_invalid_batches,
        "stuck_due_batches": stuck_due_batches,
        "delivery_unknown": states[ST_DELIVERY_UNKNOWN],
        "recent_worker_errors": recent_errors,
        "degraded_reasons": degraded_reasons,
    }


def get_pending_counts(db):
    health = get_queue_health(db)
    return {
        f"inbound_{key}": value for key, value in health["jobs_by_state"].items()
    } | {"oldest_pending": health["oldest_pending_at"]}


async def process_one_batch(db, *, worker_id, llm, sender, now=None):
    """Process at most one due batch. Injection points keep tests/provider dry."""
    claimed = await asyncio.to_thread(
        claim_pending_batch, db, worker_id=worker_id, now=now
    )
    if not claimed:
        return None
    batch_id = claimed["_id"]
    token = claimed["delivery_token"]
    text = _valid_text(claimed.get("combined_text"))
    if not text:
        return await asyncio.to_thread(
            finalize_batch, db, batch_id=batch_id, state=ST_FAILED_TERMINAL,
            error="invalid_or_empty_snapshot", worker_id=worker_id,
            delivery_token=token,
        )
    try:
        response = await llm(claimed["phone"], text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return await asyncio.to_thread(
            finalize_batch, db, batch_id=batch_id, state=ST_FAILED_RETRYABLE,
            error=f"llm:{type(exc).__name__}:{str(exc)[:300]}", worker_id=worker_id,
            delivery_token=token,
            next_attempt_at=(now or utc_now()) + timedelta(seconds=30),
        )
    response = _valid_text(response)
    if not response:
        return await asyncio.to_thread(
            finalize_batch, db, batch_id=batch_id, state=ST_FAILED_RETRYABLE,
            error="empty_response", worker_id=worker_id, delivery_token=token,
        )

    # Commercial work is a separate durable state machine. It observes the
    # verified inbound snapshot, but cannot reuse or block chatbot delivery.
    try:
        from .commercial_intake import ensure_indexes, process_inbound
        await asyncio.to_thread(ensure_indexes, db)
        for item in claimed.get("snapshot") or []:
            job = await asyncio.to_thread(
                db[JOB_COLLECTION].find_one, {"_id": item["job_id"]}
            )
            if job:
                await asyncio.to_thread(
                    process_inbound, db,
                    inbound_provider_id=item.get("provider_id"),
                    phone=claimed["phone"], text=item.get("text") or "",
                    received_at=job.get("received_at"), is_test=bool(job.get("is_test")),
                )
    except Exception:
        logger.exception("[COMMERCIAL_INTAKE] durable commercial processing failed")

    await asyncio.to_thread(
        record_delivery_attempt, db, batch_id=batch_id, worker_id=worker_id,
        delivery_token=token,
        status="started",
    )
    try:
        receipt = await sender(claimed["phone"], response)
    except asyncio.CancelledError:
        await asyncio.to_thread(
            finalize_batch, db, batch_id=batch_id, state=ST_DELIVERY_UNKNOWN,
            error="send_cancelled_after_attempt_started", worker_id=worker_id,
            delivery_token=token,
        )
        raise
    except Exception as exc:
        await asyncio.to_thread(
            record_delivery_attempt, db, batch_id=batch_id, worker_id=worker_id,
            delivery_token=token,
            status=ST_DELIVERY_UNKNOWN, error=type(exc).__name__,
        )
        return await asyncio.to_thread(
            finalize_batch, db, batch_id=batch_id, state=ST_DELIVERY_UNKNOWN,
            error=f"send:{type(exc).__name__}", worker_id=worker_id,
            delivery_token=token,
        )

    http_status = receipt.get("http_status")
    provider_id = receipt.get("provider_message_id")
    if receipt.get("provider_call_uncertain"):
        state, error, retry_at = ST_DELIVERY_UNKNOWN, "provider_call_uncertain", None
    elif receipt.get("success") and provider_id:
        await asyncio.to_thread(
            record_delivery_attempt, db, batch_id=batch_id, worker_id=worker_id,
            delivery_token=token,
            status="accepted", provider_message_id=provider_id, http_status=http_status,
        )
        return await asyncio.to_thread(
            finalize_batch, db, batch_id=batch_id, state=ST_RESPONDED,
            outbound_provider_message_id=provider_id, worker_id=worker_id,
            delivery_token=token,
        )
    elif receipt.get("success") and not provider_id:
        state, error = ST_DELIVERY_UNKNOWN, "accepted_without_provider_message_id"
        retry_at = None
    elif http_status == 422:
        state, error, retry_at = ST_FAILED_TERMINAL, "http_422", None
    elif http_status == 429:
        retry_after = max(int(receipt.get("retry_after") or 60), 1)
        state, error = ST_FAILED_RETRYABLE, "http_429"
        retry_at = (now or utc_now()) + timedelta(seconds=retry_after)
    else:
        state, error, retry_at = ST_FAILED_RETRYABLE, f"http_{http_status or 'unknown'}", None
    await asyncio.to_thread(
        record_delivery_attempt, db, batch_id=batch_id, worker_id=worker_id,
        delivery_token=token,
        status=state, http_status=http_status, error=error,
    )
    return await asyncio.to_thread(
        finalize_batch, db, batch_id=batch_id, state=state, error=error,
        worker_id=worker_id,
        delivery_token=token, next_attempt_at=retry_at,
    )


async def chatbot_response_worker_loop():
    from chatbot.core import process_user_message_sync
    from chatbot.storage import get_db
    from chatbot.whatsapp_client import send_whatsapp_message_detailed

    worker_id = f"chatbot_response_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        from webhook import background_tasks_status
    except ImportError:
        background_tasks_status = {}
    status = background_tasks_status.setdefault("chatbot_response", {})
    logger.info("[CHATBOT_WORKER] starting worker=%s", worker_id)
    try:
        db = get_db()
        ensure_queue_indexes(db)
        while True:
            heartbeat = utc_now()
            status.update({"status": "running", "last_heartbeat": heartbeat.isoformat(),
                           "worker_id": worker_id})
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: reconcile_expired_leases(db))
            legacy_phones = await loop.run_in_executor(
                None,
                lambda: db[JOB_COLLECTION].distinct(
                    "phone", {"state": ST_RECEIVED, "is_from_me": False}
                ),
            )
            for phone in legacy_phones:
                await loop.run_in_executor(
                    None, lambda p=phone: batch_inbound_jobs(db, phone=p)
                )

            async def llm(phone, text):
                return await loop.run_in_executor(
                    None, lambda: process_user_message_sync(phone, text)
                )

            try:
                processed = await process_one_batch(
                    db, worker_id=worker_id, llm=llm,
                    sender=send_whatsapp_message_detailed,
                )
                if not processed:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                status.update({"status": "stopped", "last_heartbeat": utc_now().isoformat()})
                raise
            except Exception as exc:
                status.update({"status": "error", "last_error": type(exc).__name__,
                               "last_heartbeat": utc_now().isoformat()})
                logger.exception("[CHATBOT_WORKER] loop failure")
                await asyncio.sleep(5)
    except asyncio.CancelledError:
        raise
