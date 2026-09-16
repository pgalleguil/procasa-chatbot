import asyncio
import importlib.util
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import mongomock
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


def test_inbound_enqueue_records_stage_timings_and_claim_observability(caplog):
    db = DB()
    timings = {"user_lookup_ms": 0.0, "log_event_ms": 0.0}
    job_id = queue.create_inbound_job(
        db, inbound_provider_message_id="wamid-observable", phone="+56911112222",
        text="hola", received_at=NOW, ingress_timings=timings,
    )
    assert job_id
    assert all(
        key in timings
        for key in (
            "user_lookup_ms",
            "lead_lookup_ms",
            "conversation_resolution_ms",
            "job_insert_ms",
            "batch_attach_ms",
        )
    )
    batch = db.collection.find_one({"kind": queue.KIND_BATCH})
    assert batch["job_ids"] == [job_id]

    async def llm(phone, text):
        return "respuesta"

    async def sender(phone, text):
        return {"success": True, "provider_message_id": "out-observable", "http_status": 200}

    with caplog.at_level("INFO", logger=queue.logger.name):
        result = asyncio.run(queue.process_one_batch(
            db, worker_id="w-observable", llm=llm, sender=sender,
            now=NOW + timedelta(seconds=15),
        ))

    assert result["state"] == queue.ST_RESPONDED
    assert any("[CHATBOT_BATCH_CLAIMED]" in record.message for record in caplog.records)


def test_webhook_acknowledges_job_already_attached_by_worker_race():
    class RaceCollection(Collection):
        def update_one(self, query, update):
            if query.get("kind") == queue.KIND_JOB and query.get("state") == queue.ST_RECEIVED:
                job = self.docs[query["_id"]]
                batch = next(doc for doc in self.docs.values() if doc.get("kind") == queue.KIND_BATCH)
                job["state"] = queue.ST_BATCHING
                job["batch_id"] = batch["_id"]
                job["updated_at"] = update["$set"]["updated_at"]
                batch.setdefault("job_ids", []).append(job["_id"])
                batch["conversation_sequence"] = batch.get("conversation_sequence", 0) + 1
                return Result(1, 0)
            return super().update_one(query, update)

    db = DB()
    db.collection = RaceCollection()
    job_id = add(db, "wamid-worker-race", "hola")
    job = db.collection.docs[job_id]
    assert job["state"] == queue.ST_BATCHING
    assert job["batch_id"]


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
    assert batches[0]["window_end_at"] == NOW + timedelta(seconds=15)

    sent = []

    async def llm(phone, text):
        assert text == "hola\nmundo"
        return "respuesta"

    async def sender(phone, text):
        sent.append((phone, text))
        return {"success": True, "provider_message_id": "out-1", "http_status": 200}

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="w1", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
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
    assert queue.claim_pending_batch(db, worker_id="w1", now=NOW + timedelta(seconds=4)) is None
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


@pytest.mark.parametrize(
    ("http_status", "expected_error"),
    [
        (401, "provider_authentication_failed_http_401"),
        (403, "provider_authorization_failed_http_403"),
    ],
)
def test_provider_auth_failures_are_terminal_and_never_retried(http_status, expected_error):
    db = DB()
    add(db, f"wamid-auth-{http_status}", "hola")
    sender_calls = []

    async def llm(_phone, _text):
        return "respuesta"

    async def sender(_phone, _text):
        sender_calls.append(http_status)
        return {"success": False, "http_status": http_status,
                "provider_message_id": None, "delivery_status": "failed"}

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="auth-worker", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    assert result["state"] == queue.ST_FAILED_TERMINAL
    assert result["last_error"] == expected_error
    assert result["next_attempt_at"] is None

    # Credential recovery must not resurrect the old customer-facing reply.
    async def recovered_sender(_phone, _text):
        return {"success": True, "provider_message_id": "late"}

    assert asyncio.run(queue.process_one_batch(
        db, worker_id="auth-worker-2", llm=llm,
        sender=recovered_sender,
        now=NOW + timedelta(hours=1),
    )) is None
    assert sender_calls == [http_status]


