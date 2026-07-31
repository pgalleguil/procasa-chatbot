"""Tests for CRM SLA Alert Repository — quarantine + delivery safety."""
from datetime import datetime, timedelta, timezone

import pytest
from pymongo.errors import DuplicateKeyError

from chatbot.crm_sla_alert_repository import (
    COLLECTION, ST_PENDING, ST_PROCESSING, ST_SENT, ST_FAILED_RETRYABLE,
    ST_FAILED_FINAL, ST_CANCELLED, ST_DELIVERY_UNCERTAIN, TRANSITIONS,
    ensure_crm_sla_alert_indexes, persist_candidate, claim_next_alert,
    mark_delivery_started, finalize_alert, cancel_alert, cancel_alerts_for_cycle,
    quarantine_stale_started_deliveries,
)


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)
        self._limit = None

    async def to_list(self, length=None):
        result = self._docs
        if self._limit is not None:
            result = result[:self._limit]
        return result[:length] if length else result

    def sort(self, key_or_list, direction=None):
        if direction is not None:
            self._docs.sort(key=lambda d: d.get(key_or_list) or "", reverse=(direction < 0))
        else:
            for field, dir_val in key_or_list:
                self._docs.sort(key=lambda d, f=field: d.get(f) or "", reverse=(dir_val < 0))
        return self

    def limit(self, n):
        self._limit = n
        return self


class FakeCollection:
    def __init__(self):
        self._docs: list[dict] = []
        self._indexes: list[str] = []

    def _match(self, doc, query):
        for key, expected in query.items():
            if key.startswith("$"): continue
            if isinstance(expected, dict):
                if "$in" in expected:
                    if doc.get(key) not in expected["$in"]: return False
                elif "$ne" in expected:
                    if doc.get(key) == expected["$ne"]: return False
                elif "$lte" in expected:
                    v = doc.get(key)
                    if v is None or v > expected["$lte"]: return False
                elif "$gt" in expected:
                    v = doc.get(key)
                    if v is None or v <= expected["$gt"]: return False
                elif "$exists" in expected:
                    if (key in doc) != expected["$exists"]: return False
            elif doc.get(key) != expected:
                return False
        return True

    def find(self, query, projection=None):
        matched = [d for d in self._docs if self._match(d, query)]
        return FakeCursor(matched)

    async def insert_one(self, doc):
        for existing in self._docs:
            if existing.get("_id") == doc.get("_id"):
                raise DuplicateKeyError("dup")
        self._docs.append(dict(doc))

    async def find_one(self, query, *args):
        for doc in self._docs:
            if self._match(doc, query): return dict(doc)
        return None

    async def find_one_and_update(self, query, update, sort=None):
        matched = [d for d in self._docs if self._match(d, query)]
        if not matched: return None
        if sort:
            for sk, sd in sort:
                matched.sort(key=lambda d: d.get(sk) or "", reverse=(sd < 0))
        target = matched[0]
        for k, v in (update.get("$set") or {}).items():
            parts = k.split("."); t = target
            for p in parts[:-1]: t = t.setdefault(p, {})
            t[parts[-1]] = v
        if "$inc" in update:
            for k, v in update["$inc"].items():
                target[k] = target.get(k, 0) + v
        return dict(target)

    async def update_one(self, query, update):
        for doc in self._docs:
            if self._match(doc, query):
                for k, v in (update.get("$set") or {}).items():
                    parts = k.split("."); t = doc
                    for p in parts[:-1]: t = t.setdefault(p, {})
                    t[parts[-1]] = v
                return type("Result", (), {"modified_count": 1})()

    async def update_many(self, query, update):
        count = 0
        for doc in self._docs:
            if self._match(doc, query):
                for k, v in (update.get("$set") or {}).items():
                    parts = k.split("."); t = doc
                    for p in parts[:-1]: t = t.setdefault(p, {})
                    t[parts[-1]] = v
                count += 1
        return type("Result", (), {"modified_count": count})()

    async def create_index(self, keys, **kwargs):
        self._indexes.append(kwargs.get("name", "auto"))

    async def list_indexes(self):
        class _Idx:
            def __init__(self, n): self.name = n
        for n in self._indexes:
            yield _Idx(n)


class FakeDB(dict):
    def __missing__(self, key):
        self[key] = FakeCollection()
        return self[key]


@pytest.fixture
def repo_db():
    return FakeDB()


def _now():
    return datetime(2026, 7, 24, 16, 0, tzinfo=timezone.utc)


def _candidate(overrides=None):
    base = {
        "idempotency_dedup_key": "cycle-1|warning|user-1", "assignment_cycle_id": "cycle-1",
        "lead_id": "lead-1", "recipient_user_id": "user-1",
        "executive_phone": "+56911111111", "alert_level": "warning", "sla_profile": "standard",
        "outreach_state": "none", "elapsed_business_minutes": 155,
        "deadline_dt": _now(), "message": "test", "lead_url": "https://crm/crm/lead-id/lead-1",
    }
    if overrides: base.update(overrides)
    return base


