import asyncio
from copy import deepcopy
import time

import pytest

from chatbot.crm_management import LeadReassignedSlaLockedError, record_management_result
from chatbot.crm_sla_reassignment_models import SLAReassignmentResult
from chatbot.crm_sla_reassignment_notifications import (
    enqueue_sla_reassignment_notification,
    process_one_sla_reassignment,
    process_one_sla_reassignment_sync,
)
from chatbot.crm_sla_reassignment_integration import execute_sla_reassignment_with_notification
from chatbot.crm_sla_global_rescue import RescueParameters
from chatbot.crm_sla_hybrid_rescue import RM_GLOBAL_RESCUE
from chatbot.crm_sla_hybrid_stabilization import L1_LOW_NEEDS_PLUS_5, R2_ROLLING_SHARE
from chatbot.crm_sla_policy_freeze import _rm_selection
import chatbot.crm_sla_reassignment_executor as executor_module
from tests.test_crm_sla_reassignment_executor import FakeCollection, make_fixture


class NotificationCollection(FakeCollection):
    def insert_one(self, document, *args, **kwargs):
        document = dict(document)
        document.setdefault("_id", f"notification-{len(self.rows) + 1}")
        return super().insert_one(document, *args, **kwargs)

    def find_one_and_update(self, filter_, update, *args, **kwargs):
        for key, row in self.rows.items():
            if not self._matches_for_test(row, filter_):
                continue
            for path, value in (update.get("$set") or {}).items():
                row[path] = deepcopy(value)
            for path in (update.get("$unset") or {}):
                row.pop(path, None)
            for path, value in (update.get("$push") or {}).items():
                row.setdefault(path, []).append(deepcopy(value))
            self.rows[key] = row
            return deepcopy(row)
        return None

    @staticmethod
    def _matches_for_test(row, filter_):
        for key, expected in (filter_ or {}).items():
            if key == "$or":
                if not any(NotificationCollection._matches_for_test(row, branch) for branch in expected):
                    return False
                continue
            actual = row.get(key)
            if isinstance(expected, dict):
                if "$exists" in expected and ((key in row) != bool(expected["$exists"])):
                    return False
                if "$in" in expected and actual not in expected["$in"]:
                    return False
                if "$ne" in expected and actual == expected["$ne"]:
                    return False
            elif actual != expected:
                return False
        return True


def notification_db():
    db, decision = make_fixture()
    db.collections["crm_notifications_v1"] = NotificationCollection()
    db["leads"].update_one(
        {"_id": "lead-1"},
        {"$set": {
            "lifecycle.current_assignment_cycle_id": "sla-reassignment:decision-1",
            "assignment_mirror_owner_user_id": "new-user",
        }},
    )
    db["crm_assignment_cycles"].insert_one({
        "_id": "destination-cycle-1",
        "assignment_cycle_id": "sla-reassignment:decision-1",
        "lead_id": "lead-1",
        "assigned_to_user_id": "new-user",
        "cycle_status": "active",
    })
    return db, decision


def _result(*, decision_id="decision-1", lead_id="lead-1", destination_cycle_id="sla-reassignment:decision-1", selected_user_id="new-user", number=1, status="APPLIED", committed=True):
    return SLAReassignmentResult(
        decision_id=decision_id,
        lead_id=lead_id,
        source_cycle_id="cycle-1",
        destination_cycle_id=destination_cycle_id,
        previous_owner_user_id="old-user",
        selected_user_id=selected_user_id,
        status=status,
        outcome=status,
        committed=committed,
        idempotent_replay=status == "ALREADY_APPLIED",
        policy_version="crm_sla_reassignment_v1",
        automatic_reassignment_number=number,
        transaction_attempts=1,
    )


def test_notification_is_post_commit_idempotent_and_targets_new_owner_only():
    db, decision = notification_db()
    db["usuarios"].update_one({"_id": "new-user"}, {"$set": {"telefono": "+56911111111"}})
    result = _result()

    first = enqueue_sla_reassignment_notification(db, decision=decision, result=result)
    second = enqueue_sla_reassignment_notification(db, decision=decision, result=result)

    assert first["_id"] == second["_id"]
    assert first["recipient_user_id"] == "new-user"
    assert "recipient_phone" not in first
    assert "phone" not in first["payload"]
    assert "email" not in first["payload"]
    assert "old-user" not in first["payload"]["message"]
    assert "score" not in first["payload"]["message"].lower()
    assert len(db["crm_notifications_v1"].rows) == 1


