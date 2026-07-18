from datetime import datetime

import pytz

from captacion_goals import CAPTACION_DAILY_GOAL, CAPTACION_WEEKLY_GOAL, build_captacion_goal_dashboard, can_manage_captacion, is_valid_captacion_action


CHILE = pytz.timezone("America/Santiago")


def _at(day, hour=10):
    return CHILE.localize(datetime(2026, 7, day, hour, 0))


def _rows(actor, day, count, prefix="p"):
    return [{"actor": actor, "property_id": f"{prefix}-{index}", "occurred_at": _at(day, 9 + index % 8)} for index in range(count)]


def test_only_real_commercial_actions_are_valid():
    assert is_valid_captacion_action("call_initiated", "tel")
    assert is_valid_captacion_action("message_sent", "wa")
    assert is_valid_captacion_action("message_sent", "email")
    assert is_valid_captacion_action("manual_contact", "manual", message="Contacto registrado")
    assert not is_valid_captacion_action("status_changed", "crm")
    assert not is_valid_captacion_action("assignment_changed", "crm")
    assert not is_valid_captacion_action("manual_contact", "manual")


def test_daily_dedup_is_property_actor_and_local_day():
    rows = [
        {"actor": "Ana", "property_id": "p-1", "occurred_at": _at(13, 9)},
        {"actor": "Ana", "property_id": "p-1", "occurred_at": _at(13, 12)},
        {"actor": "Ana", "property_id": "p-1", "occurred_at": _at(14, 9)},
    ]
    result = build_captacion_goal_dashboard([{"name": "Ana"}], rows, "Ana", now=_at(14))
    assert result["daily"][0]["count"] == 1
    assert result["daily"][1]["count"] == 1
    assert result["week_count"] == 2


def test_week_total_does_not_hide_a_failed_workday():
    rows = []
    for day, count in ((13, 20), (14, 0), (15, 10), (16, 10), (17, 10)):
        rows.extend(_rows("Ana", day, count, f"d{day}"))
    result = build_captacion_goal_dashboard([{"name": "Ana"}], rows, "Ana", now=_at(17))
    assert result["week_count"] == CAPTACION_WEEKLY_GOAL
    assert result["days_met"] == 4
    assert result["daily"][1]["met"] is False


def test_weekend_activity_is_additional_and_does_not_compensate():
    result = build_captacion_goal_dashboard([{"name": "Ana"}], _rows("Ana", 18, 12, "sat"), "Ana", now=_at(18))
    assert result["week_count"] == 0
    assert result["weekend_activity"] == 12
    assert result["today_goal"] == 0


def test_team_goal_uses_only_active_capture_members_supplied():
    team = [{"name": "Ana"}, {"name": "Beto"}]
    rows = _rows("Ana", 15, 10, "ana") + _rows("Beto", 15, 4, "beto")
    result = build_captacion_goal_dashboard(team, rows, now=_at(15))
    assert result["member_count"] == 2
    assert result["today_goal"] == 2 * CAPTACION_DAILY_GOAL
    assert result["week_goal"] == 2 * CAPTACION_WEEKLY_GOAL
    assert result["executives_met_today"] == 1
    assert result["executives_pending_today"] == 1


def test_management_permission_uses_role_or_exact_assignment():
    prop = {"gestion": {"ejecutivo_asignado": "Ana Pérez"}}
    assert can_manage_captacion({"rol": "agente", "nombre": "Ana Pérez"}, prop)
    assert not can_manage_captacion({"rol": "agente", "nombre": "Otra Persona"}, prop)
    for role in ("admin", "supervisor", "jefatura"):
        assert can_manage_captacion({"rol": role, "nombre": "Jefe"}, prop)


def test_capture_mutation_routes_apply_the_same_backend_permission_gate():
    source = open("webhook.py", encoding="utf-8").read()
    assert source.count("can_manage_captacion(user_doc, captacion_doc)") >= 2
    assert "can_manage_captacion(user_doc, data)" in source
    assert "can_manage_captacion(user, data)" in source
    assert 'status_code=403, detail="No autorizado para gestionar esta captación"' in source
