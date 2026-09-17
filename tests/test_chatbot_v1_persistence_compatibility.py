import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import mongomock

sys.path.insert(0, str(Path(__file__).parents[1]))
from chatbot import chatbot_queue as queue


NOW = datetime(2026, 9, 16, 23, 0, tzinfo=timezone.utc)


def _db():
    return mongomock.MongoClient().get_database("chatbot_v1_compat")


def test_worker_attach_race_is_idempotent(monkeypatch):
    db = _db()
    collection = db[queue.JOB_COLLECTION]
    original_update_one = collection.update_one
    raced = {"value": False}

    def worker_wins(query, update, *args, **kwargs):
        if (
            not raced["value"]
            and query.get("kind") == queue.KIND_JOB
            and query.get("state") == queue.ST_RECEIVED
        ):
            raced["value"] = True
            job = collection.find_one({"_id": query["_id"]})
            batch = collection.find_one({"kind": queue.KIND_BATCH})
            original_update_one(
                {"_id": job["_id"]},
                {"$set": {"state": queue.ST_BATCHING, "batch_id": batch["_id"]}},
            )
            original_update_one(
                {"_id": batch["_id"]},
                {"$addToSet": {"job_ids": job["_id"]}},
            )
            return SimpleNamespace(modified_count=0, matched_count=1)
        return original_update_one(query, update, *args, **kwargs)

    monkeypatch.setattr(collection, "update_one", worker_wins)
    job_id = queue.create_inbound_job(
        db,
        inbound_provider_message_id="race-inbound-1",
        phone="+56911112222",
        text="hola",
        received_at=NOW,
    )

    job = collection.find_one({"_id": job_id})
    batch = collection.find_one({"kind": queue.KIND_BATCH})
    assert job["state"] == queue.ST_BATCHING
    assert job["batch_id"] == batch["_id"]
    assert batch["job_ids"] == [job_id]


def test_claim_accepts_received_job_during_legacy_attach_window():
    db = _db()
    collection = db[queue.JOB_COLLECTION]
    job_id = "received-before-attach"
    batch_id = "batch:received-before-attach"
    collection.insert_one({
        "_id": job_id,
        "kind": queue.KIND_JOB,
        "inbound_provider_message_id": "provider-received-before-attach",
        "phone": "+56911112222",
        "text": "quiero visitarla",
        "state": queue.ST_RECEIVED,
        "message_domain": "chatbot",
        "received_at": NOW,
    })
    collection.insert_one({
        "_id": batch_id,
        "kind": queue.KIND_BATCH,
        "message_domain": "chatbot",
        "phone": "+56911112222",
        "job_ids": [job_id],
        "state": queue.ST_BATCHING,
        "window_end_at": NOW,
        "created_at": NOW,
    })

    claimed = queue.claim_pending_batch(db, worker_id="worker", now=NOW)

    assert claimed["state"] == queue.ST_PROCESSING
    assert claimed["snapshot"][0]["job_id"] == job_id
    assert claimed["snapshot"][0]["text"] == "quiero visitarla"


def test_empty_batch_is_not_terminalized_during_attach_window():
    db = _db()
    collection = db[queue.JOB_COLLECTION]
    batch_id = "batch:transient-empty"
    collection.insert_one({
        "_id": batch_id,
        "kind": queue.KIND_BATCH,
        "message_domain": "chatbot",
        "phone": "+56911112222",
        "job_ids": [],
        "state": queue.ST_BATCHING,
        "window_end_at": NOW,
        "created_at": NOW,
    })

    assert queue.claim_pending_batch(db, worker_id="worker", now=NOW) is None
    batch = collection.find_one({"_id": batch_id})
    assert batch["state"] == queue.ST_BATCHING
    assert "last_error" not in batch


def test_legacy_batch_seed_contains_job_before_state_update():
    db = _db()
    collection = db[queue.JOB_COLLECTION]
    job_id = "legacy-job"
    collection.insert_one({
        "_id": job_id,
        "kind": queue.KIND_JOB,
        "inbound_provider_message_id": "legacy-provider",
        "phone": "+56911112222",
        "text": "hola",
        "state": queue.ST_RECEIVED,
        "is_from_me": False,
        "received_at": NOW,
    })

    batch = queue.batch_inbound_jobs(db, phone="+56911112222", now=NOW)

    assert batch["job_ids"] == [job_id]
    assert batch["conversation_sequence"] == 1
    assert collection.find_one({"_id": job_id})["message_domain"] == "chatbot"


def test_401_and_403_are_terminal_in_v1_delivery_state_machine():
    async def llm(_phone, _text):
        return "respuesta"

    async def run(status):
        db = _db()
        queue.create_inbound_job(
            db,
            inbound_provider_message_id=f"auth-{status}",
            phone="+56911112222",
            text="hola",
            received_at=NOW,
        )

        async def sender(_phone, _text):
            return {"success": False, "http_status": status, "provider_message_id": None}

        return await queue.process_one_batch(
            db, worker_id="worker", llm=llm, sender=sender,
            now=NOW + timedelta(seconds=15),
        )

    for status in (401, 403):
        result = asyncio.run(run(status))
        assert result["state"] == queue.ST_FAILED_TERMINAL
        assert result["next_attempt_at"] is None


def test_429_is_retryable_and_uncertain_provider_call_is_not_retried():
    async def llm(_phone, _text):
        return "respuesta"

    async def run(receipt):
        db = _db()
        queue.create_inbound_job(
            db,
            inbound_provider_message_id="retryable-inbound",
            phone="+56911112222",
            text="hola",
            received_at=NOW,
        )

        async def sender(_phone, _text):
            return receipt

        return await queue.process_one_batch(
            db, worker_id="worker", llm=llm, sender=sender,
            now=NOW + timedelta(seconds=15),
        )

    retryable = asyncio.run(run({
        "success": False, "http_status": 429, "retry_after": 37,
    }))
    assert retryable["state"] == queue.ST_FAILED_RETRYABLE
    assert retryable["next_attempt_at"].replace(tzinfo=timezone.utc) == NOW + timedelta(seconds=52)

    uncertain = asyncio.run(run({
        "success": False, "provider_call_uncertain": True, "http_status": None,
    }))
    assert uncertain["state"] == queue.ST_DELIVERY_UNKNOWN
    assert uncertain["next_attempt_at"] is None
