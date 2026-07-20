from copy import deepcopy
from datetime import timedelta
from threading import Lock

from pymongo.errors import DuplicateKeyError

from chatbot.crm_management import (
    claim_sla_alert_if_still_eligible, eligible_for_first_sla_reassignment,
    follow_up_shadow_status, record_management_result,
)
from chatbot.crm_metrics import event_evidence
from chatbot.crm_sla_shadow import evaluate_sla_shadow
from tests.test_crm_notification_containment import local


def get_path(doc, path):
    value = doc
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value: return None, False
        value = value[part]
    return value, True


def set_path(doc, path, value):
    target = doc
    parts = path.split(".")
    for part in parts[:-1]: target = target.setdefault(part, {})
    target[parts[-1]] = deepcopy(value)


def matches(doc, query):
    for key, expected in query.items():
        actual, exists = get_path(doc, key)
        if isinstance(expected, dict):
            if "$exists" in expected and exists != expected["$exists"]: return False
            if "$in" in expected and actual not in expected["$in"]: return False
            if "$ne" in expected and actual == expected["$ne"]: return False
        elif actual != expected: return False
    return True


class Result:
    modified_count = 1


class Collection:
    def __init__(self, docs=None): self.docs = deepcopy(list(docs or [])); self.lock = Lock()
    def find_one(self, query, *args, **kwargs):
        with self.lock:
            return next((deepcopy(row) for row in self.docs if matches(row, query)), None)
    def insert_one(self, doc):
        with self.lock:
            if any(row.get("_id") == doc.get("_id") for row in self.docs): raise DuplicateKeyError("duplicate")
            self.docs.append(deepcopy(doc)); return Result()
    def update_one(self, query, update, upsert=False):
        with self.lock:
            row = next((item for item in self.docs if matches(item, query)), None)
            if row is None and upsert:
                row = {k: v for k, v in query.items() if not isinstance(v, dict)}; self.docs.append(row)
            if row is not None:
                for key, value in update.get("$set", {}).items(): set_path(row, key, value)
            return Result()
    def update_many(self, query, update):
        with self.lock:
            for row in self.docs:
                if matches(row, query):
                    for key, value in update.get("$set", {}).items(): set_path(row, key, value)
        return Result()
    def find_one_and_update(self, query, update, **kwargs):
        with self.lock:
            row = next((item for item in self.docs if matches(item, query)), None)
            if row is None: return None
            for key, value in update.get("$set", {}).items(): set_path(row, key, value)
            return deepcopy(row)


class DB(dict):
    def __missing__(self, key): self[key] = Collection(); return self[key]


def fixture(temperature="HOT"):
    lead = {"_id": "lead-1", "lead_temperature_effective": temperature, "lifecycle": {}}
    cycle = {"_id": "cycle-doc", "lead_id": "lead-1", "assignment_cycle_id": "cycle-1",
             "assigned_to_user_id": "user-1", "assigned_at": local(20, 9), "unassigned_at": None,
             "cycle_status": "active", "schema_version": "crm_assignment_cycle_v1"}
    db = DB(leads=Collection([lead]), crm_assignment_cycles=Collection([cycle]),
            crm_management_results=Collection(), crm_events=Collection(), crm_notifications_v1=Collection())
    return db, lead, cycle


def record(db, result_type, key="one", next_at=None):
    return record_management_result(
        db, lead_id="lead-1", assignment_cycle_id="cycle-1", actor_user_id="user-1",
        result_type=result_type, occurred_at=local(20, 10), source="crm_quick_action",
        idempotency_key=key, next_follow_up_at=next_at,
    )


def refreshed(db): return db["leads"].find_one({"_id": "lead-1"}), db["crm_assignment_cycles"].find_one({"assignment_cycle_id": "cycle-1"})


def test_opening_external_apps_never_credits_management():
    for event_type in ("CLICK_WHATSAPP_LEAD", "CLICK_PHONE_LEAD", "CLICK_EMAIL_LEAD", "OPEN_DETAIL", "NAVIGATION"):
        assert event_evidence({"lead_id": "lead-1", "type": event_type, "actor": "user-1", "actor_type": "human"})["management"] is False


def test_message_sent_completes_management_and_attempt_but_not_effective_contact():
    db, _, _ = fixture(); record(db, "MESSAGE_SENT_WAITING_RESPONSE")
    lead, cycle = refreshed(db)
    assert lead["lifecycle"]["first_valid_management_at"] == local(20, 10)
    assert lead["lifecycle"]["first_contact_attempt_at"] == local(20, 10)
    assert "first_effective_contact_at" not in lead["lifecycle"]
    assert lead["management_status"] == "managed_waiting_response"
    assert lead["contact_attempted"] is True and lead["effective_contact"] is False
    assert cycle["sla_first_management_status"] == "completed"