def test_reassignment_commit_to_notification_delivery_end_to_end(monkeypatch):
    from config import Config
    from tests.test_crm_sla_reassignment_executor import enable_flags

    enable_flags(monkeypatch)
    monkeypatch.setattr(Config, "CRM_SLA_REASSIGNMENT_WORKER_ENABLED", True)
    db, decision = make_fixture()
    db.collections["crm_notifications_v1"] = NotificationCollection()
    db["usuarios"].update_one({"_id": "new-user"}, {"$set": {"telefono": "+56911111111"}})

    execution = asyncio.run(execute_sla_reassignment_with_notification(db, decision))
    pending = db["crm_notifications_v1"].find_one({"state": "pending"})
    assert execution.result.committed is True
    assert pending is not None
    assert pending.get("provider_message_id") in (None, "")
    destination = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": pending["assignment_cycle_id"]})
    assert destination["reassignment_state"] == "AWAITING_OWNER_NOTIFICATION"
    assert destination.get("owner_notified_at") is None
    assert destination.get("sla_started_at") is None

    calls = []
    delivered = process_one_sla_reassignment_sync(
        db,
        worker_id="sla-notifier-e2e",
        sender=lambda phone, message: calls.append((phone, message)) or {
            "success": True, "provider_message_id": "provider-e2e", "http_status": 200,
        },
    )

    stored = db["crm_notifications_v1"].find_one({"_id": pending["_id"]})
    assert delivered["status"] == "sent"
    assert len(calls) == 1
    assert stored["provider_message_id"] == "provider-e2e"
    assert stored["state"] == "sent"
    assert stored["actually_delivered"] is True
    destination = db["crm_assignment_cycles"].find_one({"assignment_cycle_id": pending["assignment_cycle_id"]})
    assert destination["reassignment_state"] == "active"
    assert destination["owner_notified_at"] is not None
    assert destination["sla_started_at"] == destination["owner_notified_at"]


def test_notification_delivery_uses_thread_safe_sync_path_and_new_owner_phone():
    db, decision = notification_db()
    db["usuarios"].update_one({"_id": "new-user"}, {"$set": {"telefono": "+56911111111"}})
    result = _result()
    enqueue_sla_reassignment_notification(db, decision=decision, result=result)
    calls = []

    def sender(phone, message):
        calls.append((phone, message))
        return {"success": True, "provider_message_id": "provider-1", "http_status": 200}

    delivered = process_one_sla_reassignment_sync(db, worker_id="sla-notifier-test", sender=sender)

    assert delivered["status"] == "sent"
    assert len(calls) == 1
    assert calls[0][0] == "+56911111111"
    assert "vencimiento de SLA" in calls[0][1]
    notification = db["crm_notifications_v1"].find_one({"delivery_id": delivered["delivery_id"]})
    assert notification["state"] == "sent"
    assert notification["actually_delivered"] is True


def test_reassignment_notifications_continue_after_two_reassignments():
    db, decision = notification_db()
    db["usuarios"].update_one({"_id": "new-user"}, {"$set": {"telefono": "+56911111111"}})
    first = _result()
    second_decision = dict(decision)
    second_decision["decision_id"] = "decision-2"
    second_decision["selected_user_id"] = "new-user"
    second = _result(
        decision_id="decision-2",
        destination_cycle_id="sla-reassignment:decision-2",
        number=2,
    )
    third = _result(
        decision_id="decision-3",
        destination_cycle_id="sla-reassignment:decision-3",
        number=3,
    )

    assert enqueue_sla_reassignment_notification(db, decision=decision, result=first)
    assert enqueue_sla_reassignment_notification(db, decision=second_decision, result=second)
    third_decision = dict(second_decision)
    third_decision["decision_id"] = "decision-3"
    third_decision["selected_user_id"] = "new-user"
    assert enqueue_sla_reassignment_notification(db, decision=third_decision, result=third)
    assert len(db["crm_notifications_v1"].rows) == 3


def test_post_commit_notification_failure_does_not_change_committed_result(monkeypatch):
    db, decision = make_fixture()
    result = _result()
    monkeypatch.setattr(executor_module, "execute_sla_reassignment_transaction", lambda *args, **kwargs: result)

    import chatbot.crm_sla_reassignment_integration as integration_module
    monkeypatch.setattr(
        integration_module,
        "enqueue_sla_reassignment_notification",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("queue unavailable")),
    )

    execution = asyncio.run(execute_sla_reassignment_with_notification(db, decision))

    assert execution.result is result
    assert execution.result.committed is True
    assert execution.notification is None
    assert execution.notification_error == "RuntimeError"


def test_async_executor_adapter_does_not_block_heartbeat(monkeypatch):
    db, decision = make_fixture()
    result = _result()

    def slow_executor(*args, **kwargs):
        time.sleep(0.12)
        return result

    monkeypatch.setattr(executor_module, "execute_sla_reassignment_transaction", slow_executor)

    async def exercise():
        task = asyncio.create_task(executor_module.execute_sla_reassignment_transaction_async(db, decision))
        ticks = 0
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.01)
        return ticks, await task

    ticks, actual = asyncio.run(exercise())
    assert ticks >= 3
    assert actual is result


