from datetime import date

from chatbot.captacion_daily_report import (
    COVERAGE_THRESHOLD_DAYS,
    DAILY_TARGET,
    build_whatsapp_message,
    scheduled_period_for_run,
    validate_reconciliation,
)


def _sample_report():
    return {
        "period": "2026-08-17",
        "period_end": "2026-08-17",
        "period_label": "17 de agosto",
        "period_days": 1,
        "team_size": 2,
        "team_goal": 20,
        "team_done": 13,
        "team_compliance": 65.0,
        "total_assigned": 30,
        "total_managed": 11,
        "pending_team": 19,
        "availability_pct": 19 / 30 * 100,
        "coverage_days": 19 / 20,
        "coverage_below_threshold_count": 1,
        "executives": [
            {"name": "A", "gestiones_dia": 10, "cumplimiento_dia": 100.0, "total_asignadas": 20,
             "total_gestionadas_acumuladas": 8, "avance_cartera": 40.0, "pendientes": 12,
             "cobertura_dias": 1.2},
            {"name": "B", "gestiones_dia": 3, "cumplimiento_dia": 30.0, "total_asignadas": 10,
             "total_gestionadas_acumuladas": 3, "avance_cartera": 30.0, "pendientes": 7,
             "cobertura_dias": 0.7},
        ],
    }


def test_message_is_compact_and_reconciles():
    report = _sample_report()
    message = build_whatsapp_message(report)
    checks = validate_reconciliation(report, message)
    assert all(checks.values())
    assert "👤 *Gestión Diaria de Captación | 17 de agosto*" in message
    assert "👥 *Equipo*\nCumplimiento diario: *13/20 · 65%*" in message
    assert "*A* ██████████" in message
    assert "Día: *10/10 · 100%* · Avance: 8 de 20 · 40,0%" in message
    assert "Cobertura de cartera: 2 ejecutivos bajo 5 días" in message
    assert "A bajo 5 días · 12 pendientes ≈ 1,2 días" in message
    assert "B bajo 5 días · 7 pendientes ≈ 0,7 días" in message
    assert "```" not in message
    assert "Disponibilidad:" in message
    assert "datos" not in message


def test_scheduler_calendar_is_closed_and_weekends_are_disabled():
    assert scheduled_period_for_run(date(2026, 8, 18)) == (date(2026, 8, 17), date(2026, 8, 17))
    assert scheduled_period_for_run(date(2026, 8, 17)) == (date(2026, 8, 10), date(2026, 8, 14))
    assert scheduled_period_for_run(date(2026, 8, 22)) is None
    assert scheduled_period_for_run(date(2026, 8, 23)) is None


def test_daily_target_is_ten():
    assert DAILY_TARGET == 10


def test_coverage_threshold_is_five_days():
    assert COVERAGE_THRESHOLD_DAYS == 5


def test_coverage_message_when_everyone_has_at_least_five_days():
    report = _sample_report()
    report["coverage_below_threshold_count"] = 0
    for row in report["executives"]:
        row["pendientes"] = 50
        row["cobertura_dias"] = 5
    message = build_whatsapp_message(report)
    assert "Cobertura de cartera: Todos con ≥5 días disponibles" in message
    assert "datos" not in message


def test_coverage_message_when_one_executive_is_below_five_days():
    report = _sample_report()
    report["executives"] = [report["executives"][0]]
    report["executives"][0].update({"name": "Hernán", "pendientes": 43, "cobertura_dias": 4.3})
    report["coverage_below_threshold_count"] = 1
    message = build_whatsapp_message(report)
    assert "Cobertura de cartera: Hernán bajo 5 días · 43 pendientes ≈ 4,3 días" in message


def test_coverage_message_uses_singular_and_plural_labels():
    report = _sample_report()
    report["executives"] = report["executives"][:2]
    for row in report["executives"]:
        row["cobertura_dias"] = 4
    report["coverage_below_threshold_count"] = 2
    message = build_whatsapp_message(report)
    assert "2 ejecutivos bajo 5 días" in message
    assert "1 ejecutivos" not in message