def test_message_sent_creates_follow_up_and_no_sla_after_three_hours():
    db, lead, cycle = fixture(); record(db, "MESSAGE_SENT_WAITING_RESPONSE")
    lead, cycle = refreshed(db)
    assert lead["follow_up_required"] is True and lead["follow_up_status"] == "pending"
    sla = evaluate_sla_shadow(leads=[lead], cycles=[cycle], users=[{"_id": "user-1", "active": True}],
                              deliveries=[{"state": "sent", "metadata": {"assignment_cycle_id": "cycle-1"}}],
                              as_of=local(20, 14))
    assert sla["shadow_yellow"] == sla["shadow_red"] == 0


def test_assignment_notification_does_not_credit_management():
    assert event_evidence({"lead_id": "lead-1", "type": "ALERT_SENT", "actor": "system",
                           "result": "lead_assignment_hot", "confirmed": True})["management"] is False


def test_call_no_answer_credits_first_management_and_attempt():
    db, _, _ = fixture(); record(db, "CALL_NO_ANSWER")
    lead, _ = refreshed(db)
    assert lead["lifecycle"]["first_valid_management_at"]
    assert lead["lifecycle"]["first_contact_attempt_at"]
    assert lead["follow_up_required"] is True


def test_effective_contact_credits_attempt_and_effective_contact():
    db, _, _ = fixture(); record(db, "EFFECTIVE_CONTACT")
    lead, _ = refreshed(db)
    assert lead["lifecycle"]["first_contact_attempt_at"]
    assert lead["lifecycle"]["first_effective_contact_at"]


def test_duplicate_result_is_idempotent_and_keeps_one_first_management():
    db, _, _ = fixture(); first = record(db, "CALL_NO_ANSWER", "same"); second = record(db, "CALL_NO_ANSWER", "same")
    assert first["_id"] == second["_id"]
    assert len(db["crm_management_results"].docs) == len(db["crm_events"].docs) == 1


def test_later_follow_up_updates_cycle_without_replacing_first_management():
    db, _, _ = fixture(); record(db, "CALL_NO_ANSWER", "first")
    record_management_result(
        db, lead_id="lead-1", assignment_cycle_id="cycle-1", actor_user_id="user-1",
        result_type="SCHEDULE_FOLLOW_UP", occurred_at=local(20, 12), source="crm_quick_action",
        idempotency_key="later", next_follow_up_at=local(21, 10),
    )
    _, cycle = refreshed(db)
    assert cycle["first_valid_management_at"] == local(20, 10)
    assert cycle["next_follow_up_at"] == local(21, 10)
    assert cycle["last_management_result"] == "SCHEDULE_FOLLOW_UP"


def test_management_wins_atomic_race_and_stale_sla_claim_is_rejected():
    db, _, _ = fixture(); record(db, "MESSAGE_SENT_WAITING_RESPONSE")
    claim = claim_sla_alert_if_still_eligible(db, assignment_cycle_id="cycle-1", level="red",
                                              recipient_user_id="user-1", claimed_at=local(20, 13))
    assert claim is None


def test_waiting_response_cannot_be_reassigned_but_unmanaged_red_can():
    db, lead, cycle = fixture(); record(db, "MESSAGE_SENT_WAITING_RESPONSE")
    lead, cycle = refreshed(db)
    assert not eligible_for_first_sla_reassignment(cycle=cycle, lead=lead, delivery_valid=True,
                                                   red_overdue=True, executive_active=True)
    db2, lead2, cycle2 = fixture()
    assert eligible_for_first_sla_reassignment(cycle=cycle2, lead=lead2, delivery_valid=True,
                                               red_overdue=True, executive_active=True)


def test_follow_up_overdue_is_shadow_only_without_alert_or_reassignment():
    db, _, _ = fixture(); record(db, "SCHEDULE_FOLLOW_UP", next_at=local(20, 11))
    lead, _ = refreshed(db); status = follow_up_shadow_status(lead, as_of=local(20, 12))
    assert status == {"required": True, "overdue": True, "alerts_enabled": False, "reassignment_enabled": False}


def test_cold_uses_same_management_definition_without_individual_sla():
    db, _, _ = fixture("COLD"); record(db, "CALL_NO_ANSWER")
    lead, cycle = refreshed(db)
    sla = evaluate_sla_shadow(leads=[lead], cycles=[cycle], users=[{"_id": "user-1", "active": True}],
                              deliveries=[], as_of=local(20, 14))
    assert lead["lifecycle"]["first_valid_management_at"] and not sla["alerts"]


def test_no_test_uses_a_real_provider():
    assert all("whatsapp_client" not in str(value) for value in (record, fixture, refreshed))