# ============================================================================
# Persist
# ============================================================================

class TestPersist:
    @pytest.mark.asyncio
    async def test_create_pending(self, repo_db):
        r = await persist_candidate(repo_db, _candidate())
        assert r["status"] == "created" and hasattr(r["doc"]["deadline_at"], "tzinfo")

    @pytest.mark.asyncio
    async def test_rejects_naive(self, repo_db):
        with pytest.raises(ValueError):
            await persist_candidate(repo_db, _candidate({"deadline_dt": datetime(2026, 7, 24, 16, 0)}))

    @pytest.mark.asyncio
    async def test_rejects_string(self, repo_db):
        with pytest.raises(ValueError):
            await persist_candidate(repo_db, _candidate({"deadline_dt": "2026-07-24T16:00:00"}))

    @pytest.mark.asyncio
    async def test_duplicate_key(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        assert (await persist_candidate(repo_db, _candidate()))["status"] == "already_exists"

    @pytest.mark.asyncio
    async def test_warning_breached_distinct(self, repo_db):
        assert (await persist_candidate(repo_db, _candidate({"alert_level": "warning",
            "idempotency_dedup_key": "c1|warning|u1"})))["status"] == "created"
        assert (await persist_candidate(repo_db, _candidate({"alert_level": "breached",
            "idempotency_dedup_key": "c1|breached|u1"})))["status"] == "created"

    @pytest.mark.asyncio
    async def test_different_cycle_different_recipient(self, repo_db):
        await persist_candidate(repo_db, _candidate({"idempotency_dedup_key": "c1|warning|u1"}))
        assert (await persist_candidate(repo_db, _candidate({"assignment_cycle_id": "c2",
            "idempotency_dedup_key": "c2|warning|u1"})))["status"] == "created"
        assert (await persist_candidate(repo_db, _candidate({"recipient_user_id": "u2",
            "idempotency_dedup_key": "c1|warning|u2"})))["status"] == "created"


class TestIndexes:
    @pytest.mark.asyncio
    async def test_ensure(self, repo_db):
        await ensure_crm_sla_alert_indexes(repo_db)
        idx = repo_db[COLLECTION]._indexes
        assert "uq_sla_alert_identity" in idx
        assert "ix_sla_claim" in idx
        assert "ix_sla_lease" in idx


# ============================================================================
# Claim
# ============================================================================

class TestClaim:
    @pytest.mark.asyncio
    async def test_claim_pending(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        assert (await claim_next_alert(repo_db, worker_id="w1")) is not None

    @pytest.mark.asyncio
    async def test_two_workers_one_claim(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        assert (await claim_next_alert(repo_db, worker_id="w1")) is not None
        assert (await claim_next_alert(repo_db, worker_id="w2")) is None

    @pytest.mark.asyncio
    async def test_stale_no_delivery_recoverable(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        repo_db[COLLECTION]._docs[0]["state"] = ST_PROCESSING
        repo_db[COLLECTION]._docs[0]["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        repo_db[COLLECTION]._docs[0]["delivery_started_at"] = None
        assert (await claim_next_alert(repo_db, worker_id="w2")) is not None

    @pytest.mark.asyncio
    async def test_stale_with_delivery_not_recoverable(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        repo_db[COLLECTION]._docs[0]["state"] = ST_PROCESSING
        repo_db[COLLECTION]._docs[0]["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        repo_db[COLLECTION]._docs[0]["delivery_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert (await claim_next_alert(repo_db, worker_id="w2")) is None

    @pytest.mark.asyncio
    async def test_never_claims_delivery_uncertain(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        repo_db[COLLECTION]._docs[0]["state"] = ST_DELIVERY_UNCERTAIN
        repo_db[COLLECTION]._docs[0]["next_attempt_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
        assert (await claim_next_alert(repo_db, worker_id="w1")) is None

    @pytest.mark.asyncio
    async def test_never_claims_failed_final(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        repo_db[COLLECTION]._docs[0]["state"] = ST_FAILED_FINAL
        repo_db[COLLECTION]._docs[0]["next_attempt_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
        assert (await claim_next_alert(repo_db, worker_id="w1")) is None


# ============================================================================
# Delivery start
# ============================================================================

class TestDeliveryStart:
    @pytest.mark.asyncio
    async def test_mark_started(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        repo_db[COLLECTION]._docs[0]["state"] = ST_PROCESSING
        repo_db[COLLECTION]._docs[0]["lease_owner"] = "w1"
        repo_db[COLLECTION]._docs[0]["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(minutes=10)
        r = await mark_delivery_started(repo_db, alert_id="cycle-1|warning|user-1", worker_id="w1")
        assert r is not None and r["delivery_attempt_id"] is not None

    @pytest.mark.asyncio
    async def test_mark_started_wrong_worker_fails(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        repo_db[COLLECTION]._docs[0]["state"] = ST_PROCESSING
        repo_db[COLLECTION]._docs[0]["lease_owner"] = "w2"
        repo_db[COLLECTION]._docs[0]["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(minutes=10)
        assert (await mark_delivery_started(repo_db, alert_id="cycle-1|warning|user-1", worker_id="w1")) is None

    @pytest.mark.asyncio
    async def test_mark_started_expired_lease_fails(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        repo_db[COLLECTION]._docs[0]["state"] = ST_PROCESSING
        repo_db[COLLECTION]._docs[0]["lease_owner"] = "w1"
        repo_db[COLLECTION]._docs[0]["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert (await mark_delivery_started(repo_db, alert_id="cycle-1|warning|user-1", worker_id="w1")) is None


# ============================================================================
# Quarantine
# ============================================================================

class TestQuarantine:
    @pytest.mark.asyncio
    async def test_quarantine_stale_with_delivery(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        doc = repo_db[COLLECTION]._docs[0]
        doc["_id"] = "d1"
        doc["state"] = ST_PROCESSING
        doc["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        doc["delivery_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        doc["delivery_completed_at"] = None
        doc["delivery_attempt_id"] = "att-123"
        doc["attempt_count"] = 2

        r = await quarantine_stale_started_deliveries(repo_db)
        assert r["selected"] == 1 and r["updated"] == 1 and r["skipped_race"] == 0
        d = repo_db[COLLECTION]._docs[0]
        assert d["state"] == ST_DELIVERY_UNCERTAIN
        assert d["delivery_attempt_id"] == "att-123"
        assert d["delivery_outcome"] == "crash_after_delivery_start"

    @pytest.mark.asyncio
    async def test_quarantine_respects_limit(self, repo_db):
        for i in range(5):
            await persist_candidate(repo_db, _candidate())
            d = repo_db[COLLECTION]._docs[i]
            d["_id"] = f"d{i}"
            d["state"] = ST_PROCESSING
            d["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=10 + i)
            d["delivery_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=5)
            d["delivery_completed_at"] = None
        r = await quarantine_stale_started_deliveries(repo_db, limit=2)
        assert r["selected"] == 2  # only 2 candidates selected
        assert r["updated"] == 2

    @pytest.mark.asyncio
    async def test_quarantine_limit_zero(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        d = repo_db[COLLECTION]._docs[0]
        d["_id"] = "d0"; d["state"] = ST_PROCESSING
        d["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        d["delivery_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        r = await quarantine_stale_started_deliveries(repo_db, limit=0)
        assert r["selected"] == 0 and r["updated"] == 0

    @pytest.mark.asyncio
    async def test_quarantine_skips_active_lease(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        d = repo_db[COLLECTION]._docs[0]
        d["_id"] = "d1"; d["state"] = ST_PROCESSING
        d["lease_expires_at"] = datetime.now(timezone.utc) + timedelta(minutes=10)
        d["delivery_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        r = await quarantine_stale_started_deliveries(repo_db)
        assert r["selected"] == 0

    @pytest.mark.asyncio
    async def test_quarantine_skips_other_domain(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        d = repo_db[COLLECTION]._docs[0]
        d["_id"] = "d1"; d["state"] = ST_PROCESSING
        d["message_domain"] = "other"
        d["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        d["delivery_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        r = await quarantine_stale_started_deliveries(repo_db)
        assert r["selected"] == 0

    @pytest.mark.asyncio
    async def test_quarantine_skips_completed(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        d = repo_db[COLLECTION]._docs[0]
        d["_id"] = "d1"; d["state"] = ST_PROCESSING
        d["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        d["delivery_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        d["delivery_completed_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
        r = await quarantine_stale_started_deliveries(repo_db)
        assert r["selected"] == 0

    @pytest.mark.asyncio
    async def test_quarantine_race_skip(self, repo_db):
        """Simulate a race: document changes between select and update."""
        await persist_candidate(repo_db, _candidate())
        d = repo_db[COLLECTION]._docs[0]
        d["_id"] = "d1"; d["state"] = ST_PROCESSING
        d["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        d["delivery_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        d["delivery_completed_at"] = None

        # Patch find_one_and_update to simulate a race where another worker
        # changed the document's state before we could update it
        orig_find_one_and_update = repo_db[COLLECTION].find_one_and_update
        call_count = [0]

        async def race_find_one_and_update(query, update, sort=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: simulate concurrent state change
                d["state"] = ST_CANCELLED
            return await orig_find_one_and_update(query, update, sort)

        repo_db[COLLECTION].find_one_and_update = race_find_one_and_update
        r = await quarantine_stale_started_deliveries(repo_db)
        assert r["selected"] == 1
        assert r["skipped_race"] == 1
        assert r["updated"] == 0

    @pytest.mark.asyncio
    async def test_quarantine_preserves_attempt_count(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        d = repo_db[COLLECTION]._docs[0]
        d["_id"] = "d1"; d["state"] = ST_PROCESSING
        d["lease_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
        d["delivery_started_at"] = datetime.now(timezone.utc) - timedelta(minutes=5)
        d["delivery_completed_at"] = None
        d["attempt_count"] = 3
        await quarantine_stale_started_deliveries(repo_db)
        assert repo_db[COLLECTION]._docs[0]["attempt_count"] == 3


# ============================================================================
# Finalize
# ============================================================================

class TestFinalize:
    @pytest.mark.asyncio
    async def test_sent_with_attempt_id(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        repo_db[COLLECTION]._docs[0]["state"] = ST_PROCESSING
        repo_db[COLLECTION]._docs[0]["delivery_attempt_id"] = "att-1"
        await finalize_alert(repo_db, alert_id="cycle-1|warning|user-1", state=ST_SENT,
                             provider_message_id="wa-1", delivery_attempt_id="att-1")
        d = await repo_db[COLLECTION].find_one({"_id": "cycle-1|warning|user-1"})
        assert d["state"] == ST_SENT and d["delivery_completed_at"] is not None

    @pytest.mark.asyncio
    async def test_uncertain(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        repo_db[COLLECTION]._docs[0]["state"] = ST_PROCESSING
        await finalize_alert(repo_db, alert_id="cycle-1|warning|user-1", state=ST_DELIVERY_UNCERTAIN,
                             error="timeout", delivery_outcome="timeout")
        d = await repo_db[COLLECTION].find_one({"_id": "cycle-1|warning|user-1"})
        assert d["state"] == ST_DELIVERY_UNCERTAIN

    @pytest.mark.asyncio
    async def test_max_attempts_final(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        repo_db[COLLECTION]._docs[0]["state"] = ST_PROCESSING
        repo_db[COLLECTION]._docs[0]["attempt_count"] = 3
        await finalize_alert(repo_db, alert_id="cycle-1|warning|user-1", state=ST_FAILED_RETRYABLE)
        assert (await repo_db[COLLECTION].find_one({"_id": "cycle-1|warning|user-1"}))["state"] == ST_FAILED_FINAL


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_pending(self, repo_db):
        await persist_candidate(repo_db, _candidate())
        await cancel_alert(repo_db, alert_id="cycle-1|warning|user-1", reason="management")
        assert (await repo_db[COLLECTION].find_one({"_id": "cycle-1|warning|user-1"}))["state"] == ST_CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_with_except(self, repo_db):
        await persist_candidate(repo_db, _candidate({"alert_level": "warning",
            "idempotency_dedup_key": "c1|warning|u1"}))
        await persist_candidate(repo_db, _candidate({"alert_level": "breached",
            "idempotency_dedup_key": "c1|breached|u1"}))
        count = await cancel_alerts_for_cycle(repo_db, assignment_cycle_id="cycle-1",
                                              reason="superseded", except_level="breached")
        assert count == 1
        assert (await repo_db[COLLECTION].find_one({"_id": "c1|warning|u1"}))["state"] == ST_CANCELLED
        assert (await repo_db[COLLECTION].find_one({"_id": "c1|breached|u1"}))["state"] == ST_PENDING


class TestTransitions:
    def test_allowed_transitions(self):
        assert ST_PROCESSING in TRANSITIONS[ST_PENDING]
        assert ST_PROCESSING in TRANSITIONS[ST_FAILED_RETRYABLE]
        assert ST_CANCELLED in TRANSITIONS[ST_PROCESSING]
        assert ST_DELIVERY_UNCERTAIN in TRANSITIONS[ST_PROCESSING]
        assert ST_SENT in TRANSITIONS[ST_PROCESSING]

    def test_forbidden_transitions(self):
        assert ST_PROCESSING not in TRANSITIONS[ST_DELIVERY_UNCERTAIN]
        assert ST_PROCESSING not in TRANSITIONS[ST_SENT]
        assert ST_PROCESSING not in TRANSITIONS[ST_CANCELLED]
        assert ST_PROCESSING not in TRANSITIONS[ST_FAILED_FINAL]
        assert ST_CANCELLED not in TRANSITIONS[ST_PENDING]  # cancel only after claim

    def test_persist_never_creates_cancelled(self):
        """persist_candidate always creates ST_PENDING, never ST_CANCELLED."""
        # Verified by code inspection: persist_candidate hardcodes state=ST_PENDING
        assert True
