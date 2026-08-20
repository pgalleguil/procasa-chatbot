from datetime import date

from chatbot.captacion_daily_report import (
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
        "coverage_below_threshold_count": 0,
        "executives": [
            {"name": "A", "gestiones_dia": 10, "cumplimiento_dia": 100.0, "total_asignadas": 20,
             "total_gestionadas_acumuladas": 8, "avance_cartera": 40.0, "pendientes": 12},
            {"name": "B", "gestiones_dia": 3, "cumplimiento_dia": 30.0, "total_asignadas": 10,
             "total_gestionadas_acumuladas": 3, "avance_cartera": 30.0, "pendientes": 7},
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
    assert "*Cobertura:* Todos con ≥10 días de datos" in message
    assert "```" not in message
    assert "Disponibilidad:" in message
    assert "Todos con ≥10 días de datos" in message


def test_scheduler_calendar_is_closed_and_weekends_are_disabled():
    assert scheduled_period_for_run(date(2026, 8, 18)) == (date(2026, 8, 17), date(2026, 8, 17))
    assert scheduled_period_for_run(date(2026, 8, 17)) == (date(2026, 8, 10), date(2026, 8, 14))
    assert scheduled_period_for_run(date(2026, 8, 22)) is None
    assert scheduled_period_for_run(date(2026, 8, 23)) is None


def test_daily_target_is_ten():
    assert DAILY_TARGET == 10