def test_401_auth_failure_is_observable_without_logging_credentials(caplog):
    db = DB()
    add(db, "wamid-auth-observable", "hola")
    secret = "do-not-log-provider-token"

    async def llm(_phone, _text):
        return "respuesta"

    async def sender(_phone, _text):
        return {"success": False, "http_status": 401,
                "provider_message_id": None, "delivery_status": "failed"}

    with caplog.at_level("ERROR", logger=queue.logger.name):
        result = asyncio.run(queue.process_one_batch(
            db, worker_id="auth-observable", llm=llm, sender=sender,
            now=NOW + timedelta(seconds=15),
        ))

    assert result["state"] == queue.ST_FAILED_TERMINAL
    messages = "\n".join(record.message for record in caplog.records)
    assert "[WHATSAPP_PROVIDER_AUTH_FAILURE]" in messages
    assert "http_status=401" in messages
    assert "provider_message_id=null" in messages
    assert secret not in messages


def test_429_retries_after_retry_after_and_can_succeed():
    db = DB()
    add(db, "wamid-rate-limit", "hola")
    sender_calls = []

    async def llm(_phone, _text):
        return "respuesta"

    async def sender(_phone, _text):
        sender_calls.append(1)
        if len(sender_calls) == 1:
            return {"success": False, "http_status": 429,
                    "retry_after": 30, "provider_message_id": None,
                    "delivery_status": "failed"}
        return {"success": True, "http_status": 200,
                "provider_message_id": "accepted-after-429",
                "delivery_status": "accepted"}

    first = asyncio.run(queue.process_one_batch(
        db, worker_id="rate-worker", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    assert first["state"] == queue.ST_FAILED_RETRYABLE
    assert first["last_error"] == "http_429"
    assert first["next_attempt_at"] == NOW + timedelta(seconds=45)

    assert asyncio.run(queue.process_one_batch(
        db, worker_id="rate-worker", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=44),
    )) is None
    second = asyncio.run(queue.process_one_batch(
        db, worker_id="rate-worker", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=46),
    ))
    assert second["state"] == queue.ST_RESPONDED
    assert sender_calls == [1, 1]


def test_network_uncertainty_is_delivery_unknown_and_never_blindly_retried():
    db = DB()
    add(db, "wamid-network-uncertain", "hola")
    sender_calls = []

    async def llm(_phone, _text):
        return "respuesta"

    async def sender(_phone, _text):
        sender_calls.append(1)
        raise TimeoutError("provider response uncertain")

    result = asyncio.run(queue.process_one_batch(
        db, worker_id="network-worker", llm=llm, sender=sender,
        now=NOW + timedelta(seconds=15),
    ))
    assert result["state"] == queue.ST_DELIVERY_UNKNOWN
    assert result["last_error"] == "send:TimeoutError"
    assert asyncio.run(queue.process_one_batch(
        db, worker_id="network-worker-2", llm=llm, sender=sender,
        now=NOW + timedelta(hours=1),
    )) is None
    assert sender_calls == [1]


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


def _runtime_test_db(name="runtime-control-tests"):
    from chatbot import runtime_control as rc
    database = mongomock.MongoClient()[name]
    rc.ensure_runtime_control(database, initial_mode=rc.V1_ACTIVE, now=NOW)
    queue.ensure_queue_indexes(database)
    from chatbot import turn_queue as tq
    tq.ensure_indexes(database)
    return database, rc, tq


