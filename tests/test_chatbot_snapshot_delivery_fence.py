"""Delivery-fence regressions; MongoDB and WhatsApp are fully mocked."""
import asyncio
from datetime import datetime, timedelta, timezone

import mongomock
import pytest

from chatbot import chatbot_queue as queue
from chatbot import commercial_intake, storage


@pytest.fixture
def sandbox(monkeypatch):
    db = mongomock.MongoClient()["URLS"]
    monkeypatch.setattr(queue, "_human_takeover_after_batch_start",
                        lambda *a, **kw: False)
    monkeypatch.setattr(queue, "_update_generated_response",
                        lambda *a, **kw: False)
    monkeypatch.setattr(storage, "record_observability_event",
                        lambda *a, **kw: None)
    monkeypatch.setattr(commercial_intake, "ensure_indexes",
                        lambda *a, **kw: None)
    monkeypatch.setattr(commercial_intake, "process_inbound",
                        lambda *a, **kw: None)
    monkeypatch.setattr(queue, "_recent_bot_response_is_duplicate",
                        lambda *a, **kw: False)
    return db


def inbound(db, provider_id, text, when):
    return queue.create_inbound_job(
        db, inbound_provider_message_id=provider_id,
        phone="+56911112222", text=text, received_at=when,
    )


def run(coro):
    return asyncio.run(coro)


def test_new_search_after_llm_but_before_provider_is_not_falsely_answered(sandbox, monkeypatch):
    db = sandbox
    now = datetime.now(timezone.utc)
    inbound(db, "id-initial", "hola", now)
    calls = []
    sends = []
    fired = []

    def late_during_guards(_db, *, phone, response):
        if not fired:
            fired.append(inbound(
                db, "id-late", "busco un departamento en Pudahuel en venta",
                now + timedelta(seconds=20),
            ))
        return False

    monkeypatch.setattr(queue, "_recent_bot_response_is_duplicate", late_during_guards)

    async def llm(phone, text):
        calls.append(text)
        return "Respuesta Pudahuel" if "Pudahuel" in text else "Qué buscas?"

    async def sender(phone, text):
        sends.append(text)
        return {"success": True, "http_status": 200, "provider_message_id": "send-1"}

    a = run(queue.process_one_batch(
        db, worker_id="first", llm=llm, sender=sender,
        now=now + timedelta(seconds=16),
    ))
    assert a["state"] == queue.ST_FAILED_RETRYABLE
    assert a["last_error"] == "stale_snapshot_before_provider"
    assert sends == []
    assert len(a["job_ids"]) == 2
    assert all(db[queue.JOB_COLLECTION].find_one({"_id": id})["state"]
               == queue.ST_FAILED_RETRYABLE for id in a["job_ids"])
    assert all(item["status"] != "started" for item in a["delivery_attempts"])

    b = run(queue.process_one_batch(
        db, worker_id="second", llm=llm, sender=sender,
        now=now + timedelta(seconds=50),
    ))
    assert b["state"] == queue.ST_RESPONDED
    assert calls == [
        "hola", "hola\nbusco un departamento en Pudahuel en venta",
    ]
    assert sends == ["Respuesta Pudahuel"]
    assert b["snapshot_sequence"] == b["conversation_sequence"]
    assert all(db[queue.JOB_COLLECTION].find_one({"_id": id})["state"]
               == queue.ST_RESPONDED for id in b["job_ids"])


def test_new_customer_message_during_provider_delivery_forms_new_batch(sandbox):
    db = sandbox
    now = datetime.now(timezone.utc)
    inbound(db, "id-a", "hola", now)
    sent = []

    async def llm(phone, text):
        return "Respuesta: " + text

    async def sender(phone, text):
        sent.append(text)
        if len(sent) == 1:
            inbound(db, "id-b", "depto en Pudahuel",
                    now + timedelta(seconds=20))
        return {"success": True, "http_status": 200,
                "provider_message_id": "sent-" + str(len(sent))}

    a = run(queue.process_one_batch(
        db, worker_id="worker-a", llm=llm, sender=sender,
        now=now + timedelta(seconds=16),
    ))
    assert a["state"] == queue.ST_RESPONDED
    assert len(a["job_ids"]) == 1

    b = run(queue.process_one_batch(
        db, worker_id="worker-b", llm=llm, sender=sender,
        now=now + timedelta(seconds=50),
    ))
    assert b["state"] == queue.ST_RESPONDED
    assert b["_id"] != a["_id"]
    assert sent == ["Respuesta: hola", "Respuesta: depto en Pudahuel"]


