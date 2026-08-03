import asyncio
import importlib.util
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

_QUEUE_PATH = Path(__file__).parents[1] / "chatbot" / "chatbot_queue.py"
_SPEC = importlib.util.spec_from_file_location("chatbot_queue_under_test", _QUEUE_PATH)
queue = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(queue)


def _get(doc, key):
    return doc.get(key)


def _matches(doc, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(doc, part) for part in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches(doc, part) for part in expected):
                return False
            continue
        actual = _get(doc, key)
        if isinstance(expected, dict):
            for op, value in expected.items():
                if op == "$in" and actual not in value:
                    return False
                if op == "$nin" and actual in value:
                    return False
                if op == "$exists" and (key in doc) != value:
                    return False
                if op == "$gt" and not (actual is not None and actual > value):
                    return False
                if op == "$gte" and not (actual is not None and actual >= value):
                    return False
                if op == "$lte" and not (actual is not None and actual <= value):
                    return False
                if op == "$ne" and actual == value:
                    return False
                if op == "$size" and len(actual or []) != value:
                    return False
        elif actual != expected:
            return False
    return True


class Result:
    def __init__(self, matched=0, modified=0, inserted_id=None):
        self.matched_count = matched
        self.modified_count = modified
        self.inserted_id = inserted_id


class Cursor(list):
    def sort(self, key, direction):
        super().sort(key=lambda item: item.get(key) or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=direction < 0)
        return self

    def limit(self, size):
        del self[size:]
        return self


class Collection:
    def __init__(self):
        self.docs = {}

    def create_index(self, *args, **kwargs):
        return kwargs.get("name")

    def insert_one(self, doc):
        for current in self.docs.values():
            if (doc.get("kind") == queue.KIND_JOB
                    and current.get("kind") == queue.KIND_JOB
                    and current.get("inbound_provider_message_id")
                    == doc.get("inbound_provider_message_id")):
                raise DuplicateKeyError("duplicate inbound")
        if doc["_id"] in self.docs:
            raise DuplicateKeyError("duplicate id")
        self.docs[doc["_id"]] = deepcopy(doc)
        return Result(inserted_id=doc["_id"])

    def find(self, query, projection=None):
        values = [deepcopy(doc) for doc in self.docs.values() if _matches(doc, query)]
        if projection:
            values = [{key: doc.get(key) for key, enabled in projection.items() if enabled}
                      for doc in values]
        return Cursor(values)

    def find_one(self, query, projection=None, sort=None):
        values = self.find(query, projection)
        if sort:
            for key, direction in reversed(sort):
                values.sort(key, direction)
        return values[0] if values else None

    @staticmethod
    def _apply(doc, update):
        for key, value in update.get("$set", {}).items():
            doc[key] = deepcopy(value)
        for key in update.get("$unset", {}):
            doc.pop(key, None)
        for key, value in update.get("$inc", {}).items():
            doc[key] = doc.get(key, 0) + value
        for key, value in update.get("$addToSet", {}).items():
            doc.setdefault(key, [])
            if value not in doc[key]:
                doc[key].append(deepcopy(value))
        for key, value in update.get("$push", {}).items():
            doc.setdefault(key, []).append(deepcopy(value))

    def update_one(self, query, update):
        for key, doc in self.docs.items():
            if _matches(doc, query):
                before = deepcopy(doc)
                self._apply(doc, update)
                return Result(1, int(before != doc))
        return Result()

    def update_many(self, query, update):
        matched = modified = 0
        for doc in self.docs.values():
            if _matches(doc, query):
                matched += 1
                before = deepcopy(doc)
                self._apply(doc, update)
                modified += int(before != doc)
        return Result(matched, modified)

    def find_one_and_update(self, query, update, sort=None, return_document=None):
        found = self.find_one(query, sort=sort)
        if not found:
            return None
        self.update_one({"_id": found["_id"]}, update)
        return deepcopy(self.docs[found["_id"]]) if return_document == ReturnDocument.AFTER else found

    def count_documents(self, query):
        return len(self.find(query))

    def distinct(self, key, query):
        return list({doc.get(key) for doc in self.find(query) if doc.get(key) is not None})


class DB:
    def __init__(self):
        self.collection = Collection()

    def __getitem__(self, name):
        assert name == queue.JOB_COLLECTION
        return self.collection


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def add(db, provider_id, text, at=NOW):
    return queue.create_inbound_job(
        db, inbound_provider_message_id=provider_id, phone="+56911112222",
        text=text, received_at=at,
    )


