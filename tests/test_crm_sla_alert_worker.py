"""Tests for CRM SLA Alert Worker â€” delivery safety + config."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from chatbot.crm_sla_alert_worker import process_one_alert, process_alerts_batch
from chatbot.crm_sla_alert_repository import (
    COLLECTION, ST_PENDING, ST_PROCESSING, ST_SENT, ST_FAILED_RETRYABLE,
    ST_DELIVERY_UNCERTAIN,
)
from chatbot.crm_sla_alert_sender import FakeSender, FailingSender, SenderResult
from chatbot.constants import CHILE_TZ


class FakeCursor:
    def __init__(self, docs): self._docs = list(docs)
    async def to_list(self, length=None): return self._docs[:length]


class FakeRepoCollection:
    def __init__(self, docs=None): self._docs: list[dict] = list(docs or [])
    def _match(self, doc, query):
        for key, expected in query.items():
            if key.startswith("$"): continue
            doc_val = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected:
                    if doc_val not in expected["$in"]: return False
                elif "$ne" in expected:
                    if doc_val == expected["$ne"]: return False
                elif "$lte" in expected:
                    if doc_val is None or doc_val > expected["$lte"]: return False
                elif "$gt" in expected:
                    if doc_val is None or doc_val <= expected["$gt"]: return False
                elif "$exists" in expected:
                    if (key in doc) != expected["$exists"]: return False
            elif doc_val != expected: return False
        return True

    async def find_one(self, query, *args):
        for d in self._docs:
            if self._match(d, query): return dict(d)
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
            for k, v in update["$inc"].items(): target[k] = target.get(k, 0) + v
        return dict(target)

    async def update_one(self, query, update):
        for d in self._docs:
            if self._match(d, query):
                for k, v in (update.get("$set") or {}).items():
                    parts = k.split("."); t = d
                    for p in parts[:-1]: t = t.setdefault(p, {})
                    t[parts[-1]] = v
                return type("R", (), {"modified_count": 1})()
    async def update_many(self, query, update):
        c = 0
        for d in self._docs:
            if self._match(d, query):
                for k, v in (update.get("$set") or {}).items():
                    parts = k.split("."); t = d
                    for p in parts[:-1]: t = t.setdefault(p, {})
                    t[parts[-1]] = v
                c += 1
        return type("R", (), {"modified_count": c})()


class ReadCollection:
    def __init__(self, docs=None): self._docs = list(docs or [])
    def find(self, query, projection=None):
        matched = self._docs
        for k, v in query.items():
            if k.startswith("$"): continue
            if isinstance(v, dict) and "$in" in v:
                vals = [str(x) for x in v["$in"]]
                matched = [d for d in matched if str(d.get(k)) in vals]
            elif isinstance(v, dict) and "$gte" in v:
                matched = [d for d in matched if d.get(k) is not None and d[k] >= v["$gte"]]
            elif isinstance(v, dict) and "$exists" in v:
                matched = [d for d in matched if (k in d) == v["$exists"]]
            else:
                matched = [d for d in matched if d.get(k) == v]
        return FakeCursor(matched)
    async def find_one(self, query, projection=None):
        for d in self._docs:
            ok = True
            for k, v in query.items():
                if isinstance(v, dict) and "$exists" in v:
                    if (k in d) != v["$exists"]: ok = False
                elif d.get(k) != v: ok = False
            if ok: return dict(d)
        return None


class FakeDB(dict):
    def __missing__(self, key):
        if key == COLLECTION: self[key] = FakeRepoCollection()
        else: self[key] = ReadCollection()
        return self[key]


@pytest.fixture
def db():
    return FakeDB()


def chile_dt(h, m=0, day=24):
    return CHILE_TZ.localize(datetime(2026, 7, day, h, m))


def make_lead(lid="lead-1", temp="COLD", phone="56912345678", exec_name="E", stage="NEW"):
    return {"_id": lid, "phone": phone, "lead_temperature_effective": temp,
            "pipeline_stage": stage, "ejecutivo_asignado": exec_name,
            "prospecto": {"nombre": "C", "codigo": "P001"}}


def make_cycle(lid="lead-1", cid="cycle-1", uid="user-1"):
    return {"lead_id": lid, "assignment_cycle_id": cid, "assigned_to_user_id": uid,
            "assigned_at": chile_dt(9, 0).astimezone(timezone.utc),
            "unassigned_at": None, "cycle_status": "active", "reason": "lead_created"}


def _alert_doc(overrides=None):
    base = {
        "_id": "cycle-1|breached|user-1", "message_domain": "crm_sla_alert",
        "assignment_cycle_id": "cycle-1", "lead_id": "lead-1", "recipient_user_id": "user-1",
        "recipient_phone_snapshot": "+56911111111", "alert_level": "breached",
        "sla_profile": "standard", "outreach_state": "none",
        "elapsed_business_minutes": 185, "deadline_at": datetime(2026, 7, 24, 16, 0, tzinfo=timezone.utc),
        "rendered_message": "test", "lead_url": "https://crm/crm/lead-id/lead-1",
        "state": ST_PENDING, "attempt_count": 0, "lease_owner": None, "lease_expires_at": None,
        "delivery_attempt_id": None, "delivery_started_at": None, "delivery_completed_at": None,
        "delivery_outcome": None, "provider_message_id": None,
        "next_attempt_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        "cancellation_reason": None, "last_error": None,
    }
    if overrides: base.update(overrides)
    return base


# ============================================================================
# LIVE_SEND=false
# ============================================================================

class TestSendDisabled:
    @pytest.mark.asyncio
    async def test_live_send_false_no_claims(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        with patch("chatbot.crm_sla_alert_worker.CRM_SLA_ALERTS_ENABLED", False):
            r = await process_alerts_batch(db=db, worker_id="w1", sender=FakeSender())
        assert r["status"] == "disabled"
        assert db[COLLECTION]._docs[0]["state"] == ST_PENDING

    @pytest.mark.asyncio
    async def test_invalid_config_no_claims(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        with patch("chatbot.crm_sla_alert_worker.CRM_SLA_ALERTS_ENABLED", True), \
             patch("chatbot.crm_sla_alert_worker.validate_live_send_config") as vcfg:
            vcfg.return_value = {"valid": False, "reason": "invalid_live_send_configuration: test"}
            r = await process_alerts_batch(db=db, worker_id="w1")
        assert r["status"] == "invalid_live_send_configuration"
        assert r["processed"] == 0

    @pytest.mark.asyncio
    async def test_valid_config_allows_processing(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        with patch("chatbot.crm_sla_alert_worker.CRM_SLA_ALERTS_ENABLED", True), \
             patch("chatbot.crm_sla_alert_worker.validate_live_send_config") as vcfg:
            vcfg.return_value = {"valid": True}
            r = await process_alerts_batch(db=db, worker_id="w1", sender=FakeSender(),
                                           max_total=1, max_per_recipient=99)
        assert r["processed"] == 1
        assert r["by_status"].get("sent", 0) == 1


# ============================================================================
# Worker claim + send
# ============================================================================

class TestWorker:
    @pytest.mark.asyncio
    async def test_revalidation_cancels_no_respondio_before_send(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = [{
            "lead_id": "lead-1", "assignment_cycle_id": "cycle-1",
            "type": "HUMAN_NOTE", "actor": "E", "actor_type": "human",
            "result": "intento_fallido", "confirmed": True,
            "timestamp": chile_dt(9, 15),
            "meta": {"meaningful_change": True},
        }]
        sender = FakeSender()
        r = await process_one_alert(db=db, worker_id="w1", sender=sender)
        assert r == {"status": "cancelled", "reason": "management_completed"}
        assert db[COLLECTION]._docs[0]["state"] == "cancelled"

    @pytest.mark.asyncio
    async def test_refreshes_lease_during_provider_call(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []

        async def slow_sender(phone, message):
            await asyncio.sleep(1.1)
            return SenderResult(outcome="confirmed_success", provider_message_id="wa-heartbeat")

        with patch("chatbot.crm_sla_alert_worker.PROVIDER_TIMEOUT_SECONDS", 2), \
             patch("chatbot.crm_sla_alert_worker.refresh_delivery_lease", autospec=True) as refresh:
            refresh.return_value = _alert_doc({"state": ST_PROCESSING})
            r = await process_one_alert(db=db, worker_id="w1", sender=slow_sender)
        assert r["status"] == "sent"
        assert refresh.await_count >= 1

    @pytest.mark.asyncio
    async def test_send_success(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        r = await process_one_alert(db=db, worker_id="w1", sender=FakeSender("confirmed_success", "wa-1"))
        assert r["status"] == "sent"

    @pytest.mark.asyncio
    async def test_managed_cancels(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = [{"assignment_cycle_id": "cycle-1",
            "result_type": "EFFECTIVE_CONTACT", "actor_user_id": "user-1",
            "occurred_at": chile_dt(9, 15), "source": "crm_quick_action"}]
        db["crm_events"]._docs = []
        r = await process_one_alert(db=db, worker_id="w1", sender=FakeSender())
        assert r["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_reassigned_cancels(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle(uid="user-2"))  # diff user
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        r = await process_one_alert(db=db, worker_id="w1", sender=FakeSender())
        assert r["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_lead_closed_cancels(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead(stage="CLOSED_WON"))
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        assert (await process_one_alert(db=db, worker_id="w1", sender=FakeSender()))["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_rejected_before_acceptance_retryable(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        r = await process_one_alert(db=db, worker_id="w1", sender=FakeSender("rejected_before_acceptance"))
        assert r["status"] == "failed_retryable"

    @pytest.mark.asyncio
    async def test_delivery_unknown_uncertain(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        r = await process_one_alert(db=db, worker_id="w1", sender=FakeSender("delivery_unknown"))
        assert r["status"] == "delivery_uncertain"

    @pytest.mark.asyncio
    async def test_timeout_after_start_uncertain(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        with patch("chatbot.crm_sla_alert_worker.asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            r = await process_one_alert(db=db, worker_id="w1", sender=FakeSender())
        assert r["status"] == "delivery_uncertain"

    @pytest.mark.asyncio
    async def test_lost_lease_no_sender_call(self, db):
        """If mark_delivery_started fails (lease expired), sender is never called."""
        db[COLLECTION]._docs.append(_alert_doc({"lease_owner": "w9"}))  # wrong owner in doc
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        # Simulate: claim works but mark_delivery_started fails because lease was set by another worker
        with patch("chatbot.crm_sla_alert_worker.mark_delivery_started", return_value=None):
            r = await process_one_alert(db=db, worker_id="w1", sender=FakeSender())
        assert r["status"] == "lost_lease"

    @pytest.mark.asyncio
    async def test_exception_after_start_uncertain(self, db):
        db[COLLECTION]._docs.append(_alert_doc())
        db["leads"]._docs.append(make_lead())
        db["crm_assignment_cycles"]._docs.append(make_cycle())
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        r = await process_one_alert(db=db, worker_id="w1", sender=FailingSender(RuntimeError("boom")))
        assert r["status"] == "delivery_uncertain"


# ============================================================================
# Batch
# ============================================================================

class TestBatch:
    @pytest.mark.asyncio
    async def test_batch(self, db):
        for i in range(3):
            db[COLLECTION]._docs.append(_alert_doc({"_id": f"c{i}|breached|u{i+1}",
                "assignment_cycle_id": f"c{i}", "lead_id": f"l{i}", "recipient_user_id": f"u{i+1}"}))
            db["leads"]._docs.append(make_lead(f"l{i}"))
            db["crm_assignment_cycles"]._docs.append(make_cycle(f"l{i}", f"c{i}", f"u{i+1}"))
        db["crm_management_results"]._docs = []
        db["crm_events"]._docs = []
        with patch("chatbot.crm_sla_alert_worker.CRM_SLA_ALERTS_ENABLED", True), \
             patch("chatbot.crm_sla_alert_worker.validate_live_send_config") as vcfg:
            vcfg.return_value = {"valid": True}
            r = await process_alerts_batch(db=db, worker_id="w1", sender=FakeSender(),
                                           max_total=10, max_per_recipient=10)
        assert r["processed"] == 3