def test_old_owner_is_locked_after_reassignment(monkeypatch):
    from chatbot.crm_sla_reassignment_executor import execute_sla_reassignment_transaction
    from tests.test_crm_sla_reassignment_executor import enable_flags

    enable_flags(monkeypatch)
    db, decision = make_fixture()
    result = execute_sla_reassignment_transaction(db, decision)
    assert result.status == "APPLIED"

    with pytest.raises(LeadReassignedSlaLockedError) as error:
        record_management_result(
            db,
            lead_id="lead-1",
            assignment_cycle_id="cycle-1",
            actor_user_id="old-user",
            result_type="EFFECTIVE_CONTACT",
            source="crm_quick_action",
            idempotency_key="old-owner-after-reassignment",
        )
    assert str(error.value) == LeadReassignedSlaLockedError.code


def test_current_owner_must_use_current_cycle_then_can_save(monkeypatch):
    from chatbot.crm_sla_reassignment_executor import execute_sla_reassignment_transaction
    from tests.test_crm_sla_reassignment_executor import enable_flags

    enable_flags(monkeypatch)
    db, decision = make_fixture()
    result = execute_sla_reassignment_transaction(db, decision)
    assert result.status == "APPLIED"

    with pytest.raises(LeadReassignedSlaLockedError):
        record_management_result(
            db, lead_id="lead-1", assignment_cycle_id="cycle-1", actor_user_id="new-user",
            result_type="EFFECTIVE_CONTACT", source="crm_quick_action",
            idempotency_key="stale-current-cycle",
        )

    saved = record_management_result(
        db, lead_id="lead-1", assignment_cycle_id=result.destination_cycle_id,
        actor_user_id="new-user", result_type="EFFECTIVE_CONTACT",
        source="crm_quick_action", idempotency_key="current-cycle-save",
    )
    assert saved["assignment_cycle_id"] == result.destination_cycle_id


def test_async_notification_wrapper_offloads_delivery():
    db, decision = notification_db()
    db["usuarios"].update_one({"_id": "new-user"}, {"$set": {"telefono": "+56911111111"}})
    enqueue_sla_reassignment_notification(db, decision=decision, result=_result())
    calls = []

    async def exercise():
        heartbeat = 0
        task = asyncio.create_task(
            process_one_sla_reassignment(
                db,
                worker_id="sla-notifier-test",
                sender=lambda phone, message: (
                    calls.append((phone, message))
                    or {"success": True, "provider_message_id": "provider-async", "http_status": 200}
                ),
                enabled=True,
            )
        )
        while not task.done():
            heartbeat += 1
            await asyncio.sleep(0.005)
        return heartbeat, await task

    heartbeat, outcome = asyncio.run(exercise())
    assert outcome["status"] == "sent"
    assert heartbeat >= 1
    assert calls and calls[0][0] == "+56911111111"


def test_sla_notification_records_one_provider_attempt_and_is_not_duplicated():
    db, decision = notification_db()
    db["usuarios"].update_one({"_id": "new-user"}, {"$set": {"telefono": "+56911111111"}})
    enqueue_sla_reassignment_notification(db, decision=decision, result=_result())
    calls = []

    def sender(phone, message):
        calls.append((phone, message))
        return {"success": True, "provider_message_id": "provider-once", "http_status": 200}

    first = process_one_sla_reassignment_sync(db, worker_id="w1", sender=sender)
    second = process_one_sla_reassignment_sync(db, worker_id="w2", sender=sender)
    stored = db["crm_notifications_v1"].find_one({"delivery_id": first["delivery_id"]})

    assert first["status"] == "sent"
    assert second["status"] == "idle"
    assert len(calls) == 1
    assert stored["provider_message_id"] == "provider-once"
    assert len(stored["delivery_attempts"]) == 1


def test_sla_notification_retry_clears_failed_reservation_and_sends_once_on_retry():
    db, decision = notification_db()
    db["usuarios"].update_one({"_id": "new-user"}, {"$set": {"telefono": "+56911111111"}})
    queued = enqueue_sla_reassignment_notification(db, decision=decision, result=_result())
    calls = []

    def sender(phone, message):
        calls.append((phone, message))
        if len(calls) == 1:
            return {"success": False, "http_status": 500, "error": "temporary"}
        return {"success": True, "provider_message_id": "provider-retry", "http_status": 200}

    failed = process_one_sla_reassignment_sync(db, worker_id="w1", sender=sender)
    delivered = process_one_sla_reassignment_sync(db, worker_id="w2", sender=sender)
    stored = db["crm_notifications_v1"].find_one({"_id": queued["_id"]})

    assert failed["status"] == "failed_retryable"
    assert delivered["status"] == "sent"
    assert len(calls) == 2
    assert stored["provider_message_id"] == "provider-retry"
    assert len(stored["delivery_attempts"]) == 2


