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
