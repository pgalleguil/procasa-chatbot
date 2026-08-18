import asyncio
from datetime import date, datetime, timezone

import mongomock
import pytest

import chatbot.captacion_daily_report as daily
from config import Config


def test_schedule_tuesday_to_friday_maps_to_previous_day():
    assert daily.scheduled_period_for_run(date(2026, 8, 18)) == (date(2026, 8, 17), date(2026, 8, 17))
    assert daily.scheduled_period_for_run(date(2026, 8, 19)) == (date(2026, 8, 18), date(2026, 8, 18))
    assert daily.scheduled_period_for_run(date(2026, 8, 20)) == (date(2026, 8, 19), date(2026, 8, 19))
    assert daily.scheduled_period_for_run(date(2026, 8, 21)) == (date(2026, 8, 20), date(2026, 8, 20))


def test_monday_is_reserved_for_weekly_report():
    assert daily.run_scheduled_production_daily_report
    assert daily.scheduled_period_for_run(date(2026, 8, 17)) == (date(2026, 8, 10), date(2026, 8, 14))


def test_weekends_do_not_schedule():
    assert daily.scheduled_period_for_run(date(2026, 8, 22)) is None
    assert daily.scheduled_period_for_run(date(2026, 8, 23)) is None


def test_scheduler_uses_chile_timezone():
    # 12:30 UTC is 08:30 in Chile during August 2026.
    run_at = datetime(2026, 8, 19, 12, 30, tzinfo=timezone.utc)
    assert run_at.astimezone(daily.CHILE).hour == 8
    assert run_at.astimezone(daily.CHILE).minute == 30


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(8, 29, False), (8, 30, True), (8, 31, True), (9, 15, True), (11, 59, True), (12, 0, False)],
)
def test_daily_production_recovery_window(hour, minute, expected):
    run_at = datetime(2026, 8, 18, hour, minute, tzinfo=daily.CHILE)
    assert daily.daily_production_window_open(run_at) is expected


def test_daily_production_recovery_window_excludes_monday_and_weekends():
    for day in (17, 22, 23):
        run_at = datetime(2026, 8, day, 9, 15, tzinfo=daily.CHILE)
        assert daily.daily_production_window_open(run_at) is False


def test_scheduler_recovers_monday_report_at_0915(monkeypatch):
    calls = []

    async def fake_send(db, report_date):
        calls.append(report_date)
        return {"status": "accepted"}

    monkeypatch.setattr(daily, "send_production_daily_report", fake_send)
    run_at = datetime(2026, 8, 18, 9, 15, tzinfo=daily.CHILE)
    result = asyncio.run(daily.run_scheduled_production_daily_report(_db(), run_at=run_at))
    assert result["status"] == "accepted"
    assert calls == [date(2026, 8, 17)]


def test_scheduler_does_not_recover_after_noon(monkeypatch):
    calls = []

    async def fake_send(db, report_date):
        calls.append(report_date)
        return {"status": "accepted"}

    monkeypatch.setattr(daily, "send_production_daily_report", fake_send)
    run_at = datetime(2026, 8, 18, 12, 0, tzinfo=daily.CHILE)
    result = asyncio.run(daily.run_scheduled_production_daily_report(_db(), run_at=run_at))
    assert result["status"] == "not_scheduled"
    assert calls == []


def _fake_report():
    return {
        "period_label": "17 de agosto",
        "team_done": 18,
        "team_goal": 70,
        "team_compliance": 25.7,
        "total_assigned": 10,
        "total_managed": 2,
        "pending_team": 8,
        "availability_pct": 80.0,
        "coverage_days": 1.14,
        "coverage_below_threshold_count": 1,
        "period_days": 1,
        "executives": [],
    }


def _db():
    return mongomock.MongoClient().db


def _patch_report(monkeypatch):
    monkeypatch.setattr(daily, "calculate_daily_report", lambda db, period: _fake_report())
    monkeypatch.setattr(daily, "build_whatsapp_message", lambda report: "mensaje")
    monkeypatch.setattr(daily, "validate_reconciliation", lambda report, message: {"ok": True})


def test_already_accepted_report_is_not_sent_twice(monkeypatch):
    _patch_report(monkeypatch)
    calls = []

    async def fake_send(recipient, message):
        calls.append((recipient, message))
        return {"success": True, "delivery_status": "accepted", "provider_message_id": "p1", "http_status": 200}

    monkeypatch.setattr(daily, "send_whatsapp_message_detailed", fake_send)
    monkeypatch.setattr(Config, "CAPTACION_DAILY_PRODUCTION_ENABLED", True)
    monkeypatch.setattr(Config, "CAPTACION_TEST_MODE", False)
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", "group@g.us")
    db = _db()
    first = asyncio.run(daily.send_production_daily_report(db, date(2026, 8, 17)))
    second = asyncio.run(daily.send_production_daily_report(db, date(2026, 8, 17)))
    assert first["status"] == "accepted"
    assert second["status"] == "already_claimed"
    assert len(calls) == 1


