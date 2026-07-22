import asyncio
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from chatbot.constants import CHILE_TZ
from chatbot.crm_cold_digest import simulate_cold_digests
from chatbot.crm_hot_delivery import assign_and_enqueue_hot, process_one_hot
from chatbot.crm_notifications import COLLECTION, claim_next, finalize_attempt
from chatbot.crm_reconciliation import reconcile_shadow
from chatbot.crm_sla_shadow import evaluate_sla_shadow
from tests.test_crm_notification_containment import Collection, DB, local


async def fake_sender(_recipient, _payload):
    return {"success": True, "provider_message_id": "fake-provider-1", "delivery_status": "accepted"}


def setup_db():
    lead = {"_id": "lead-1", "lead_temperature_effective": "HOT", "temperature_history": [{"at": local(20, 8), "value": "HOT"}]}
    db = DB(leads=Collection([lead]), crm_assignment_cycles=Collection(), crm_notifications_v1=Collection(unique={"individual_identity"}))
    return db, lead


def test_hot_business_hours_full_canonical_flow_and_sla_eligibility():
    db, lead = setup_db()
    created = assign_and_enqueue_hot(db, lead=lead, recipient_user_id="u1", recipient_phone="+56911111111",
                                     payload={"target_name": "u1"}, assigned_at=local(20, 9), send_after=local(20, 9))
    repeated = assign_and_enqueue_hot(db, lead=lead, recipient_user_id="u1", recipient_phone="+56911111111",
                                      payload={"target_name": "u1"}, assigned_at=local(20, 9), send_after=local(20, 9))
    assert len(db["crm_assignment_cycles"].docs) == 1
    assert len(db[COLLECTION].docs) == 1
    assert repeated["notification"]["delivery_id"] == created["notification"]["delivery_id"]
    sent = asyncio.run(process_one_hot(db, sender=fake_sender, worker_id="w1", now=local(20, 10), enabled=True))
    assert sent == {"status": "sent", "delivery_id": created["notification"]["delivery_id"], "provider_message_id": "fake-provider-1"}
    sla = evaluate_sla_shadow(leads=[lead], cycles=db["crm_assignment_cycles"].docs,
                              users=[{"_id": "u1", "active": True}], deliveries=db[COLLECTION].docs,
                              as_of=local(20, 13))
    assert sla["eligible_sla_cycles"] == 1 and sla["shadow_red"] == 1


def test_hot_outside_hours_waits_until_business_start_and_restart_does_not_duplicate():
    db, lead = setup_db()
    next_start = local(20, 9)
    created = assign_and_enqueue_hot(db, lead=lead, recipient_user_id="u1", recipient_phone="+56911111111",
                                     payload={}, assigned_at=next_start, send_after=next_start)
    assert asyncio.run(process_one_hot(db, sender=fake_sender, worker_id="before", now=local(20, 8), enabled=True))["status"] == "idle"
    assert asyncio.run(process_one_hot(db, sender=fake_sender, worker_id="after", now=next_start, enabled=True))["status"] == "sent"
    assert asyncio.run(process_one_hot(db, sender=fake_sender, worker_id="restart", now=local(20, 10), enabled=True))["status"] == "idle"
    assert len(db[COLLECTION].docs) == 1
    assert db["crm_assignment_cycles"].docs[0]["assigned_at"] == next_start


def test_cold_assignment_is_shadow_digest_only_and_never_sla_alert():
    lead = {"_id": "cold-1", "lead_temperature_effective": "COLD", "temperature_history": [{"at": local(20, 8), "value": "COLD"}]}
    db = DB(leads=Collection([lead]), crm_assignment_cycles=Collection(), crm_notifications_v1=Collection())
    from chatbot.crm_metrics import create_assignment_cycle
    cycle = create_assignment_cycle(db, lead=lead, assigned_to_user_id="u1", assigned_by="system", reason="router", assigned_at=local(20, 9))
    run = reconcile_shadow(leads=[lead], cycles=[cycle], users=[{"_id": "u1", "active": True}], deliveries=[],
                           scan_from=local(20, 0), scan_to=local(21, 0))
    digest = simulate_cold_digests(run, business_period="2026-07-20")
    assert run["missing_cold"] == 1 and digest["simulated_count"] == 1
    assert len(db[COLLECTION].docs) == 0
    sla = evaluate_sla_shadow(leads=[lead], cycles=[cycle], users=[{"_id": "u1", "active": True}], deliveries=[], as_of=local(20, 13))
    assert sla["shadow_yellow"] == sla["shadow_red"] == 0


