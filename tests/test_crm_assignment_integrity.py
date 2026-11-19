from chatbot.crm_assignment_integrity import (
    _chatbot_owner_findings,
    _transition_findings,
    _valid_transfer_transition,
    _was_transition_administratively_corrected,
    cycle_has_human_management,
)
import chatbot.crm_assignment_integrity as integrity
from pymongo.errors import NetworkTimeout
from unittest.mock import patch


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def limit(self, count):
        return Cursor(self.rows[:count])

    def __iter__(self):
        return iter(self.rows)


class Collection:
    def __init__(self, rows=(), first=None):
        self.rows = list(rows)
        self.first = first
        self.last_query = None

    def find(self, query):
        self.last_query = query
        return Cursor(self.rows)

    def find_one(self, query):
        self.last_query = query
        return self.first


class Database:
    def __init__(self, **collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections.setdefault(name, Collection())


def test_management_marker_and_legacy_events_must_fall_inside_cycle_window():
    cycle = {
        "lead_id": "lead-1",
        "assignment_cycle_id": "cycle-1",
        "assigned_at": "2026-10-19T09:00:00Z",
        "unassigned_at": "2026-10-19T10:00:00Z",
        "first_valid_management_at": "2026-10-19T10:00:01Z",
        "sla_first_management_status": "completed",
    }
    db = Database(
        crm_management_results=Collection([{
            "assignment_cycle_id": "cycle-1",
            "result_type": "EFFECTIVE_CONTACT",
            "status": "completed",
            "occurred_at": "2026-10-19T10:00:01Z",
        }]),
        crm_events=Collection([{
            "type": "HUMAN_NOTE",
            "timestamp": "2026-10-19T10:00:01Z",
            "text": "lead_cerrado",
        }]),
    )

    assert not cycle_has_human_management(db, cycle)


def test_in_cycle_effective_contact_still_counts_as_completed_management():
    cycle = {
        "lead_id": "lead-1",
        "assignment_cycle_id": "cycle-1",
        "assigned_at": "2026-10-19T09:00:00Z",
        "unassigned_at": "2026-10-19T10:00:00Z",
    }
    db = Database(
        crm_management_results=Collection([{
            "assignment_cycle_id": "cycle-1",
            "result_type": "EFFECTIVE_CONTACT",
            "status": "completed",
            "occurred_at": "2026-10-19T09:20:26Z",
        }]),
        crm_events=Collection(),
    )

    assert cycle_has_human_management(db, cycle)


def test_late_legacy_note_does_not_turn_prop360_transfer_into_managed_transfer():
    lead_id = "lead-1"
    previous = {
        "lead_id": lead_id,
        "assignment_cycle_id": "cycle-old",
        "assigned_to_user_id": "owner-a",
        "assigned_at": "2026-10-19T09:00:00Z",
        "unassigned_at": "2026-10-19T10:00:00Z",
        "cycle_status": "reassigned",
    }
    current = {
        "lead_id": lead_id,
        "assignment_cycle_id": "cycle-new",
        "assigned_to_user_id": "owner-b",
        "assigned_at": "2026-10-19T10:01:00Z",
        "assigned_by": "commercial_intake",
        "reason": "inbound_message",
        "source_system": "Prop360",
        "cycle_status": "active",
        "unassigned_at": None,
    }
    db = Database(
        crm_management_results=Collection(),
        crm_events=Collection([{
            "type": "HUMAN_NOTE",
            "timestamp": "2026-10-19T10:00:01Z",
            "text": "lead_cerrado",
        }]),
    )

    findings = _transition_findings(db, lead_id, [previous, current])

    assert [row["issue_code"] for row in findings] == ["owner_change_without_authorization"]
    assert findings[0]["previous_cycle_managed"] is False


def test_holiday_admin_correction_requires_exact_linked_event():
    lead_id = "lead-1"
    source = {
        "lead_id": lead_id,
        "assignment_cycle_id": "sla-reassignment:source",
        "assigned_to_user_id": "intermediate-owner",
    }
    correction = {
        "lead_id": lead_id,
        "assignment_cycle_id": "admin-correction:HOLIDAY_SLA_CORRECTION_2026_10_12:lead-1:v1",
        "assigned_by": "administrative_correction",
        "assigned_to_user_id": "original-owner",
        "notification_eligible": False,
    }
    db = Database(crm_events=Collection(first={"actor": "administrator"}))

    assert _was_transition_administratively_corrected(source, correction, [source, correction], db=db)
    query = db["crm_events"].last_query
    assert query["meta.source_assignment_cycle_id"] == source["assignment_cycle_id"]
    assert query["meta.target_owner_user_id"] == correction["assigned_to_user_id"]


def test_manual_admin_transfer_requires_matching_human_assignment_event():
    previous = {"lead_id": "lead-1", "assignment_cycle_id": "cycle-old"}
    current = {
        "lead_id": "lead-1",
        "assignment_cycle_id": "cycle-new",
        "assigned_to_display_name": "Mariela Arriagada",
        "reason": "manual_exceptional_reassignment",
    }
    db = Database(crm_events=Collection(first={"actor": "Administración"}))

    assert _valid_transfer_transition(previous, current, db=db)
    query = db["crm_events"].last_query
    assert query["assignment_cycle_id"] == current["assignment_cycle_id"]
    assert query["meta.method"] == current["reason"]
    assert query["meta.executive"] == current["assigned_to_display_name"]


def test_sla_transfer_uses_cycle_creation_decision_and_exact_source_target_pair():
    previous = {"assignment_cycle_id": "cycle-source"}
    current = {
        "assignment_cycle_id": "sla-reassignment:creating-decision",
        # This field points to a later rescue and must not shadow the decision
        # encoded by the cycle being audited.
        "reassignment_decision_id": "next-decision",
        "assigned_by": "sla_reassignment",
    }
    audit = Collection(first={"_id": "creating-decision"})
    db = Database(crm_sla_reassignment_audit_v1=audit)

    assert _valid_transfer_transition(previous, current, db=db)
    query = audit.last_query
    assert "creating-decision" in query["_id"]["$in"]
    assert query["source_cycle_id"] == previous["assignment_cycle_id"]
    assert query["new_cycle_id"] == current["assignment_cycle_id"]


def test_chatbot_finding_requires_provider_evidence_and_unique_cycle_at_message_time():
    cycles = [
        {
            "assignment_cycle_id": "cycle-old",
            "assigned_to_display_name": "Erika Garrido",
            "assigned_at": "2026-10-19T09:00:00Z",
            "unassigned_at": "2026-10-19T10:00:00Z",
        },
        {
            "assignment_cycle_id": "cycle-current",
            "assigned_to_display_name": "Mariela Arriagada",
            "assigned_at": "2026-10-19T10:00:00Z",
            "unassigned_at": None,
            "cycle_status": "active",
        },
    ]
    integrity_state = {"status": "conflict"}
    generated_only = {
        "role": "assistant",
        "content": "Erika Garrido te contactará.",
        "timestamp": "2026-10-19T10:01:00Z",
        "delivery_status": "generated",
    }
    delivered_wrong_owner = {
        "role": "assistant",
        "content": "Erika Garrido te contactará.",
        "timestamp": "2026-10-19T10:01:00Z",
        "delivery_status": "accepted",
        "provider_message_id": "provider-message-1",
    }
    delivered_correct_owner = {
        "role": "assistant",
        "content": "Mariela Arriagada te contactará.",
        "timestamp": "2026-10-19T10:01:00Z",
        "delivery_status": "accepted",
        "provider_message_id": "provider-message-2",
    }

    assert not _chatbot_owner_findings(
        {"messages": [generated_only]}, cycles, integrity_state,
    )
    finding = _chatbot_owner_findings(
        {"_id": "lead-1", "messages": [delivered_wrong_owner]}, cycles, integrity_state,
    )
    assert finding[0]["cycle_ids"] == ["cycle-current"]
    assert finding[0]["provider_accepted"] is True
    assert _chatbot_owner_findings(
        {"messages": [delivered_correct_owner]}, cycles, integrity_state,
    ) == []


def test_reconciler_retries_network_timeouts_with_bounded_backoff_and_same_run_id():
    calls = []
    sleeps = []

    def flaky_reconcile(_db, **kwargs):
        calls.append(kwargs["detection_run_id"])
        if len(calls) < 3:
            raise NetworkTimeout("transient")
        return {"status": "completed"}

    with patch.object(integrity, "reconcile_assignment_integrity", side_effect=flaky_reconcile):
        result = integrity.reconcile_assignment_integrity_with_retries(
            object(), sleep=sleeps.append,
        )

    assert result["attempts"] == 3
    assert len(set(calls)) == 1
    assert sleeps == [1.0, 2.0]