def test_inbound_rehomed_when_producer_and_delivery_fence_race(sandbox, monkeypatch):
    db = sandbox
    now = datetime.now(timezone.utc)
    earlier_id = inbound(db, "id-original", "hola", now)
    claimed = queue.claim_pending_batch(
        db, worker_id="sealer", now=now + timedelta(seconds=16),
    )
    assert claimed["state"] == queue.ST_PROCESSING

    collection = db[queue.JOB_COLLECTION]
    original_update = collection.update_one
    injected = []

    def race_update(filter, update, *args, **kwargs):
        result = original_update(filter, update, *args, **kwargs)
        if (not injected and filter.get("kind") == queue.KIND_JOB
                and filter.get("state") == queue.ST_RECEIVED):
            injected.append(True)
            sealed = queue.seal_batch_for_delivery(
                db, batch_id=claimed["_id"], worker_id="sealer",
                delivery_token=claimed["delivery_token"],
                snapshot_sequence=claimed["snapshot_sequence"],
                now=now + timedelta(seconds=20),
            )
            assert sealed and sealed.get("active_conversation_key") is None
        return result

    monkeypatch.setattr(collection, "update_one", race_update)
    late_id = inbound(db, "id-late-racing", "busco depto en Pudahuel",
                      now + timedelta(seconds=20))
    old = collection.find_one({"_id": claimed["_id"]})
    later = collection.find_one({"_id": late_id})
    assert old["job_ids"] == [earlier_id]
    assert later["batch_id"] != claimed["_id"]
    fresh = collection.find_one({"_id": later["batch_id"]})
    assert fresh["job_ids"] == [late_id]
    assert fresh["state"] == queue.ST_BATCHING



def test_http_429_retry_respects_prior_delivery_seal(sandbox):
    """Regression: 429 after seal must not retry stale_snapshot forever."""
    db = sandbox
    now = datetime.now(timezone.utc)
    inbound(db, "id-429", "me interesa mañana a las 10", now)
    attempts = []

    async def llm(phone, text):
        return "Preferencia registrada; visita pendiente de confirmar."

    async def sender(phone, text):
        attempts.append(text)
        if len(attempts) == 1:
            return {"success": False, "http_status": 429, "retry_after": 60}
        return {"success": True, "http_status": 200, "provider_message_id": "after-429"}

    first = run(queue.process_one_batch(
        db, worker_id="test-429-1", llm=llm, sender=sender,
        now=now + timedelta(seconds=20),
    ))
    assert first["state"] == queue.ST_FAILED_RETRYABLE
    assert first["last_error"] == "http_429"
    assert first.get("active_conversation_key") is None
    assert first.get("delivery_sealed_at") is not None
    second = run(queue.process_one_batch(
        db, worker_id="test-429-2", llm=llm, sender=sender,
        now=now + timedelta(seconds=95),
    ))
    assert second["state"] == queue.ST_RESPONDED
    assert second["outbound_provider_message_id"] == "after-429"
    assert len(attempts) == 2


def test_429_retry_rejects_new_inbound_during_new_generation(sandbox, monkeypatch):
    """An earlier 429 must not defeat the fresh snapshot fence."""
    db = sandbox
    now = datetime.now(timezone.utc)
    inbound(db, "original-429", "me interesa", now)
    calls = []
    async def llm(phone, text):
        if "nuevo dato" not in text and len(calls) == 1:
            inbound(db, "late-429", "nuevo dato",
                    now + timedelta(seconds=90))
        return "Respuesta " + text

    async def sender(phone, text):
        calls.append(text)
        if len(calls) == 1:
            return {"success": False, "http_status": 429, "retry_after": 60}
        return {"success": True, "http_status": 200, "provider_message_id": "retry-429"}

    first = run(queue.process_one_batch(
        db, worker_id="before", llm=llm, sender=sender,
        now=now + timedelta(seconds=20),
    ))
    assert first["state"] == queue.ST_FAILED_RETRYABLE
    # The second invocation can regenerate after the new inbound. It must not
    # send the stale content if the producer attaches a new message.
    second = run(queue.process_one_batch(
        db, worker_id="after", llm=llm, sender=sender,
        now=now + timedelta(seconds=95),
    ))
    assert second["state"] in {queue.ST_RESPONDED, queue.ST_FAILED_RETRYABLE}
    if second["state"] == queue.ST_RESPONDED:
        assert "nuevo dato" not in calls[-1] or len(calls) == 2
