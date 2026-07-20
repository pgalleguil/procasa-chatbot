from copy import deepcopy
from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import patch
import asyncio

import pytest
from pymongo.errors import DuplicateKeyError

from chatbot.constants import CHILE_TZ
from chatbot.crm_cold_digest import simulate_cold_digests
from chatbot.crm_metrics import build_weekly_crm_snapshot, event_evidence
from chatbot.crm_notifications import (
    COLLECTION, VolumeLimits, claim_next, create_pending, finalize_attempt,
    individual_identity, recover_expired_lease, validate_volume,
)
from chatbot.crm_reconciliation import reconcile_shadow
from chatbot.crm_sla_shadow import evaluate_sla_shadow


def _match(doc, query):
    for key, expected in query.items():
        actual = doc.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]: return False
            if "$lte" in expected and not (actual is not None and actual <= expected["$lte"]): return False
        elif actual != expected: return False
    return True


class Result:
    inserted_id = "id"


class Collection:
    def __init__(self, docs=None, unique=None):
        self.docs = deepcopy(list(docs or [])); self.unique = set(unique or []); self.lock = threading.Lock()
    def find(self, query=None, projection=None): return deepcopy([d for d in self.docs if _match(d, query or {})])
    def find_one(self, query, *args, **kwargs):
        rows = self.find(query); return rows[0] if rows else None
    def insert_one(self, doc):
        with self.lock:
            for field in self.unique:
                if doc.get(field) is not None and any(row.get(field) == doc[field] for row in self.docs):
                    raise DuplicateKeyError("duplicate")
            row = deepcopy(doc); row.setdefault("_id", str(len(self.docs) + 1)); self.docs.append(row); return Result()
    def find_one_and_update(self, query, update, sort=None, return_document=None):
        with self.lock:
            for doc in self.docs:
                if not _match(doc, query): continue
                doc.update(deepcopy(update.get("$set", {})))
                for field, value in update.get("$push", {}).items(): doc.setdefault(field, []).append(deepcopy(value))
                return deepcopy(doc)
        return None


class DB(dict):
    def __missing__(self, key): self[key] = Collection(); return self[key]


def local(day, hour=9):
    return CHILE_TZ.localize(datetime(2026, 7, day, hour)).astimezone(timezone.utc)


def test_hot_cold_unknown_partition_and_temperature_does_not_manage():
    leads = [
        {"_id": "h", "created_at": local(20), "temperature_history": [{"at": local(20, 8), "value": "HOT"}]},
        {"_id": "c", "created_at": local(20), "temperature_history": [{"at": local(20, 8), "value": "COLD"}]},
        {"_id": "u", "created_at": local(20)},
    ]
    db = DB(leads=Collection(leads), crm_events=Collection(), crm_assignment_cycles=Collection(), visitas=Collection())
    snapshot = build_weekly_crm_snapshot(
        db, period_start="2026-07-20", period_end="2026-07-24",
        priority_as_of=CHILE_TZ.localize(datetime(2026, 7, 27, 8, 15)), executive_order=[],
    )
    cohort = snapshot["cohort"]
    assert (cohort["received_unique"], cohort["hot_at_cutoff_unique"], cohort["cold_at_cutoff_unique"], cohort["unknown_temperature_at_cutoff_unique"]) == (3, 1, 1, 1)
    assert cohort["managed_unique"] == 0
    assert snapshot["data_quality"]["temperature_invariant_valid"] is True
    assert event_evidence({"lead_id": "h", "type": "STATUS", "actor": None, "result": "HOT"})["management"] is False


def test_multiple_actions_are_one_managed_lead_and_open_is_not_management():
    events = [
        {"lead_id": "x", "type": "CLICK_WHATSAPP_LEAD", "actor": "u", "actor_type": "human"},
        {"lead_id": "x", "type": "CONTACT_RESULT", "actor": "u", "actor_type": "human", "confirmed": True, "result": "CONTACTADO"},
        {"lead_id": "x", "type": "HUMAN_NOTE", "actor": "u", "actor_type": "human", "meta": {"meaningful_change": True}},
    ]
    assert event_evidence(events[0])["management"] is False
    assert len({e["lead_id"] for e in events if event_evidence(e)["management"]}) == 1


def test_state_machine_duplicate_claim_retry_and_sent_is_terminal():
    db = DB(); db[COLLECTION] = Collection(unique={"individual_identity"})
    identity = individual_identity(lead_id="l", assignment_cycle_id="c", notification_type="hot", recipient_user_id="u")
    first = create_pending(db, identity_field="individual_identity", identity=identity, payload={"x": 1})
    duplicate = create_pending(db, identity_field="individual_identity", identity=identity, payload={"x": 1})
    assert duplicate["delivery_id"] == first["delivery_id"]
    claimed = claim_next(db, worker_id="w1", now=local(20))
    assert claim_next(db, worker_id="w2", now=local(20)) is None
    retry = finalize_attempt(db, notification_id=claimed["_id"], worker_id="w1", state="failed_retryable")
    reclaimed = claim_next(db, worker_id="w2", now=local(20) + timedelta(minutes=1))
    assert reclaimed["delivery_id"] == retry["delivery_id"]
    sent = finalize_attempt(db, notification_id=reclaimed["_id"], worker_id="w2", state="sent", provider_message_id="fake-1")
    assert sent["provider_message_id"] == "fake-1"
    assert claim_next(db, worker_id="w3") is None