def test_runtime_drain_routes_new_inbounds_and_fences_old_v1_before_provider():
    db, rc, tq = _runtime_test_db("runtime-drain")
    old_job_id = queue.create_inbound_job(
        db, inbound_provider_message_id="old-v1-inbound", phone="+56911112222",
        text="quiero verla", received_at=NOW,
    )
    old_batch = queue.claim_pending_batch(
        db, worker_id="v1-worker", now=NOW + timedelta(seconds=15),
    )
    assert old_batch and old_batch["pipeline_version"] == rc.V1
    assert rc.transition_runtime(
        db, to_mode=rc.DRAINING_TO_V2, expected_mode=rc.V1_ACTIVE,
        now=NOW + timedelta(seconds=16),
    )
    new = tq.persist_inbound(
        db, provider_message_id="new-v2-inbound", conversation_id="conv-1",
        lead_id="lead-1", phone="+56911112222", text="mañana", received_at=NOW,
        quiet_seconds=0,
    )
    new_job = db[tq.JOB_COLLECTION].find_one({"_id": new.job_id})
    assert new_job["pipeline_version"] == rc.V2
    assert db[tq.TURN_COLLECTION].find_one({"_id": new.turn_id})["state"] == tq.TURN_PENDING
    assert rc.worker_can_claim(db, old_batch) is True
    assert rc.worker_can_send(db, old_batch) is False
    assert rc.runtime_fence(db, old_batch, phase="send")["reason"] == "FENCED_OUT"
    assert db[queue.JOB_COLLECTION].find_one({"_id": old_job_id})["state"] == queue.ST_BATCHING
    assert db[queue.JOB_COLLECTION].find_one({"_id": old_job_id}).get("outbound_provider_message_id") is None


def test_fenced_v1_batch_is_recoverable_by_v2_and_activation_is_atomic():
    db, rc, tq = _runtime_test_db("runtime-migration")
    old_job_id = queue.create_inbound_job(
        db, inbound_provider_message_id="migrate-v1-inbound", phone="+56911112222",
        text="quiero verla mañana", received_at=NOW,
    )
    old_batch = queue.claim_pending_batch(db, worker_id="v1-worker", now=NOW + timedelta(seconds=15))
    rc.transition_runtime(db, to_mode=rc.DRAINING_TO_V2, expected_mode=rc.V1_ACTIVE, now=NOW + timedelta(seconds=16))
    migration = tq.migrate_legacy_batch_to_v2(
        db, batch_id=old_batch["_id"], now=NOW + timedelta(seconds=17),
    )
    assert migration["migrated"] is True
    assert db[tq.JOB_COLLECTION].count_documents({"pipeline_version": rc.V2}) == 1
    queue.finalize_batch(
        db, batch_id=old_batch["_id"], state=queue.ST_FAILED_TERMINAL,
        error="FENCED_OUT_RUNTIME_GENERATION", worker_id="v1-worker",
        delivery_token=old_batch["delivery_token"], now=NOW + timedelta(seconds=18),
    )
    active = rc.activate_after_drain(
        db, expected_drain_mode=rc.DRAINING_TO_V2, now=NOW + timedelta(seconds=19),
    )
    assert active and active["mode"] == rc.V2_ACTIVE
    migrated_turn = tq.claim_pending_turn(db, worker_id="v2-worker", now=NOW + timedelta(seconds=20))
    assert migrated_turn and migrated_turn["pipeline_version"] == rc.V2
    sent = []
    delivered = tq.deliver_claimed_turn(
        db, turn=migrated_turn, worker_id="v2-worker", response="continuidad",
        sender=lambda phone, text: sent.append((phone, text)) or {
            "success": True, "provider_message_id": "v2-out", "delivery_status": "accepted",
        }, now=NOW + timedelta(seconds=21),
    )
    assert delivered.sent is True
    assert len(sent) == 1
    assert db[queue.JOB_COLLECTION].find_one({"_id": old_job_id})["state"] == queue.ST_FAILED_TERMINAL


def test_runtime_accepted_and_delivery_unknown_work_never_migrate():
    from chatbot import runtime_control as rc
    accepted = rc.migration_decision({"outbound_provider_message_id": "out-1"})
    unknown = rc.migration_decision({"delivery_attempts": [{"status": "started"}]})
    assert accepted["eligible"] is False
    assert unknown["eligible"] is False


