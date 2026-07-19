from datetime import datetime, timezone

from scripts import migrate_captacion_workforce
from scripts.reconcile_captacion_membership_dates import local_date


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


def test_reconciliation_local_date_uses_chile_timezone():
    assert local_date(datetime(2026, 7, 14, 2, 30)) == "2026-07-13"
