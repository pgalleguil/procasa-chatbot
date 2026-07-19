from datetime import date, datetime

import pytz

from captacion_workforce import applicable_target, compliance_status, membership_is_active


CHILE = pytz.timezone("America/Santiago")


class _Collection:
    def __init__(self, row=None):
        self.row = row

    def find_one(self, query):
        return self.row


class _Db:
    def __init__(self, calendar=None, exception=None):
        self.collections = {
            "captacion_work_calendar": _Collection(calendar),
            "captacion_work_exceptions": _Collection(exception),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _membership():
    return {
        "user_id": "u1",
        "enabled": True,
        "start_date": "2026-07-01",
        "end_date": None,
        "daily_target": 10,
        "workdays": [0, 1, 2, 3, 4],
        "timezone": "America/Santiago",
        "close_hour": 19,
    }


def test_membership_period_is_explicit_and_inclusive():
    membership = _membership()
    assert membership_is_active(membership, date(2026, 7, 1))
    assert membership_is_active(membership, date(2026, 12, 1))
    membership["end_date"] = "2026-07-31"
    assert membership_is_active(membership, date(2026, 7, 31))
    assert not membership_is_active(membership, date(2026, 8, 1))


def test_full_day_exception_is_exempt_and_auditable():
    exception = {
        "_id": "e1",
        "type": "vacaciones",
        "target_override": None,
        "reason": "Vacaciones aprobadas",
    }
    result = applicable_target(_Db(exception=exception), _membership(), date(2026, 7, 20))
    assert result["target"] == 0
    assert result["exempt"] is True
    assert result["source"] == "exception"
    assert result["reason"] == "Vacaciones aprobadas"


def test_half_day_has_half_target():
    exception = {"_id": "e2", "type": "media_jornada", "target_override": None, "reason": "AM"}
    result = applicable_target(_Db(exception=exception), _membership(), date(2026, 7, 20))
    assert result["target"] == 5
    assert result["exempt"] is False


def test_holiday_uses_local_stored_calendar():
    holiday = {"is_working_day": False, "target_override": 0, "label": "Feriado nacional"}
    result = applicable_target(_Db(calendar=holiday), _membership(), date(2026, 7, 20))
    assert result["target"] == 0
    assert result["reason"] == "Feriado nacional"
    assert result["source"] == "calendar"


def test_day_is_not_failed_before_close_hour():
    before_close = CHILE.localize(datetime(2026, 7, 20, 18, 59))
    after_close = CHILE.localize(datetime(2026, 7, 20, 19, 0))
    assert compliance_status(count=7, target=10, local_day=date(2026, 7, 20), now=before_close) == "EN_PROGRESO"
    assert compliance_status(count=7, target=10, local_day=date(2026, 7, 20), now=after_close) == "INCUMPLIDO"
    assert compliance_status(count=10, target=10, local_day=date(2026, 7, 20), now=before_close) == "CUMPLIDO"
    assert compliance_status(count=0, target=0, local_day=date(2026, 7, 20), now=before_close, exempt=True) == "EXENTO"