def test_concurrent_attempts_produce_one_message(monkeypatch):
    _patch_report(monkeypatch)
    calls = []

    async def fake_send(recipient, message):
        calls.append(1)
        return {"success": True, "delivery_status": "accepted", "provider_message_id": "p2", "http_status": 200}

    monkeypatch.setattr(daily, "send_whatsapp_message_detailed", fake_send)
    monkeypatch.setattr(Config, "CAPTACION_DAILY_PRODUCTION_ENABLED", True)
    monkeypatch.setattr(Config, "CAPTACION_TEST_MODE", False)
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", "group@g.us")
    db = _db()
    async def run_both():
        return await asyncio.gather(
            daily.send_production_daily_report(db, date(2026, 8, 18)),
            daily.send_production_daily_report(db, date(2026, 8, 18)),
        )

    results = asyncio.run(run_both())
    assert sum(result["status"] == "accepted" for result in results) == 1
    assert len(calls) == 1


def test_production_never_uses_test_recipient(monkeypatch):
    monkeypatch.setattr(Config, "CAPTACION_DAILY_PRODUCTION_ENABLED", True)
    monkeypatch.setattr(Config, "CAPTACION_TEST_MODE", False)
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", Config.CAPTACION_TEST_RECIPIENT)
    with pytest.raises(PermissionError):
        asyncio.run(daily.send_production_daily_report(_db(), date(2026, 8, 17)))


def test_disabled_production_does_not_send(monkeypatch):
    monkeypatch.setattr(Config, "CAPTACION_DAILY_PRODUCTION_ENABLED", False)
    result = asyncio.run(daily.send_production_daily_report(_db(), date(2026, 8, 17)))
    assert result["status"] == "disabled"


def test_reconciliation_failure_blocks_provider(monkeypatch):
    monkeypatch.setattr(Config, "CAPTACION_DAILY_PRODUCTION_ENABLED", True)
    monkeypatch.setattr(Config, "CAPTACION_TEST_MODE", False)
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", "group@g.us")
    monkeypatch.setattr(daily, "calculate_daily_report", lambda db, period: _fake_report())
    monkeypatch.setattr(daily, "build_whatsapp_message", lambda report: "mensaje")
    monkeypatch.setattr(daily, "validate_reconciliation", lambda report, message: (_ for _ in ()).throw(ValueError("bad")))
    calls = []

    async def fake_send(recipient, message):
        calls.append(1)
        return {"success": True}

    monkeypatch.setattr(daily, "send_whatsapp_message_detailed", fake_send)
    with pytest.raises(ValueError):
        asyncio.run(daily.send_production_daily_report(_db(), date(2026, 8, 17)))
    assert calls == []


def test_failed_provider_is_recorded_for_diagnosis(monkeypatch):
    _patch_report(monkeypatch)
    monkeypatch.setattr(Config, "CAPTACION_DAILY_PRODUCTION_ENABLED", True)
    monkeypatch.setattr(Config, "CAPTACION_TEST_MODE", False)
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", "group@g.us")

    async def fake_send(recipient, message):
        return {"success": False, "delivery_status": "failed", "provider_message_id": None, "http_status": 500}

    monkeypatch.setattr(daily, "send_whatsapp_message_detailed", fake_send)
    db = _db()
    result = asyncio.run(daily.send_production_daily_report(db, date(2026, 8, 17)))
    assert result["status"] == "failed"
    assert db[daily.DAILY_DELIVERY_COLLECTION].find_one({"report_date": "2026-08-17"})["provider_http_status"] == 500


def test_recent_failed_delivery_is_skipped_during_recovery_window(monkeypatch):
    _patch_report(monkeypatch)
    monkeypatch.setattr(Config, "CAPTACION_DAILY_PRODUCTION_ENABLED", True)
    monkeypatch.setattr(Config, "CAPTACION_TEST_MODE", False)
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", "group@g.us")
    calls = []

    async def fake_send(recipient, message):
        calls.append(1)
        return {"success": False, "delivery_status": "failed", "http_status": 500}

    monkeypatch.setattr(daily, "send_whatsapp_message_detailed", fake_send)
    db = _db()
    first = asyncio.run(daily.send_production_daily_report(db, date(2026, 8, 17)))
    second = asyncio.run(daily.send_production_daily_report(db, date(2026, 8, 17)))
    assert first["status"] == "failed"
    assert second["status"] == "already_claimed"
    assert calls == [1]
