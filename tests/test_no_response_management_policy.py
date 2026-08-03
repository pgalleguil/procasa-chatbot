from datetime import datetime, timezone

from chatbot.crm_metrics import calculate_sla, event_evidence


def test_no_respondio_is_managed_and_stops_sla_without_effective_contact():
    event = {
        "lead_id": "lead-1",
        "type": "HUMAN_NOTE",
        "actor": "Mariela",
        "actor_type": "human",
        "result": "NO_RESPONDIO",
        "confirmed": True,
    }

    evidence = event_evidence(event)
    assert evidence["management"] is True
    assert evidence["contact_attempt"] is True
    assert evidence["effective_contact"] is False

    sla = calculate_sla(
        assigned_at=datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc),
        first_valid_management_at=datetime(2026, 8, 3, 14, 50, 59, 295000, tzinfo=timezone.utc),
        now=datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc),
        temperature="HOT",
    )
    assert sla["fulfilled"] is True
    assert sla["status"] == "fulfilled"
