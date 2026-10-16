from datetime import datetime, timedelta, timezone

from captacion_goals import _capture_control_payload


def test_capture_control_reconciles_stock_unique_activity_and_attention():
    now = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
    team = [
        {"id": "u1", "name": "Ana"},
        {"id": "u2", "name": "Beto"},
    ]
    current_properties = [
        {
            "id": "p1",
            "title": "Casa 1",
            "comuna": "Ñuñoa",
            "origin": "toctoc",
            "executive_id": "u1",
            "status": "En gestión",
            "assigned_at": now - timedelta(days=2),
            "first_action_at": now - timedelta(days=1),
            "last_management_at": now - timedelta(hours=2),
            "next_followup": now - timedelta(hours=1),
        },
        {
            "id": "p2",
            "title": "Casa 2",
            "comuna": "La Reina",
            "origin": "yapo",
            "executive_id": "u2",
            "status": "En gestión",
            "assigned_at": now - timedelta(days=1),
            "first_action_at": now - timedelta(hours=3),
            "last_management_at": now - timedelta(hours=3),
        },
        {
            "id": "p3",
            "title": "Casa 3",
            "comuna": "Macul",
            "origin": "toctoc",
            "executive_id": "u1",
            "status": "Por contactar",
            "assigned_at": now - timedelta(days=12),
            "first_action_at": None,
            "last_management_at": None,
        },
    ]
    rows = [
        {
            "event_id": "e1",
            "property_id": "p1",
            "actor_user_id": "u1",
            "credited": True,
            "contact_attempt": True,
            "contact_effective": True,
            "event_type": "management_confirmed",
            "result": "contacted",
            "occurred_at": now - timedelta(hours=2),
        },
        {
            "event_id": "e2",
            "property_id": "p2",
            "actor_user_id": "u2",
            "credited": True,
            "contact_attempt": True,
            "contact_effective": False,
            "event_type": "management_confirmed",
            "result": "no_answer",
            "occurred_at": now - timedelta(hours=3),
        },
    ]
    result = {
        "mode": "team",
        "week_goal": 20,
        "response_total": 2,
        "executives": [
            {"user_id": "u1", "name": "Ana", "daily": []},
            {"user_id": "u2", "name": "Beto", "daily": []},
        ],
    }

    payload = _capture_control_payload(
        None, result, team, rows, [], current_properties, unassigned_count=4, now=now
    )

    assert payload["kpis"]["assigned_current"] == 3
    assert payload["kpis"]["managed_unique_period"] == 2
    assert payload["kpis"]["coverage_current_pct"] == 66.7
    ana = next(row for row in payload["executives"] if row["user_id"] == "u1")
    assert ana["managed_unique_current_portfolio"] == 1
    assert ana["managed_unique_current_portfolio"] / ana["assigned_count"] * 100 == 50
    assert payload["kpis"]["contact_attempts"] == 2
    assert payload["kpis"]["effective_contacts"] == 1
    assert payload["backlog"]["total"] == 1
    assert payload["backlog"]["unassigned"] == 4
    assert payload["aging"]["team"]["d_11_20"] == 1
    assert payload["followups"]["overdue"] == 1
    assert payload["source_performance"][0]["managed_unique"] == 1
