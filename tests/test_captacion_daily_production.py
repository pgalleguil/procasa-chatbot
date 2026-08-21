import asyncio
import threading
from datetime import date, datetime, timedelta, timezone

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


def test_canonical_commercial_group_has_priority_for_daily(monkeypatch):
    monkeypatch.setattr(Config, "PROCASA_COMMERCIAL_GROUP_ID", "canonical@g.us")
    monkeypatch.setattr(Config, "DAILY_REPORT_GROUP_ID", "legacy-daily@g.us")
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", "legacy-production@g.us")
    assert Config.resolve_daily_group_id() == "canonical@g.us"


def test_daily_group_falls_back_to_legacy_destination(monkeypatch):
    monkeypatch.setattr(Config, "PROCASA_COMMERCIAL_GROUP_ID", "")
    monkeypatch.setattr(Config, "DAILY_REPORT_GROUP_ID", "legacy-daily@g.us")
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", "legacy-production@g.us")
    assert Config.resolve_daily_group_id() == "legacy-daily@g.us"


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
        "team_size": 1,
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
    monkeypatch.setattr(Config, "PROCASA_COMMERCIAL_GROUP_ID", "")
    monkeypatch.setattr(Config, "DAILY_REPORT_GROUP_ID", "")
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
    db = _db()
    with pytest.raises(ValueError):
        asyncio.run(daily.send_production_daily_report(db, date(2026, 8, 17)))
    assert calls == []
    delivery = db[daily.DAILY_DELIVERY_COLLECTION].find_one({"report_date": "2026-08-17"})
    assert delivery["status"] == "failed"


def test_provider_exception_is_recorded_as_failed(monkeypatch):
    _patch_report(monkeypatch)
    monkeypatch.setattr(Config, "CAPTACION_DAILY_PRODUCTION_ENABLED", True)
    monkeypatch.setattr(Config, "CAPTACION_TEST_MODE", False)
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", "group@g.us")

    async def fake_send(recipient, message):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(daily, "send_whatsapp_message_detailed", fake_send)
    db = _db()
    with pytest.raises(RuntimeError, match="provider unavailable"):
        asyncio.run(daily.send_production_daily_report(db, date(2026, 8, 17)))
    delivery = db[daily.DAILY_DELIVERY_COLLECTION].find_one({"report_date": "2026-08-17"})
    assert delivery["status"] == "failed"
    assert delivery["error"] == "provider unavailable"


def test_empty_daily_report_is_not_sent_or_retried(monkeypatch, caplog):
    monkeypatch.setattr(Config, "CAPTACION_DAILY_PRODUCTION_ENABLED", True)
    monkeypatch.setattr(Config, "CAPTACION_TEST_MODE", False)
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", "group@g.us")
    monkeypatch.setattr(
        daily,
        "calculate_daily_report",
        lambda db, period: {"team_size": 0, "executives": []},
    )
    calls = []

    async def fake_send(recipient, message):
        calls.append(1)
        return {"success": True}

    monkeypatch.setattr(daily, "send_whatsapp_message_detailed", fake_send)
    db = _db()
    with caplog.at_level("WARNING"):
        first = asyncio.run(daily.send_production_daily_report(db, date(2026, 8, 20)))
    second = asyncio.run(daily.send_production_daily_report(db, date(2026, 8, 20)))
    delivery = db[daily.DAILY_DELIVERY_COLLECTION].find_one({"report_date": "2026-08-20"})
    assert first["status"] == daily.DAILY_NO_DATA_STATUS
    assert second["status"] == "already_claimed"
    assert delivery["status"] == daily.DAILY_NO_DATA_STATUS
    assert calls == []
    assert "status=skipped_no_data" in caplog.text
    assert "report_date=2026-08-20" in caplog.text
    assert "reason=no_applicable_executives" in caplog.text


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


def _claim_key(day="2026-08-20"):
    return f"daily:{day}:group@g.us"


def _seed_delivery(db, *, status, age_seconds=0, **fields):
    now = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    document = {
        "idempotency_key": _claim_key(),
        "report_type": "daily",
        "report_date": "2026-08-20",
        "recipient": "group@g.us",
        "status": status,
        "generated_at": now,
        "updated_at": now,
        **fields,
    }
    db[daily.DAILY_DELIVERY_COLLECTION].insert_one(document)
    return document


def _run_claim(db):
    return asyncio.run(daily._claim_daily_delivery(db, _claim_key(), date(2026, 8, 20), "group@g.us"))


def test_first_claim_is_atomic_and_claimed():
    delivery, claimed = _run_claim(_db())
    assert claimed is True
    assert delivery["status"] == "sending"


@pytest.mark.parametrize("status", ["accepted", "delivered", "read"])
def test_terminal_delivery_states_are_never_retried(status):
    db = _db()
    _seed_delivery(db, status=status)
    _, claimed = _run_claim(db)
    assert claimed is False