def test_runtime_rollback_drains_v2_then_routes_new_v1():
    db, rc, tq = _runtime_test_db("runtime-rollback")
    rc.transition_runtime(db, to_mode=rc.DRAINING_TO_V2, expected_mode=rc.V1_ACTIVE, now=NOW)
    rc.transition_runtime(db, to_mode=rc.V2_ACTIVE, expected_mode=rc.DRAINING_TO_V2, now=NOW + timedelta(seconds=1))
    v2 = tq.persist_inbound(
        db, provider_message_id="rollback-v2", conversation_id="rollback",
        lead_id="lead-r", phone="+56911112222", text="hola", received_at=NOW,
        quiet_seconds=0,
    )
    v2_turn = tq.claim_pending_turn(db, worker_id="v2-worker", now=NOW + timedelta(seconds=2))
    assert v2_turn
    tq.deliver_claimed_turn(
        db, turn=v2_turn, worker_id="v2-worker", response="respuesta",
        sender=lambda _phone, _text: {"success": True, "provider_message_id": "rollback-out"},
        now=NOW + timedelta(seconds=3),
    )
    draining = rc.transition_runtime(db, to_mode=rc.DRAINING_TO_V1, expected_mode=rc.V2_ACTIVE, now=NOW + timedelta(seconds=4))
    assert draining and draining["target"] == rc.V1
    v1_job = queue.create_inbound_job(
        db, inbound_provider_message_id="rollback-v1", phone="+56911112222",
        text="nueva consulta", received_at=NOW + timedelta(seconds=4),
    )
    v1_doc = db[queue.JOB_COLLECTION].find_one({"_id": v1_job})
    assert v1_doc["pipeline_version"] == rc.V1
    assert queue.claim_pending_batch(db, worker_id="v1-worker", now=NOW + timedelta(seconds=20)) is None
    active = rc.activate_after_drain(db, expected_drain_mode=rc.DRAINING_TO_V1, now=NOW + timedelta(seconds=21))
    assert active and active["mode"] == rc.V1_ACTIVE
    assert queue.claim_pending_batch(db, worker_id="v1-worker", now=NOW + timedelta(seconds=22))


def test_release_a_keeps_v1_active_and_does_not_admit_v2_sender():
    db, rc, tq = _runtime_test_db("release-a-v1")
    control = rc.get_runtime_control(db)
    assert control["mode"] == rc.V1_ACTIVE
    assert rc.route_new_inbound(db)["pipeline_version"] == rc.V1
    legacy_job = queue.create_inbound_job(
        db, inbound_provider_message_id="release-a-inbound", phone="+56911112222",
        text="consulta", received_at=NOW,
    )
    assert db[tq.JOB_COLLECTION].count_documents({"kind": tq.KIND_JOB}) == 0
    assert db[queue.JOB_COLLECTION].find_one({"_id": legacy_job})["pipeline_version"] == rc.V1
    assert tq.claim_pending_turn(db, worker_id="v2-disabled", now=NOW + timedelta(seconds=10)) is None


def test_provider_duplicate_is_idempotent_across_cutover_and_rollback():
    db, rc, tq = _runtime_test_db("cross-pipeline-idempotency")
    old_id = queue.create_inbound_job(
        db, inbound_provider_message_id="cross-pipeline-1", phone="+56911112222",
        text="original", received_at=NOW,
    )
    rc.transition_runtime(db, to_mode=rc.DRAINING_TO_V2, expected_mode=rc.V1_ACTIVE, now=NOW)
    duplicate_old = queue.create_inbound_job(
        db, inbound_provider_message_id="cross-pipeline-1", phone="+56911112222",
        text="duplicado", received_at=NOW + timedelta(seconds=1),
    )
    assert duplicate_old == old_id
    assert db[tq.JOB_COLLECTION].count_documents({"kind": tq.KIND_JOB}) == 0

    rc.transition_runtime(db, to_mode=rc.V2_ACTIVE, expected_mode=rc.DRAINING_TO_V2, now=NOW + timedelta(seconds=1))
    v2_result = tq.persist_inbound(
        db, provider_message_id="cross-pipeline-2", conversation_id="cross",
        lead_id="lead-cross", phone="+56911112222", text="v2 original",
        received_at=NOW, quiet_seconds=0,
    )
    rc.transition_runtime(db, to_mode=rc.DRAINING_TO_V1, expected_mode=rc.V2_ACTIVE, now=NOW + timedelta(seconds=2))
    duplicate_v2 = queue.create_inbound_job(
        db, inbound_provider_message_id="cross-pipeline-2", phone="+56911112222",
        text="duplicado v2", received_at=NOW + timedelta(seconds=3),
    )
    assert duplicate_v2 == v2_result.job_id
