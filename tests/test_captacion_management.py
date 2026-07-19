from datetime import datetime, timezone

import pytest

import captacion_management as management


class _Result:
    def __init__(self, upserted_id=None):
        self.upserted_id = upserted_id


class _Collection:
    def __init__(self):
        self.rows = []

    def create_index(self, *args, **kwargs):
        return kwargs.get("name")

    def insert_one(self, row):
        self.rows.append(dict(row))
        return _Result(str(len(self.rows)))

    def find_one(self, query, projection=None):
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                if projection:
                    return {key: row.get(key) for key, enabled in projection.items() if enabled}
                return dict(row)
        return None

    def update_one(self, query, update, upsert=False):
        row = self.find_one(query)
        if row:
            original = next(item for item in self.rows if all(item.get(key) == value for key, value in query.items()))
            original.update(update.get("$set", {}))
            return _Result()
        if upsert:
            inserted = dict(query)
            inserted.update(update.get("$setOnInsert", {}))
            inserted.update(update.get("$set", {}))
            self.rows.append(inserted)
            return _Result(str(len(self.rows)))
        return _Result()


class _Db:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _Collection())


@pytest.fixture(autouse=True)
def reset_indexes():
    management._INDEXES_READY = False


def _property():
    return {"_id": "p1", "gestion": {"ejecutivo_id": "u1", "fecha_asignacion": "2026-07-20T09:00:00Z"}}


def _user(user_id="u1"):
    return {"_id": user_id, "nombre": "Ana", "email": "ana@example.test"}


def test_opening_external_app_creates_attempt_but_no_credit():
    db = _Db()
    attempt = management.start_management_attempt(
        db, property_doc=_property(), actor_user=_user(), action="message", channel="wa", now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    )
    assert attempt["status"] == "pending_confirmation"
    assert len(db[management.ATTEMPT_COLLECTION].rows) == 1
    assert db[management.LEDGER_COLLECTION].rows == []


def test_valid_confirmation_credits_once_per_property_user_and_day():
    db = _Db()
    now = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    first = management.start_management_attempt(db, property_doc=_property(), actor_user=_user(), action="call", channel="tel", now=now)
    result = management.confirm_management_attempt(db, attempt_id=first["attempt_id"], actor_user=_user(), result="no_answer", now=now)
    assert result["credited"] is True
    assert len(db[management.LEDGER_COLLECTION].rows) == 1

    second = management.start_management_attempt(db, property_doc=_property(), actor_user=_user(), action="message", channel="wa", now=now)
    duplicate = management.confirm_management_attempt(db, attempt_id=second["attempt_id"], actor_user=_user(), result="message_sent", now=now)
    assert duplicate["credited"] is False
    assert len(db[management.LEDGER_COLLECTION].rows) == 1


def test_cancel_never_creates_ledger_event():
    db = _Db()
    attempt = management.start_management_attempt(db, property_doc=_property(), actor_user=_user(), action="call", channel="tel")
    result = management.confirm_management_attempt(db, attempt_id=attempt["attempt_id"], actor_user=_user(), result="cancel")
    assert result == {"status": "cancelled", "credited": False, "contact_effective": False}
    assert db[management.LEDGER_COLLECTION].rows == []


def test_only_attempt_owner_can_confirm():
    db = _Db()
    attempt = management.start_management_attempt(db, property_doc=_property(), actor_user=_user(), action="call", channel="tel")
    with pytest.raises(PermissionError):
        management.confirm_management_attempt(db, attempt_id=attempt["attempt_id"], actor_user=_user("u2"), result="contacted")


def test_contact_effective_is_separate_from_managed_property():
    db = _Db()
    attempt = management.start_management_attempt(db, property_doc=_property(), actor_user=_user(), action="call", channel="tel")
    result = management.confirm_management_attempt(db, attempt_id=attempt["attempt_id"], actor_user=_user(), result="contacted")
    assert result["credited"] is True
    assert result["contact_effective"] is True
    assert db[management.LEDGER_COLLECTION].rows[0]["contact_effective"] is True
