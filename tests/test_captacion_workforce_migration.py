from datetime import datetime, timezone

from scripts import migrate_captacion_workforce
from scripts.reconcile_captacion_membership_dates import build_plan, local_date


class _CaptacionCollection:
    def __init__(self, assigned_at):
        self.assigned_at = assigned_at

    def find_one(self, query, projection, sort=None):
        return {"gestion": {"fecha_asignacion": self.assigned_at}}


def test_membership_start_uses_first_assignment_in_chile(monkeypatch):
    collection = _CaptacionCollection(datetime(2026, 7, 14, 2, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(
        migrate_captacion_workforce.Config,
        "get_captacion_collection",
        lambda db: collection,
    )
    inferred = migrate_captacion_workforce.infer_start_date(
        object(), {"_id": "u1", "nombre": "Ana"}, "2026-07-19"
    )
    assert inferred == "2026-07-13"


def test_membership_start_covers_the_complete_assignment_week(monkeypatch):
    collection = _CaptacionCollection(datetime(2026, 7, 14, 17, 30, tzinfo=timezone.utc))
    monkeypatch.setattr(
        migrate_captacion_workforce.Config,
        "get_captacion_collection",
        lambda db: collection,
    )
    inferred = migrate_captacion_workforce.infer_start_date(
        object(), {"_id": "u1", "nombre": "Ana"}, "2026-07-19"
    )
    assert inferred == "2026-07-13"


def test_reconciliation_local_date_uses_chile_timezone():
    assert local_date(datetime(2026, 7, 14, 2, 30)) == "2026-07-13"


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query):
        return self

    def sort(self, key, direction):
        return self.rows

    def find_one(self, query, projection=None, sort=None):
        return {"gestion": {"fecha_asignacion": datetime(2026, 7, 14, 17, 30, tzinfo=timezone.utc)}}


class _ReconciliationDb:
    def __init__(self):
        self.memberships = _Rows([{
            "user_id": "u1",
            "enabled": True,
            "start_date": "2026-07-19",
        }])
        self.users = _Rows([{"_id": "u1", "nombre": "Paula"}])

    def __getitem__(self, name):
        return self.memberships if name == "captacion_team_memberships" else self.users


def test_reconciliation_does_not_exempt_days_before_first_assignment(monkeypatch):
    db = _ReconciliationDb()
    monkeypatch.setattr(
        migrate_captacion_workforce.Config,
        "get_captacion_collection",
        lambda current_db: _Rows([]),
    )
    import scripts.reconcile_captacion_membership_dates as reconciliation
    monkeypatch.setattr(reconciliation.Config, "get_captacion_collection", lambda current_db: _Rows([]))
    plan = build_plan(db)
    assert plan[0]["inferred_start_date"] == "2026-07-13"
    assert plan[0]["evidence"] == "semana_laboral_de_gestion.fecha_asignacion"
