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


def test_two_reassignments_create_two_notifications_but_max_two_blocks_third():
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
    assert enqueue_sla_reassignment_notification(db, decision=second_decision, result=third) is None
    assert len(db["crm_notifications_v1"].rows) == 2


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