def test_stale_sla_notification_is_marked_without_provider_call():
    db, decision = notification_db()
    db["usuarios"].update_one({"_id": "new-user"}, {"$set": {"telefono": "+56911111111"}})
    queued = enqueue_sla_reassignment_notification(db, decision=decision, result=_result())
    db["crm_assignment_cycles"].update_one(
        {"assignment_cycle_id": "sla-reassignment:decision-1"},
        {"$set": {"assigned_to_user_id": "other-user"}},
    )
    calls = []

    result = process_one_sla_reassignment_sync(
        db,
        worker_id="w1",
        sender=lambda *args: calls.append(args) or {
            "success": True, "provider_message_id": "must-not-send", "http_status": 200,
        },
    )

    assert result["status"] == "stale_not_sent"
    assert calls == []
    assert db["crm_notifications_v1"].find_one({"_id": queued["_id"]})["state"] == "stale_not_sent"


def _rm_candidate(user_id, name, *, p50, active=True, role="agente"):
    return {
        "user_id": user_id,
        "identity_key": name,
        "executive": name,
        "active": active,
        "role": role,
        "sample_size": 40,
        "p50_first_management_business_minutes": p50,
        "p90_first_management_business_minutes": p50 + 10,
        "sla_compliance_rate": 0.90,
        "attention_rate": 0.90,
        "open_current_policy": 1,
        "unmanaged_current_policy": 0,
        "expired_current_policy": 0,
        "shadow_received_count": 0,
    }


def _rm_policy(candidates, *, owner="old-user"):
    return _rm_selection(
        {"owner_user_id": owner, "rm_candidates": candidates},
        {},
        [],
        scenario=R2_ROLLING_SHARE,
        low_policy=L1_LOW_NEEDS_PLUS_5,
        team={
            "sla_compliance_rate": 0.80,
            "attention_rate": 0.80,
            "team_p50_average": 30,
            "team_p90_average": 60,
        },
        params=RescueParameters(),
    )


def test_rm_tier1_wins_over_higher_scoring_tier2():
    result = _rm_policy([
        _rm_candidate("hernan", "Hernán Castro", p50=60),
        _rm_candidate("susana", "Susana Ensignia", p50=1),
    ])
    assert result["winner"]["user_id"] == "hernan"
    assert result["candidate_tier"] == 1
    assert result["tier_fallback_reason"] == ""
    assert next(row for row in result["scored"] if row["user_id"] == "susana")["tier_exclusion_reason"] == "TIER2_DEFERRED_TIER1_AVAILABLE"


def test_rm_maria_wins_within_tier1_even_when_tier2_scores_higher():
    result = _rm_policy([
        _rm_candidate("maria", "María Paz Galleguillos", p50=5),
        _rm_candidate("hernan", "Hernán Castro", p50=30),
        _rm_candidate("susana", "Susana Ensignia", p50=1),
    ])
    assert result["winner"]["user_id"] == "maria"
    assert result["candidate_tier"] == 1


def test_rm_tier2_fallback_only_when_both_tier1_are_ineligible():
    result = _rm_policy([
        _rm_candidate("maria", "María Paz Galleguillos", p50=5, active=False),
        _rm_candidate("hernan", "Hernán Castro", p50=30, role="supervisor"),
        _rm_candidate("susana", "Susana Ensignia", p50=1),
    ])
    assert result["winner"]["user_id"] == "susana"
    assert result["candidate_tier"] == 2
    assert result["tier_fallback_reason"] == "TIER1_NO_ELIGIBLE_CANDIDATE"


def test_rm_previous_owner_hernan_is_excluded_but_maria_remains_tier1():
    result = _rm_policy([
        _rm_candidate("hernan", "Hernán Castro", p50=1),
        _rm_candidate("maria", "María Paz Galleguillos", p50=30),
    ], owner="hernan")
    assert result["winner"]["user_id"] == "maria"
    assert result["candidate_tier"] == 1


def test_rm_r2_cannot_promote_tier2_when_tier1_is_available():
    result = _rm_policy([
        _rm_candidate("maria", "María Paz Galleguillos", p50=30),
        _rm_candidate("susana", "Susana Ensignia", p50=1),
    ])
    assert result["winner"]["candidate_tier"] == 1
    assert all(row.get("candidate_tier") != 2 or row.get("tier_exclusion_reason") for row in result["scored"])
