from datetime import date, datetime

import pytz

from captacion_workforce import (
    applicable_target,
    compliance_status,
    get_active_captacion_team,
    membership_is_active,
)


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


def test_target_is_zero_outside_membership_period():
    membership = _membership()
    membership["start_date"] = "2026-07-20"
    result = applicable_target(_Db(), membership, date(2026, 7, 17))
    assert result["target"] == 0
    assert result["exempt"] is True
    assert result["source"] == "membership_period"


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


def test_active_scheduled_day_is_not_exempt_without_calendar_or_exception():
    day = date(2026, 7, 20)
    result = applicable_target(_Db(), _membership(), day)
    assert result["target"] == 10
    assert result["exempt"] is False
    assert result["source"] == "membership"
    assert compliance_status(
        count=0,
        target=result["target"],
        local_day=day,
        now=CHILE.localize(datetime(2026, 7, 20, 10, 0)),
        exempt=result["exempt"],
    ) == "EN_PROGRESO"


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


def _matches(doc, query):
    for key, cond in (query or {}).items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
            continue
        val = doc.get(key)
        if isinstance(cond, dict):
            for op, operand in cond.items():
                if op == "$in" and val not in operand:
                    return False
                elif op == "$lte" and (val is None or val > operand):
                    return False
                elif op == "$gte" and (val is None or val < operand):
                    return False
                elif op == "$exists" and (val is None) == bool(operand):
                    return False
        elif val != cond:
            return False
    return True


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, *args, **kwargs):
        return self

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)


class _ListCollection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def find(self, query=None, projection=None):
        return _Cursor([row for row in self.rows if _matches(row, query)])

    def find_one(self, query=None):
        for row in self.rows:
            if _matches(row, query):
                return row
        return None

    def create_index(self, *args, **kwargs):
        return None


class _TeamDb:
    def __init__(self, memberships=None, usuarios=None):
        self.collections = {
            "captacion_team_memberships": _ListCollection(memberships),
            "captacion_work_calendar": _ListCollection(),
            "captacion_work_exceptions": _ListCollection(),
            "captacion_workforce_audit": _ListCollection(),
            "usuarios": _ListCollection(usuarios),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _membership_row(user_id, enabled=True):
    return {
        "user_id": user_id,
        "enabled": enabled,
        "start_date": "2026-07-01",
        "end_date": None,
        "daily_target": 10,
        "workdays": [0, 1, 2, 3, 4],
        "timezone": "America/Santiago",
        "close_hour": 19,
    }


def test_team_auto_includes_active_agents_without_membership():
    usuarios = [
        {"_id": "u1", "nombre": "Ana", "rol": "agente", "is_active": True},
        {"_id": "u2", "nombre": "Rocio", "rol": "agente", "is_active": True},
        {"_id": "u3", "nombre": "Inactiva", "rol": "agente", "is_active": False},
        {"_id": "u4", "nombre": "Jefe", "rol": "admin", "is_active": True},
    ]
    db = _TeamDb(memberships=[_membership_row("u1")], usuarios=usuarios)
    team = get_active_captacion_team(db, date(2026, 7, 20))
    by_id = {member["id"]: member for member in team}
    assert set(by_id) == {"u1", "u2"}
    inferred = by_id["u2"]["membership"]
    assert inferred["auto_inferred"] is True
    assert inferred["daily_target"] == 10
    assert applicable_target(db, inferred, date(2026, 7, 20))["target"] == 10


def test_explicit_membership_blocks_auto_inclusion_even_when_disabled():
    db = _TeamDb(
        memberships=[_membership_row("u2", enabled=False)],
        usuarios=[{"_id": "u2", "nombre": "Rocio", "rol": "agente", "is_active": True}],
    )
    team = get_active_captacion_team(db, date(2026, 7, 20))
    assert team == []
