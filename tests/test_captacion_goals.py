from datetime import date, datetime, timedelta

import pytz

from captacion_goals import (
    CAPTACION_DAILY_GOAL,
    CAPTACION_WEEKLY_GOAL,
    _build_period_series,
    build_captacion_goal_dashboard,
    can_manage_captacion,
)
from captacion_management import evaluate_manual_decision


CHILE = pytz.timezone("America/Santiago")


def _at(day, hour=10):
    return CHILE.localize(datetime(2026, 7, day, hour, 0))


def _rows(actor, day, count, prefix="p"):
    return [{"actor": actor, "property_id": f"{prefix}-{index}", "occurred_at": _at(day, 9 + index % 8)} for index in range(count)]


def test_only_meaningful_manual_decisions_are_valid():
    assert evaluate_manual_decision(status="Contacto exitoso", previous_status="Por contactar")["eligible"]
    assert evaluate_manual_decision(status="En gestion", previous_status="Por contactar", notes="Llamar manana")["eligible"]
    assert evaluate_manual_decision(status="Por contactar", previous_status="Disponible")["eligible"]
    assert not evaluate_manual_decision(status="Por contactar", previous_status="Por contactar")["eligible"]
    assert not evaluate_manual_decision(status="Disponible", previous_status="Por contactar")["eligible"]
    assert not evaluate_manual_decision(status="Captado", previous_status="Por contactar", is_automatic=True)["eligible"]


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
    assert result["week_count"] == 12
    assert result["week_goal"] == CAPTACION_WEEKLY_GOAL
    assert result["weekend_activity"] == 12
    assert result["today_goal"] == 0


def test_sunday_has_no_daily_goal_but_preserves_the_workweek():
    rows = _rows("Ana", 13, 10, "mon") + _rows("Ana", 14, 4, "tue")
    result = build_captacion_goal_dashboard([{"name": "Ana"}], rows, "Ana", now=_at(19))
    assert result["today_status"] == "EXENTO"
    assert result["today_reason"] == "Domingo"
    assert result["week_goal"] == 50
    assert result["week_count"] == 14
    assert result["daily"][0]["status"] == "CUMPLIDO"
    assert result["daily"][1]["status"] == "INCUMPLIDO"
    assert result["daily"][5]["target"] == 0
    assert result["daily"][6]["target"] == 0
    assert result["daily"][6]["status"] == "EXENTO"


def test_selected_period_keeps_weekend_production_without_weekend_goal():
    rows = (
        _rows("Ana", 17, 8, "fri")
        + _rows("Ana", 18, 6, "sat")
        + _rows("Ana", 19, 4, "sun")
        + _rows("Ana", 20, 12, "mon")
    )
    result = build_captacion_goal_dashboard(
        [{"name": "Ana"}],
        rows,
        "Ana",
        now=_at(20),
        period_start="2026-07-17",
        period_end="2026-07-20",
    )
    assert result["week_count"] == 30
    assert result["week_goal"] == 20
    assert [item["target"] for item in result["daily"]] == [10, 0, 0, 10]


def test_period_series_keeps_real_weekend_counts_and_flattens_goal():
    start = date(2026, 7, 17)
    days = [start + timedelta(days=index) for index in range(4)]
    result = {
        "executives": [{
            "daily": [
                {"date": day.isoformat(), "count": count, "target": target}
                for day, count, target in zip(days, [8, 6, 4, 12], [10, 0, 0, 10])
            ]
        }]
    }
    series = _build_period_series(result, days, [], {})
    assert [item["current"] for item in series] == [8, 6, 4, 12]
    assert [item["current_cumulative"] for item in series] == [8, 14, 18, 30]
    assert [item["target"] for item in series] == [10, 0, 0, 10]
    assert [item["target_cumulative"] for item in series] == [10, 10, 10, 20]


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


def test_user_id_is_primary_for_attribution_and_daily_states_are_explicit():
    targets = {
        f"2026-07-{day}": {"target": 10, "exempt": False, "reason": None, "close_hour": 19}
        for day in range(13, 18)
    }
    team = [{
        "id": "u1",
        "name": "Ana",
        "day_targets": targets,
        "daily_metrics": {"2026-07-15": {"contact_attempts": 5, "effective_contacts": 3, "captures": 1}},
        "anomaly_count": 2,
    }]
    rows = [{"actor": "Nombre antiguo", "actor_user_id": "u1", "property_id": f"p-{index}", "occurred_at": _at(15)} for index in range(7)]
    result = build_captacion_goal_dashboard(team, rows, "Ana", now=_at(15, 18))
    assert result["today_count"] == 7
    assert result["today_status"] == "EN_PROGRESO"
    assert result["contact_attempts"] == 5
    assert result["effective_contacts"] == 3
    assert result["captures"] == 1
    assert result["anomaly_count"] == 2


def test_team_total_includes_verified_rows_from_every_member():
    targets = {
        f"2026-07-{day}": {"target": 10, "exempt": False, "reason": None, "close_hour": 19}
        for day in range(13, 18)
    }
    team = [
        {"id": "u-susana", "name": "Susana", "day_targets": targets},
        {"id": "u-mariela", "name": "Mariela", "day_targets": targets},
        {"id": "u-paula", "name": "Paula", "day_targets": targets},
        {"id": "u-erika", "name": "Erika", "day_targets": targets},
    ]
    rows = (
        [{"actor_user_id": "u-susana", "property_id": f"s-{index}", "occurred_at": _at(16)} for index in range(5)]
        + [{"actor_user_id": "u-mariela", "property_id": f"m-{index}", "occurred_at": _at(16)} for index in range(2)]
        + [{"actor_user_id": "u-paula", "property_id": "p-1", "occurred_at": _at(15)}]
    )
    result = build_captacion_goal_dashboard(team, rows, now=_at(19))
    by_name = {row["name"]: row for row in result["executives"]}
    assert result["week_count"] == 8
    assert result["week_goal"] == 200
    assert by_name["Susana"]["week_count"] == 5
    assert by_name["Mariela"]["week_count"] == 2
    assert by_name["Paula"]["week_count"] == 1
    assert by_name["Erika"]["week_count"] == 0


def test_team_outcomes_exclude_credited_activity_from_non_member():
    team = [{"id": "u1", "name": "Ana"}]
    rows = [
        {"actor_user_id": "u1", "property_id": "p-team", "occurred_at": _at(15), "result": "contacted", "credited": True},
        {"actor_user_id": "former-user", "property_id": "p-former", "occurred_at": _at(15), "result": "captured", "credited": True},
    ]
    result = build_captacion_goal_dashboard(team, rows, now=_at(15))
    assert result["week_count"] == 1
    assert sum(group["total"] for group in result["outcome_groups"].values()) == 1
    assert result["outcome_groups"]["management_in_progress"]["total"] == 1