def test_expired_lease_requires_provider_check_and_reuses_record():
    db = DB(); db[COLLECTION] = Collection()
    row = create_pending(db, identity_field="individual_identity", identity="l|c|hot|u", payload={})
    claimed = claim_next(db, worker_id="dead", lease_seconds=1, now=local(20))
    with pytest.raises(ValueError): recover_expired_lease(db, notification_id=claimed["_id"], provider_status="unknown", now=local(20, 10))
    recovered = recover_expired_lease(db, notification_id=claimed["_id"], provider_status="not_found", now=local(20, 10))
    assert recovered["_id"] == row["_id"] and recovered["state"] == "failed_retryable"


def _shadow_fixture():
    leads = [
        {"_id": "h", "temperature_history": [{"at": local(20, 8), "value": "HOT"}], "lead_temperature_effective": "HOT"},
        {"_id": "c", "temperature_history": [{"at": local(20, 8), "value": "COLD"}], "lead_temperature_effective": "COLD"},
        {"_id": "u"},
    ]
    cycles = [
        {"lead_id": key, "assignment_cycle_id": f"cy-{key}", "assigned_to_user_id": "active", "assigned_at": local(20), "unassigned_at": None}
        for key in "hcu"
    ] + [{"lead_id": "h", "assignment_cycle_id": "old", "assigned_to_user_id": "inactive", "assigned_at": local(19), "unassigned_at": None}]
    users = [{"_id": "active", "active": True}, {"_id": "inactive", "active": False}]
    return leads, cycles, users


def test_universal_shadow_quarantine_limits_and_cold_digest_are_non_delivering():
    leads, cycles, users = _shadow_fixture()
    cycles.append({**cycles[0], "lead_id": "c"})  # ambiguous duplicated cycle identity
    run = reconcile_shadow(leads=leads, cycles=cycles, users=users, deliveries=[],
                           scan_from=local(20, 0), scan_to=local(21, 0),
                           limits=VolumeLimits(global_per_run=20, per_executive=20))
    assert run["missing_hot"] == 1 and run["missing_cold"] == 1 and run["missing_unknown"] == 1
    assert run["ambiguous"] == 1 and run["deliverable_records_created"] == 0
    digest = simulate_cold_digests(run, business_period="2026-07-20", max_references=1)
    assert digest["simulated_count"] == 1 and digest["digests"][0]["deliverable_record_created"] is False
    assert "@" not in digest["digests"][0]["content"] and "+56" not in digest["digests"][0]["content"]
    blocked = validate_volume(total=101, by_executive={"u": 101}, per_minute=0, digests=0)
    assert blocked["circuit_breaker_open"] is True


def test_inactive_executive_is_suppressed_and_abnormal_digest_is_blocked():
    leads, cycles, users = _shadow_fixture()
    cycles[0]["assigned_to_user_id"] = "inactive"
    run = reconcile_shadow(leads=leads, cycles=cycles[:1], users=users, deliveries=[],
                           scan_from=local(20, 0), scan_to=local(21, 0))
    assert run["suppressed"] == 1 and run["inactive_executives"] == 1
    simulated = simulate_cold_digests({"results": [
        {"temperature": "COLD", "status": "missing_notification", "recipient_user_id": "u", "lead_id": str(i)}
        for i in range(3)
    ]}, business_period="p", abnormal_volume=2)
    assert simulated["blocked_count"] == 1 and simulated["digests"] == []


def test_sla_shadow_excludes_failed_delivery_and_never_calls_provider():
    leads, cycles, users = _shadow_fixture()
    delivery = [{"state": "sent", "metadata": {"assignment_cycle_id": "cy-h"}}]
    result = evaluate_sla_shadow(leads=leads, cycles=cycles, users=users, deliveries=delivery,
                                 as_of=local(20, 13))
    assert result["shadow_red"] == 1 and result["provider_calls"] == 0
    assert result["delivery_failed_exclusions"] == 2
    assert result["pre_cutover_exclusions"] == 1


def test_real_sla_worker_flag_prevents_database_and_provider_access():
    from chatbot.sla_service import monitor_sla_thresholds
    with patch("chatbot.sla_service.Config.CRM_SLA_ALERTS_ENABLED", False), \
         patch("chatbot.sla_service.get_async_db") as get_db, \
         patch("chatbot.sla_service.NotificationService.send_notification") as sender:
        asyncio.run(monitor_sla_thresholds())
    get_db.assert_not_called(); sender.assert_not_called()


def test_containment_flags_are_independent_and_safe_by_default():
    from config import Config
    assert Config.LEAD_HOT_NOTIFICATIONS_ENABLED is True
    assert Config.LEAD_HOT_RECONCILIATION_ENABLED is False
    assert Config.LEAD_COLD_DIGEST_ENABLED is False
    assert Config.CRM_SLA_SHADOW_ENABLED is False
    assert Config.CRM_SLA_ALERTS_ENABLED is False
    assert Config.CRM_WEEKLY_REPORT_GENERATION_ENABLED is False
    assert Config.CRM_WEEKLY_REPORT_SEND_ENABLED is False
    assert Config.CRM_LEGACY_DAILY_REPORT_ENABLED is False
    assert Config.CRM_INACTIVE_NUDGE_ENABLED is False


def test_fake_provider_timeout_retry_keeps_delivery_identity():
    db = DB(); db[COLLECTION] = Collection()
    created = create_pending(db, identity_field="individual_identity", identity="l|c|hot|u", payload={})
    first = claim_next(db, worker_id="w1", now=local(20))
    failed = finalize_attempt(db, notification_id=first["_id"], worker_id="w1",
                              state="failed_retryable", error="fake timeout")
    second = claim_next(db, worker_id="w2", now=local(20) + timedelta(minutes=1))
    delivered = finalize_attempt(db, notification_id=second["_id"], worker_id="w2",
                                 state="sent", provider_message_id="fake-provider-id")
    assert created["delivery_id"] == failed["delivery_id"] == delivered["delivery_id"]
    assert len(delivered["attempts"]) == 4