def test_recent_sending_delivery_is_not_retried():
    db = _db()
    _seed_delivery(db, status="sending", age_seconds=daily.DAILY_PRODUCTION_RETRY_COOLDOWN_SECONDS - 1)
    _, claimed = _run_claim(db)
    assert claimed is False


def test_stale_sending_delivery_is_recovered():
    db = _db()
    _seed_delivery(db, status="sending", age_seconds=daily.DAILY_PRODUCTION_RETRY_COOLDOWN_SECONDS + 1)
    delivery, claimed = _run_claim(db)
    assert claimed is True
    assert delivery["status"] == "sending"


def test_concurrent_stale_sending_recovery_has_one_winner():
    db = _db()
    db[daily.DAILY_DELIVERY_COLLECTION].create_index("idempotency_key", unique=True)
    _seed_delivery(db, status="sending", age_seconds=daily.DAILY_PRODUCTION_RETRY_COOLDOWN_SECONDS + 1)

    async def run_both():
        return await asyncio.gather(
            daily._claim_daily_delivery(db, _claim_key(), date(2026, 8, 20), "group@g.us"),
            daily._claim_daily_delivery(db, _claim_key(), date(2026, 8, 20), "group@g.us"),
        )

    results = asyncio.run(run_both())
    assert sum(claimed for _, claimed in results) == 1


@pytest.mark.parametrize("age_seconds, expected", [
    (daily.DAILY_PRODUCTION_RETRY_COOLDOWN_SECONDS - 1, False),
    (daily.DAILY_PRODUCTION_RETRY_COOLDOWN_SECONDS + 1, True),
])
def test_failed_delivery_respects_cooldown(age_seconds, expected):
    db = _db()
    failed_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    _seed_delivery(db, status="failed", age_seconds=age_seconds, failed_at=failed_at)
    _, claimed = _run_claim(db)
    assert claimed is expected


def test_daily_scheduler_on_friday_processes_thursday(monkeypatch):
    calls = []

    async def fake_send(db, report_date):
        calls.append(report_date)
        return {"status": "accepted"}

    monkeypatch.setattr(daily, "send_production_daily_report", fake_send)
    run_at = datetime(2026, 8, 21, 9, 15, tzinfo=daily.CHILE)
    result = asyncio.run(daily.run_scheduled_production_daily_report(_db(), run_at=run_at))
    assert result["status"] == "accepted"
    assert calls == [date(2026, 8, 20)]


def test_daily_flow_delegates_all_sync_mongo_operations(monkeypatch):
    class CheckingCollection:
        def __init__(self):
            self.collection = mongomock.MongoClient().db[daily.DAILY_DELIVERY_COLLECTION]
            self.main_thread_calls = []

        def _call(self, operation, *args, **kwargs):
            self.main_thread_calls.append((operation, threading.current_thread() is threading.main_thread()))
            return getattr(self.collection, operation)(*args, **kwargs)

        def create_index(self, *args, **kwargs):
            return self._call("create_index", *args, **kwargs)

        def insert_one(self, *args, **kwargs):
            return self._call("insert_one", *args, **kwargs)

        def find_one(self, *args, **kwargs):
            return self._call("find_one", *args, **kwargs)

        def find_one_and_update(self, *args, **kwargs):
            return self._call("find_one_and_update", *args, **kwargs)

        def update_one(self, *args, **kwargs):
            return self._call("update_one", *args, **kwargs)

    class CheckingDB:
        def __init__(self):
            self.collection = CheckingCollection()

        def __getitem__(self, name):
            assert name == daily.DAILY_DELIVERY_COLLECTION
            return self.collection

    calculation_threads = []
    monkeypatch.setattr(
        daily,
        "calculate_daily_report",
        lambda db, period: (calculation_threads.append(threading.current_thread() is threading.main_thread()) or _fake_report()),
    )
    monkeypatch.setattr(daily, "build_whatsapp_message", lambda report: "mensaje")
    monkeypatch.setattr(daily, "validate_reconciliation", lambda report, message: {"ok": True})
    db = CheckingDB()
    monkeypatch.setattr(Config, "CAPTACION_DAILY_PRODUCTION_ENABLED", True)
    monkeypatch.setattr(Config, "CAPTACION_TEST_MODE", False)
    monkeypatch.setattr(Config, "CAPTACION_PRODUCTION_GROUP", "group@g.us")

    async def fake_send(recipient, message):
        return {"success": True, "delivery_status": "accepted", "provider_message_id": "p-thread", "http_status": 200}

    monkeypatch.setattr(daily, "send_whatsapp_message_detailed", fake_send)
    result = asyncio.run(daily.send_production_daily_report(db, date(2026, 8, 20)))
    assert result["status"] == "accepted"
    assert db.collection.main_thread_calls
    assert all(not on_main for _, on_main in db.collection.main_thread_calls)
    assert calculation_threads == [False]