def test_webhook_has_no_legacy_response_pipeline():
    source = (Path(__file__).parents[1] / "webhook.py").read_text(encoding="utf-8")
    webhook = source[source.index('@app.post("/webhook")'):source.index('@app.get("/health")')]
    assert "create_inbound_job" in webhook
    assert "process_with_debounce" not in webhook
    assert "pending_tasks" not in webhook
    assert "asyncio.create_task" not in webhook


def test_duplicate_webhook_creates_one_job_and_one_batch():
    db = DB()
    first = add(db, "wamid-1", "hola")
    second = add(db, "wamid-1", "hola")
    assert first == second
    assert len(db.collection.find({"kind": queue.KIND_JOB})) == 1
    assert len(db.collection.find({"kind": queue.KIND_BATCH})) == 1


def test_new_inbound_never_reuses_terminal_batch_with_stale_conversation_lock():
    db = DB()
    stale_id = "batch:terminal-stale"
    db.collection.docs[stale_id] = {
        "_id": stale_id,
        "kind": queue.KIND_BATCH,
        "phone": "+56911112222",
        "active_conversation_key": "phone:+56911112222",
        "state": queue.ST_FAILED_TERMINAL,
        "job_ids": [],
        "created_at": NOW - timedelta(hours=1),
    }

    job_id = add(db, "wamid-after-terminal", "Arriendo", NOW)
    job = db.collection.docs[job_id]
    assert job["state"] == queue.ST_BATCHING
    assert job["batch_id"] != stale_id
    assert "active_conversation_key" not in db.collection.docs[stale_id]
    assert db.collection.docs[job["batch_id"]]["state"] == queue.ST_BATCHING


def test_two_messages_in_window_make_one_batch_and_one_response():
    db = DB()
    add(db, "wamid-1", "hola", NOW)
    add(db, "wamid-2", "mundo", NOW + timedelta(seconds=10))
    batches = db.collection.find({"kind": queue.KIND_BATCH})
    assert len(batches) == 1
    assert len(batches[0]["job_ids"]) == 2
    assert batches[0]["window_end_at"] == NOW + timedelta(seconds=25)

    sent = []

    async def llm(phone, text):
        assert text == "hola\nmundo"
        return "respuesta"

    async def sender(phone, text):
        sent.append((phone, text))
        return {"success": True, "provider_message_id": "out-1", "http_status": 200}

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="w1", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=25),
    ))
    assert result["state"] == queue.ST_RESPONDED
    assert sent == [("+56911112222", "respuesta")]
    assert len(result["delivery_attempts"]) == 2