def test_reassignment_closes_old_cycle_and_preserves_delivery_and_actor_history():
    db, lead = setup_db()
    first = assign_and_enqueue_hot(db, lead=lead, recipient_user_id="u1", recipient_phone="+56911111111", payload={}, assigned_at=local(20, 9))
    asyncio.run(process_one_hot(db, sender=fake_sender, worker_id="w1", now=local(20, 10), enabled=True))
    db["crm_assignment_cycles"].docs[0]["first_valid_management_actor"] = "u1"
    second = assign_and_enqueue_hot(db, lead=lead, recipient_user_id="u2", recipient_phone="+56922222222", payload={}, assigned_at=local(20, 11))
    assert len(db["crm_assignment_cycles"].docs) == 2
    assert db["crm_assignment_cycles"].docs[0]["cycle_status"] == "closed"
    assert db["crm_assignment_cycles"].docs[0]["first_valid_management_actor"] == "u1"
    assert first["notification"]["individual_identity"] != second["notification"]["individual_identity"]
    assert db[COLLECTION].docs[0]["state"] == "sent"


def test_crash_after_cycle_before_notification_is_found_without_duplicate_cycle():
    db, lead = setup_db()
    from chatbot.crm_metrics import create_assignment_cycle
    cycle = create_assignment_cycle(db, lead=lead, assigned_to_user_id="u1", assigned_by="system", reason="router", assigned_at=local(20, 9))
    run = reconcile_shadow(leads=[lead], cycles=[cycle], users=[{"_id": "u1", "active": True}], deliveries=[],
                           scan_from=local(20, 0), scan_to=local(21, 0))
    assert run["missing_hot"] == 1
    create_assignment_cycle(db, lead=lead, assigned_to_user_id="u1", assigned_by="system", reason="retry", assigned_at=local(20, 10))
    assert len(db["crm_assignment_cycles"].docs) == 1


def test_provider_acceptance_without_message_id_is_quarantined_not_retried():
    db, lead = setup_db()
    assign_and_enqueue_hot(db, lead=lead, recipient_user_id="u1", recipient_phone="+56911111111",
                           payload={}, assigned_at=local(20, 9), send_after=local(20, 9))
    async def accepted_without_evidence(_recipient, _payload):
        return {"success": True, "provider_message_id": None, "delivery_status": "accepted"}
    result = asyncio.run(process_one_hot(db, sender=accepted_without_evidence,
                                         worker_id="w", now=local(20, 10), enabled=True))
    assert result["status"] == "quarantined"
    assert asyncio.run(process_one_hot(db, sender=accepted_without_evidence,
                                       worker_id="restart", now=local(20, 11), enabled=True))["status"] == "idle"


def test_claim_and_finalize_offloaded_from_event_loop_and_main_thread():
    """Regression: claim_next/finalize_attempt must not run on MainThread or event loop.

    Verifies:
    1. Thread identity: functions execute on a threadpool worker, not MainThread.
    2. Event-loop absence: asyncio.get_running_loop() raises RuntimeError inside
       the thread — proving no event loop is active on that thread.
       (If the sync function ran on the event-loop thread, get_running_loop()
       would return the loop instead of raising.)
    """
    db, lead = setup_db()
    assign_and_enqueue_hot(db, lead=lead, recipient_user_id="u1", recipient_phone="+56911111111",
                           payload={"target_name": "u1"}, assigned_at=local(20, 9), send_after=local(20, 9))

    main_thread_id = threading.get_ident()
    call_sites = []  # (function_name, thread_id, thread_name, event_loop_active)

    def _track(name, fn):
        def _wrapped(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                loop_active = True
            except RuntimeError:
                loop_active = False
            call_sites.append((name, threading.get_ident(), threading.current_thread().name, loop_active))
            return fn(*args, **kwargs)
        return _wrapped

    # Instrument the actual functions used by process_one_hot
    with patch("chatbot.crm_hot_delivery.claim_next", _track("claim_next", claim_next)), \
         patch("chatbot.crm_hot_delivery.finalize_attempt", _track("finalize_attempt", finalize_attempt)):
        result = asyncio.run(process_one_hot(db, sender=fake_sender, worker_id="w1", now=local(20, 10), enabled=True))

    assert result["status"] == "sent"
    assert len(call_sites) >= 2, f"Expected at least claim_next + finalize_attempt, got {call_sites}"

    for func_name, thread_id, thread_name, loop_active in call_sites:
        assert thread_id != main_thread_id, (
            f"{func_name} ran on MainThread (id={thread_id}, name='{thread_name}') — "
            f"not offloaded via asyncio.to_thread()"
        )
        assert "MainThread" not in thread_name, (
            f"{func_name} thread name contains 'MainThread': '{thread_name}'"
        )
        assert not loop_active, (
            f"{func_name} has an active event loop on thread '{thread_name}' (id={thread_id}) — "
            f"asyncio.get_running_loop() did NOT raise RuntimeError. "
            f"Function is still executing on an event-loop thread, not a worker thread."
        )
