from datetime import datetime, timezone

import pytest

from maintenance import repair_procasa_p0_17252 as repair


def _notification(*, accepted_at=None, status=200, result="sent", provider_id="provider-1"):
    row = {
        "_id": "notification-1",
        "state": "sent",
        "lead_ids": [repair.LEAD_OID],
        "assignment_cycle_ids": [repair.ORIGINAL_CYCLE_ID],
        "provider_message_id": provider_id,
        "created_at": datetime(2026, 10, 19, 12, 0, 41, tzinfo=timezone.utc),
        "effective_delivery_at": datetime(2026, 10, 19, 12, 15, 0, tzinfo=timezone.utc),
        "delivery_attempts": [{
            "completed_at": accepted_at,
            "provider_http_status": status,
            "provider_message_id": provider_id,
            "result": result,
        }],
    }
    return row


def test_17252_sla_origin_uses_provider_acceptance_not_creation_or_delivery():
    accepted_at = datetime(2026, 10, 19, 12, 6, 18, 25000, tzinfo=timezone.utc)

    proof = repair._provider_acceptance_evidence([
        _notification(accepted_at=accepted_at),
    ])

    assert proof["accepted_at"] == accepted_at
    assert proof["accepted_at"] != datetime(2026, 10, 19, 12, 0, 41, tzinfo=timezone.utc)
    assert proof["accepted_at"] != datetime(2026, 10, 19, 12, 15, 0, tzinfo=timezone.utc)
    assert proof["timestamp_source"].endswith("completed_at_after_provider_2xx")


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_notification(accepted_at=None)],
        [_notification(accepted_at=datetime(2026, 10, 19, 12, 6, tzinfo=timezone.utc), status=500)],
        [_notification(accepted_at=datetime(2026, 10, 19, 12, 6, tzinfo=timezone.utc), result="unknown")],
        [
            _notification(accepted_at=datetime(2026, 10, 19, 12, 6, tzinfo=timezone.utc)),
            {**_notification(accepted_at=datetime(2026, 10, 19, 12, 7, tzinfo=timezone.utc)), "_id": "notification-2"},
        ],
    ],
)
def test_17252_provider_sla_origin_fails_closed_without_unique_acceptance(rows):
    with pytest.raises(repair.RepairSafetyError, match="refusing to infer an SLA start"):
        repair._provider_acceptance_evidence(rows)


def test_17252_preserves_later_effective_contact_without_recounting_first_sla():
    source = {
        "_id": repair.VALID_MANAGEMENT_ID,
        "assignment_cycle_id": repair.ORIGINAL_CYCLE_ID,
        "actor_user_id": repair.MARIELA_USER_ID,
        "result_type": "EFFECTIVE_CONTACT",
        "status": "completed",
        "occurred_at": datetime(2026, 10, 19, 12, 20, 26, 254000, tzinfo=timezone.utc),
    }
    duplicate_cycle = {
        "assignment_cycle_id": repair.DUPLICATE_CYCLE_ID,
        "first_valid_management_at": repair.DUPLICATE_MANAGEMENT_AT,
        "first_effective_contact_at": repair.DUPLICATE_MANAGEMENT_AT,
    }
    duplicate_result = {
        "_id": repair.DUPLICATE_MANAGEMENT_ID,
        "assignment_cycle_id": repair.DUPLICATE_CYCLE_ID,
        "actor_user_id": repair.MARIELA_USER_ID,
        "result_type": "EFFECTIVE_CONTACT",
        "status": "completed",
        "occurred_at": repair.DUPLICATE_MANAGEMENT_AT,
    }

    original, follow_up = repair._validated_management_history(
        [source, duplicate_result],
        {"first_valid_management_at": source["occurred_at"]},
        duplicate_cycle,
    )

    assert original["_id"] == repair.VALID_MANAGEMENT_ID
    assert follow_up["_id"] == repair.DUPLICATE_MANAGEMENT_ID
    assert original["assignment_cycle_id"] == repair.ORIGINAL_CYCLE_ID
    assert follow_up["assignment_cycle_id"] == repair.DUPLICATE_CYCLE_ID
    assert follow_up["occurred_at"] > repair.HOT_SINCE_AT
    with pytest.raises(repair.RepairSafetyError, match="refusing to discard"):
        repair._validated_management_history([source], {"first_valid_management_at": source["occurred_at"]}, duplicate_cycle)


@pytest.mark.parametrize("issue_code", [
    "chatbot_customer_message_assignment_conflict",
    "commercial_intake_blocked_by_assignment_conflict",
    "canonical_cycle_mirror_inconsistency",
])
def test_17252_resolves_only_pointer_alerts_for_the_duplicate_cycle(issue_code):
    alert = {
        "issue_code": issue_code,
        "status": "open",
        "evidence": {
            "issues": ["cycle_pointer_mismatch"],
            "active_cycle_ids": [repair.DUPLICATE_CYCLE_ID],
        },
    }
    assert repair._is_target_pointer_alert(alert)
    assert not repair._is_target_pointer_alert({
        **alert, "evidence": {"issues": ["other_issue"], "active_cycle_ids": [repair.DUPLICATE_CYCLE_ID]},
    })