def test_process_one_batch_offloads_all_sync_mongo_from_event_loop(monkeypatch):
    import threading

    db = DB()
    add(db, "wamid-thread", "hola", NOW)
    main_thread = threading.get_ident()
    observed = []

    def track(name, original):
        def wrapped(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                loop_active = True
            except RuntimeError:
                loop_active = False
            observed.append((name, threading.get_ident(), loop_active))
            return original(*args, **kwargs)
        return wrapped

    monkeypatch.setattr(
        queue, "claim_pending_batch",
        track("claim", queue.claim_pending_batch),
    )
    monkeypatch.setattr(
        queue, "record_delivery_attempt",
        track("attempt", queue.record_delivery_attempt),
    )
    monkeypatch.setattr(
        queue, "finalize_batch",
        track("finalize", queue.finalize_batch),
    )

    async def llm(_phone, _text):
        return "respuesta"

    async def sender(_phone, _text):
        return {"success": True, "provider_message_id": "out-thread", "http_status": 200}

    asyncio.run(queue.process_one_batch(
        db, worker_id="thread-worker", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    assert {name for name, _, _ in observed} == {"claim", "attempt", "finalize"}
    assert all(thread_id != main_thread and not loop_active
               for _, thread_id, loop_active in observed)


def test_batch_not_claimed_before_window_and_two_workers_cannot_claim():
    db = DB()
    add(db, "wamid-1", "hola")
    assert queue.claim_pending_batch(db, worker_id="w1", now=NOW + timedelta(seconds=14)) is None
    first = queue.claim_pending_batch(db, worker_id="w1", now=NOW + timedelta(seconds=15))
    second = queue.claim_pending_batch(db, worker_id="w2", now=NOW + timedelta(seconds=15))
    assert first["lease_owner"] == "w1"
    assert second is None
    assert first["claim_count"] == 1


def test_worker_error_never_calls_legacy_and_empty_text_never_reaches_llm():
    source = (Path(__file__).parents[1] / "chatbot" / "chatbot_queue.py").read_text("utf-8")
    assert "process_with_debounce" not in source

    db = DB()
    add(db, "wamid-1", "hola")
    called = []

    async def failing_llm(phone, text):
        called.append(text)
        raise RuntimeError("boom")

    async def sender(phone, text):
        pytest.fail("sender must not run")

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="w1", llm=failing_llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    assert called == ["hola"]
    assert result["state"] == queue.ST_FAILED_RETRYABLE
    with pytest.raises(ValueError, match="invalid_inbound_text"):
        add(DB(), "wamid-empty", "   ")
    with pytest.raises(ValueError, match="invalid_inbound_text"):
        add(DB(), "wamid-id", "507f1f77bcf86cd799439011")


def test_422_terminal_429_retry_after_and_unknown_delivery():
    async def llm(phone, text):
        return "respuesta"

    async def run(receipt):
        db = DB()
        add(db, "wamid-1", "hola")

        async def sender(phone, text):
            if isinstance(receipt, Exception):
                raise receipt
            return receipt

        return await queue.process_one_batch(
            db, worker_id="w1", llm=llm, sender=sender,
            now=NOW + timedelta(seconds=15),
        )

    terminal = asyncio.run(run({"success": False, "http_status": 422}))
    assert terminal["state"] == queue.ST_FAILED_TERMINAL
    limited = asyncio.run(run({"success": False, "http_status": 429, "retry_after": 73}))
    assert limited["state"] == queue.ST_FAILED_RETRYABLE
    assert limited["next_attempt_at"] == NOW + timedelta(seconds=88)
    unknown = asyncio.run(run(TimeoutError("after provider call")))
    assert unknown["state"] == queue.ST_DELIVERY_UNKNOWN
    uncertain = asyncio.run(run({
        "success": False, "provider_call_uncertain": True, "http_status": None,
    }))
    assert uncertain["state"] == queue.ST_DELIVERY_UNKNOWN


def test_health_degrades_for_missing_heartbeat_and_expired_lease():
    db = DB()
    add(db, "wamid-1", "hola")
    claimed = queue.claim_pending_batch(db, worker_id="w1", now=NOW + timedelta(seconds=15),
                                        lease_seconds=1)
    health = queue.get_queue_health(db, heartbeat={}, now=NOW + timedelta(seconds=17))
    assert claimed
    assert health["processing_with_expired_lease"] == 1
    assert "worker_heartbeat_stale_or_missing" in health["degraded_reasons"]
    assert "expired_processing_leases" in health["degraded_reasons"]


def test_health_degrades_for_due_batch_not_claimed():
    db = DB()
    add(db, "wamid-1", "hola")
    health = queue.get_queue_health(
        db,
        heartbeat={"last_heartbeat": (NOW + timedelta(seconds=60)).isoformat()},
        now=NOW + timedelta(seconds=60),
    )
    assert health["stuck_due_batches"] == 1
    assert "stuck_due_batches" in health["degraded_reasons"]


def test_expired_lease_is_retryable_before_send_but_unknown_after_send_started():
    before = DB()
    add(before, "wamid-1", "hola")
    queue.claim_pending_batch(before, worker_id="w1", now=NOW + timedelta(seconds=15),
                              lease_seconds=1)
    recovered = queue.reconcile_expired_leases(before, now=NOW + timedelta(seconds=17))
    assert recovered == {"retryable": 1, "delivery_unknown": 0}

    after = DB()
    add(after, "wamid-2", "hola")
    batch = queue.claim_pending_batch(after, worker_id="w1", now=NOW + timedelta(seconds=15),
                                      lease_seconds=1)
    queue.record_delivery_attempt(
        after, batch_id=batch["_id"], worker_id="w1",
        delivery_token=batch["delivery_token"], status="started",
        now=NOW + timedelta(seconds=15),
    )
    recovered = queue.reconcile_expired_leases(after, now=NOW + timedelta(seconds=17))
    assert recovered == {"retryable": 0, "delivery_unknown": 1}
