"""Durable inbound job queue for chatbot responses"""
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone, timedelta
from pymongo import ASCENDING, ReturnDocument

logger = logging.getLogger(__name__)

JOB_COLLECTION = "chatbot_inbound_jobs"

# States
ST_RECEIVED = "received"
ST_BATCHING = "batching"
ST_PENDING = "pending"
ST_PROCESSING = "processing"
ST_RESPONDED = "responded"
ST_FAILED_RETRYABLE = "failed_retryable"
ST_FAILED_TERMINAL = "failed_terminal"
ST_DELIVERY_UNKNOWN = "delivery_unknown"


def utc_now():
    return datetime.now(timezone.utc)


def create_inbound_job(db, *, inbound_provider_message_id, conversation_id=None,
                       lead_id=None, phone, text, received_at=None, is_from_me=False):
    """Create or get existing inbound job (idempotent by provider message id)."""
    now = received_at or utc_now()
    doc = {
        "inbound_provider_message_id": inbound_provider_message_id,
        "conversation_id": conversation_id,
        "lead_id": lead_id,
        "phone": phone,
        "received_at": now,
        "text": text,
        "state": ST_RECEIVED,
        "attempts": 0,
        "is_from_me": is_from_me,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = db[JOB_COLLECTION].insert_one(doc)
        return result.inserted_id
    except Exception:
        existing = db[JOB_COLLECTION].find_one({"inbound_provider_message_id": inbound_provider_message_id})
        if existing:
            return existing["_id"]
        return None


def batch_inbound_jobs(db, *, phone, batch_id=None, max_wait_seconds=15):
    """Collect all received jobs for a phone into a batch. Close after max_wait_seconds."""
    now = utc_now()
    batch_id = batch_id or str(uuid.uuid4())
    cutoff = now - timedelta(seconds=max_wait_seconds)
    
    # Find jobs in received state for this phone, within the window
    jobs = list(db[JOB_COLLECTION].find({
        "phone": phone,
        "state": ST_RECEIVED,
        "is_from_me": False,
        "received_at": {"$gte": cutoff},
    }).sort("received_at", ASCENDING))
    
    if not jobs:
        return None
    
    # Atomically claim them into a batch
    texts = []
    ids = []
    for job in jobs:
        updated = db[JOB_COLLECTION].find_one_and_update(
            {"_id": job["_id"], "state": ST_RECEIVED},
            {"$set": {"state": ST_BATCHING, "batch_id": batch_id, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if updated:
            texts.append(str(job.get("text", "")).strip())
            ids.append(str(job["_id"]))
    
    if not texts:
        return None
    
    # Close the batch
    cid = job.get("conversation_id") or ids[0]
    lid = job.get("lead_id")
    batch_doc = {
        "_id": batch_id,
        "phone": phone,
        "conversation_id": cid,
        "lead_id": lid,
        "job_ids": ids,
        "combined_text": "\n".join(texts),
        "state": ST_PENDING,
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }
    db[JOB_COLLECTION].insert_one(batch_doc)
    return batch_doc


def claim_pending_batch(db, *, worker_id, lease_seconds=120):
    """Claim a pending batch atomically."""
    now = utc_now()
    return db[JOB_COLLECTION].find_one_and_update(
        {"state": ST_PENDING},
        {"$set": {"state": ST_PROCESSING, "lease_owner": worker_id,
                  "lease_until": now + timedelta(seconds=lease_seconds),
                  "processing_started_at": now, "updated_at": now}},
        sort=[("created_at", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )


def finalize_batch(db, *, batch_id, state, outbound_provider_message_id=None,
                   error=None, worker_id=None):
    """Finalize a batch after processing."""
    now = utc_now()
    update = {"state": state, "updated_at": now}
    if outbound_provider_message_id:
        update["outbound_provider_message_id"] = outbound_provider_message_id
    if error:
        update["last_error"] = error
    if state == ST_RESPONDED:
        update["responded_at"] = now
    if state in (ST_FAILED_RETRYABLE, ST_FAILED_TERMINAL):
        update["attempts"] = 1  # Will be incremented properly
    db[JOB_COLLECTION].update_one({"_id": batch_id}, {"$set": update})
    
    # Update all jobs in the batch
    job_state = ST_RESPONDED if state == ST_RESPONDED else state
    batch = db[JOB_COLLECTION].find_one({"_id": batch_id})
    if batch:
        for jid in batch.get("job_ids", []):
            db[JOB_COLLECTION].update_one(
                {"_id": jid},
                {"$set": {"state": job_state, "batch_id": batch_id, "updated_at": now}},
            )


def recover_expired_batches(db, *, worker_id, max_age_seconds=300):
    """Recover batches stuck in processing with expired leases."""
    now = utc_now()
    cutoff = now - timedelta(seconds=max_age_seconds)
    return list(db[JOB_COLLECTION].find({
        "state": ST_PROCESSING,
        "lease_until": {"$lte": now},
        "processing_started_at": {"$gte": cutoff},
    }))


def get_pending_counts(db):
    """Get counts for health monitoring."""
    now = utc_now()
    return {
        "inbound_received": db[JOB_COLLECTION].count_documents({"state": ST_RECEIVED}),
        "inbound_batching": db[JOB_COLLECTION].count_documents({"state": ST_BATCHING}),
        "inbound_pending": db[JOB_COLLECTION].count_documents({"state": ST_PENDING}),
        "inbound_processing": db[JOB_COLLECTION].count_documents({"state": ST_PROCESSING}),
        "inbound_failed_retryable": db[JOB_COLLECTION].count_documents({"state": ST_FAILED_RETRYABLE}),
        "oldest_pending": None,
    }


# ---- Worker ----

async def chatbot_response_worker_loop():
    """Periodic worker: batch received jobs, process via LLM, send via WASender."""
    import os, asyncio, hashlib, time
    from chatbot.storage import get_db
    worker_id = f"chatbot_response_{os.getpid()}"
    logger.info("[CHATBOT_WORKER] Starting worker %s", worker_id)
    
    # Track in global status
    try:
        from webhook import background_tasks_status
    except ImportError:
        background_tasks_status = {}
    background_tasks_status["chatbot_response"] = {"status": "running"}

    while True:
        try:
            db = get_db()
            loop = asyncio.get_running_loop()

            # 1. Batch any received jobs older than 15s
            phones_with_received = db[JOB_COLLECTION].distinct("phone", {"state": ST_RECEIVED})
            for phone in phones_with_received:
                batch = await loop.run_in_executor(None, lambda p=phone: batch_inbound_jobs(db, phone=p, max_wait_seconds=15))
                if batch:
                    logger.info("[CHATBOT_WORKER] Batch created %s for phone %s", batch["_id"], phone[-4:])

            # 2. Claim a pending batch
            batch = await loop.run_in_executor(None, lambda: claim_pending_batch(db, worker_id=worker_id))
            if not batch:
                await asyncio.sleep(5)
                continue

            bid = batch["_id"]
            phone = batch.get("phone", "")
            combined = batch.get("combined_text", "")
            logger.info("[CHATBOT_WORKER] Processing batch %s phone=%s len=%d", str(bid)[:12], phone[-4:], len(combined))

            # 3. Generate response via LLM
            try:
                from chatbot.core import process_user_message_sync
                response = await loop.run_in_executor(None, lambda: process_user_message_sync(phone, combined))
            except Exception as exc:
                logger.error("[CHATBOT_WORKER] LLM error batch=%s: %s", str(bid)[:12], exc)
                finalize_batch(db, batch_id=bid, state=ST_FAILED_RETRYABLE,
                               error=str(exc)[:500], worker_id=worker_id)
                # Also call existing process_with_debounce as fallback
                try:
                    from webhook import process_with_debounce
                    await process_with_debounce(phone, combined, is_from_me=False)
                except Exception:
                    pass
                continue

            if not response or not response.strip():
                finalize_batch(db, batch_id=bid, state=ST_FAILED_RETRYABLE,
                               error="empty_response", worker_id=worker_id)
                continue

            # 4. Send via WASender
            try:
                from chatbot.whatsapp_client import send_whatsapp_message_detailed
                receipt = await send_whatsapp_message_detailed(phone, response)
                success = receipt.get("success")
                pid = receipt.get("provider_message_id")

                if success and pid:
                    finalize_batch(db, batch_id=bid, state=ST_RESPONDED,
                                   outbound_provider_message_id=pid, worker_id=worker_id)
                    logger.info("[CHATBOT_WORKER] Responded batch=%s pid=%s phone=%s",
                                str(bid)[:12], pid, phone[-4:])
                else:
                    http_status = receipt.get("http_status")
                    state = ST_FAILED_RETRYABLE if http_status != 422 else ST_FAILED_TERMINAL
                    error = f"http_{http_status}" if http_status else "send_failed"
                    finalize_batch(db, batch_id=bid, state=state, error=error, worker_id=worker_id)
            except Exception as exc:
                finalize_batch(db, batch_id=bid, state=ST_FAILED_RETRYABLE,
                               error=str(exc)[:500], worker_id=worker_id)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("[CHATBOT_WORKER] Loop error: %s", exc)
            await asyncio.sleep(10)
