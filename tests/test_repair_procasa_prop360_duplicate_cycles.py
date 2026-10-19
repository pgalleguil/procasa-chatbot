from copy import deepcopy
from datetime import datetime, timezone

import pytest

from maintenance import repair_procasa_prop360_duplicate_cycles as repair


def _case_snapshot(case_name):
    case = repair.CASES[case_name]
    original_id = case["original_cycle_id"]
    duplicate_id = case["duplicate_cycle_id"]
    lead_id = case["lead_id"]
    owner_id = case["owner_user_id"]
    duplicate_assigned = datetime(2026, 10, 19, 12, 0, 34, 613000, tzinfo=timezone.utc)
    if case_name == "5806":
        duplicate_assigned = datetime(2026, 10, 19, 12, 0, 10, 37000, tzinfo=timezone.utc)
    original_assigned = datetime(2026, 10, 18, 15, 21, 51, 633000, tzinfo=timezone.utc)
    if case_name == "5806":
        original_assigned = datetime(2026, 10, 18, 0, 43, 43, 722000, tzinfo=timezone.utc)
    original_management = [row for row in case["management"].values() if row["cycle_id"] == original_id]
    duplicate_management = [row for row in case["management"].values() if row["cycle_id"] == duplicate_id]
    original_first = min(row["occurred_at"] for row in original_management)
    duplicate_first = min(row["occurred_at"] for row in duplicate_management)
    sla_start = duplicate_assigned.replace(microsecond=(duplicate_assigned.microsecond // 1000) * 1000)
    lead = {
        "_id": lead_id,
        "phone": "+56912345678",
        "sla_status": "fulfilled",
        "lead_temperature_effective": "COLD",
        "ejecutivo_asignado": case["owner_name"],
        "prospecto": {"codigo": case["property_code"], "ejecutivo": case["owner_name"]},
        "lifecycle": {
            "current_assignment_cycle_id": duplicate_id,
            "assignment_cycle_id": duplicate_id,
            "assigned_to_user_id": owner_id,
            "assigned_to_display_name": case["owner_name"],
            "assigned_at": duplicate_assigned.isoformat(),
            "cycle_started_at": original_assigned,
            "sla_started_at": sla_start,
            "first_valid_management_at": original_first,
            "first_contact_attempt_at": original_first,
            "first_effective_contact_at": original_first,
        },
    }
    original = {
        "_id": f"cycle-{case_name}-original",
        "lead_id": lead_id,
        "assignment_cycle_id": original_id,
        "assigned_to_user_id": owner_id,
        "assigned_to_display_name": case["owner_name"],
        "property_code": case["property_code"],
        "assigned_at": original_assigned,
        "cycle_started_at": original_assigned,
        "sla_started_at": sla_start,
        "sla_segments": [{"policy": "NON_HOT", "segment_start": sla_start, "segment_end": None}],
        "temperature_at_assignment": "COLD",
        "cycle_status": "closed",
        "unassigned_at": duplicate_assigned,
        "closed_at": duplicate_assigned,
        "closed_reason": "cycle_replaced",
        "cycle_version": 1,
        "first_valid_management_at": original_first,
        "first_effective_contact_at": original_first,
        "sla_first_management_status": "completed",
    }
    duplicate = {
        "_id": f"cycle-{case_name}-duplicate",
        "lead_id": lead_id,
        "assignment_cycle_id": duplicate_id,
        "assigned_to_user_id": owner_id,
        "assigned_to_display_name": case["owner_name"],
        "property_code": case["property_code"],
        "assigned_at": duplicate_assigned,
        "sla_started_at": duplicate_assigned,
        "sla_segments": [{"policy": "NON_HOT", "segment_start": duplicate_assigned, "segment_end": None}],
        "temperature_at_assignment": "COLD",
        "cycle_status": "active",
        "unassigned_at": None,
        "cycle_version": 1,
        "source_system": "prop360",
        "source_event_id": case["prop360_event_id"],
        "first_valid_management_at": duplicate_first,
        "sla_first_management_status": "completed",
    }
    acceptance = datetime(2026, 10, 18, 15, 22, 42, 989000, tzinfo=timezone.utc)
    if case_name == "5806":
        acceptance = datetime(2026, 10, 18, 0, 44, 11, 355000, tzinfo=timezone.utc)
    management = []
    for record_id, expected in case["management"].items():
        management.append({
            "_id": record_id,
            "lead_id": lead_id,
            "assignment_cycle_id": expected["cycle_id"],
            "actor_user_id": owner_id,
            "result_type": expected["result_type"],
            "status": "completed",
            "occurred_at": expected["occurred_at"],
        })
    notification = {
        "_id": f"notification-{case_name}",
        "state": "sent",
        "lead_ids": [lead_id],
        "assignment_cycle_ids": [original_id],
        "provider_message_id": "provider-accepted-original",
        "delivery_attempts": [{
            "provider_http_status": 200,
            "provider_message_id": "provider-accepted-original",
            "result": "sent",
            "completed_at": acceptance,
        }],
    }
    return {
        "lead": [lead],
        "cycles": [original, duplicate],
        "management": management,
        "events": [],
        "alerts": [],
        "sla_alerts": [],
        "notifications": [notification],
        "pending_notifications": [],
        "active_jobs": [],
        "ingest": [{
            "source_system": "prop360",
            "source_event_id": case["prop360_event_id"],
            "lead_id": lead_id,
            "property_code": case["property_code"],
        }],
        "tasks": [],
        "visits": [],
        "visit_orders": [],
    }


@pytest.mark.parametrize("case_name", ["17252-other", "5806"])
def test_reviewed_duplicate_cases_require_original_provider_acceptance_and_preserve_human_history(case_name):
    snapshot = _case_snapshot(case_name)
    lead, original, duplicate, accepted = repair._validate_scenario(case_name, snapshot)

    assert lead["sla_status"] == "fulfilled"
    assert original["cycle_status"] == "closed"
    assert duplicate["cycle_status"] == "active"
    assert accepted["provider_http_status"] == 200
    assert accepted["accepted_at"] < duplicate["assigned_at"]
    assert len(snapshot["management"]) == len(repair.CASES[case_name]["management"])


def test_prop360_cycle_is_not_used_as_sla_start_when_original_acceptance_is_missing():
    snapshot = _case_snapshot("5806")
    notification = deepcopy(snapshot["notifications"][0])
    notification["delivery_attempts"][0].pop("completed_at")
    snapshot["notifications"] = [notification]

    with pytest.raises(repair.RepairSafetyError, match="SLA start will not be guessed"):
        repair._validate_scenario("5806", snapshot)


def test_validation_aborts_if_any_human_management_record_changed():
    snapshot = _case_snapshot("17252-other")
    snapshot["management"][0]["occurred_at"] = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)

    with pytest.raises(repair.RepairSafetyError, match="management evidence changed"):
        repair._validate_scenario("17252-other", snapshot)


def test_second_active_cycle_or_wrong_owner_is_never_treated_as_a_repairable_duplicate():
    snapshot = _case_snapshot("5806")
    snapshot["cycles"].append(deepcopy(snapshot["cycles"][0]))

    with pytest.raises(repair.RepairSafetyError, match="exactly the individually reviewed two cycles"):
        repair._validate_scenario("5806", snapshot)


@pytest.mark.parametrize("case_name", ["17252-other", "5806"])
def test_postcondition_chatbot_lookup_uses_transaction_snapshot(case_name):
    from chatbot.crm_assignment_integrity import assignment_context
    import mongomock

    snapshot = _case_snapshot(case_name)
    case = repair.CASES[case_name]
    lead = deepcopy(snapshot["lead"][0])
    lead["lifecycle"]["current_assignment_cycle_id"] = case["original_cycle_id"]
    lead["lifecycle"]["assignment_cycle_id"] = case["original_cycle_id"]
    cycles = deepcopy(snapshot["cycles"])
    original = next(row for row in cycles if row["assignment_cycle_id"] == case["original_cycle_id"])
    duplicate = next(row for row in cycles if row["assignment_cycle_id"] == case["duplicate_cycle_id"])
    original["cycle_status"] = "active"
    original["unassigned_at"] = None
    duplicate["cycle_status"] = "closed"
    duplicate["unassigned_at"] = datetime.now(timezone.utc)

    # The database deliberately contains no cycles: in a transaction the helper
    # must use the snapshot override, since a separate read cannot see writes.
    context = assignment_context(
        mongomock.MongoClient().incident, lead, active_cycles_override=cycles
    )
    assert context["status"] == "consistent"
    assert context["cycle"]["assignment_cycle_id"] == case["original_cycle_id"]


@pytest.mark.parametrize("case_name", ["17252-other", "5806"])
def test_audit_event_acceptance_time_matches_naive_mongo_utc(case_name):
    snapshot = _case_snapshot(case_name)
    case = repair.CASES[case_name]
    _, _, duplicate, accepted = repair._validate_scenario(case_name, snapshot)
    audit = {
        "type": "ADMINISTRATIVE_PROP360_DUPLICATE_CYCLE_REPAIR",
        "meta": {
            "incident_id": case["incident_id"],
            "provider_accepted_at": accepted["accepted_at"].replace(tzinfo=None),
            "customer_message_sent": False,
            "assignment_notification_sent": False,
        },
    }

    assert repair._audit_event_matches(audit, case, accepted["accepted_at"])
    audit["meta"]["provider_accepted_at"] = duplicate["assigned_at"]
    assert not repair._audit_event_matches(audit, case, accepted["accepted_at"])

