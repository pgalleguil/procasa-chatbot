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
def reset_indexes(monkeypatch):
    management._INDEXES_READY = False
    monkeypatch.setattr(management, "_record_first_action_for_cycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(management, "recalculate_daily_metric", lambda *args, **kwargs: {})
    monkeypatch.setattr(management, "audit_management_patterns", lambda *args, **kwargs: [])


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
    assert len(db[management.LEDGER_COLLECTION].rows) == 2
    assert sum(bool(row.get("credited")) for row in db[management.LEDGER_COLLECTION].rows) == 1
    assert db[management.LEDGER_COLLECTION].rows[1]["credit_duplicate"] is True


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


@pytest.mark.parametrize("status,result", [("Corredor", "broker_identified"), ("Descartado", "discarded")])
def test_manual_commercial_conclusion_with_reason_credits(status, result):
    db = _Db()
    saved = management.record_manual_management_decision(
        db,
        property_doc=_property(),
        actor_user=_user(),
        status=status,
        previous_status="Por contactar",
        notes="Motivo comercial confirmado",
        now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
    )
    assert saved["credited"] is True
    assert db[management.LEDGER_COLLECTION].rows[0]["result"] == result


def test_in_progress_transition_credits_without_requiring_a_comment():
    decision = management.evaluate_manual_decision(
        status="En gestion", previous_status="Por contactar", notes=""
    )
    assert decision["eligible"] is True
    assert decision["result"] == "in_progress"
    assert not management.evaluate_manual_decision(
        status="En gestion", previous_status="En gestion", notes=""
    )["eligible"]


def test_capture_is_a_managed_property():
    db = _Db()
    captured = management.record_manual_management_decision(
        db,
        property_doc=_property(),
        actor_user=_user(),
        status="Captado",
        previous_status="En gestion",
        now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
    )
    assert captured["credited"] is True
    assert captured["capture"] is True
    assert db[management.LEDGER_COLLECTION].rows[0]["event_type"] == "capture_confirmed"


@pytest.mark.parametrize("status", ["Corredor", "Descartado"])
def test_conclusions_requiring_evidence_reject_an_empty_reason(status):
    with pytest.raises(ValueError, match="motivo"):
        management.evaluate_manual_decision(status=status, previous_status="Por contactar", notes="")


def test_non_commercial_and_automatic_changes_never_credit():
    assert not management.evaluate_manual_decision(status="assignment_changed", previous_status="Por contactar")["eligible"]
    assert not management.evaluate_manual_decision(status="reassignment_changed", previous_status="Por contactar")["eligible"]
    assert not management.evaluate_manual_decision(
        status="Captado", previous_status="Por contactar", is_automatic=True
    )["eligible"]


def test_manual_ready_to_contact_transition_credits_without_contact_metrics():
    db = _Db()
    prop = _property()
    prop["gestion"]["assignment_cycle_id"] = "cycle-1"
    saved = management.record_manual_management_decision(
        db,
        property_doc=prop,
        actor_user=_user(),
        status="Por contactar",
        previous_status="NUEVO",
        now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
    )
    assert saved["credited"] is True
    event = db[management.LEDGER_COLLECTION].rows[0]
    assert event["result"] == "ready_to_contact"
    assert event["contact_attempt"] is False
    assert event["contact_effective"] is False
    assert event["event_type"] == "manual_decision_confirmed"
    assert management.summarize_management_metrics([event]) == {
        "managed_properties": 1,
        "contact_attempts": 0,
        "effective_contacts": 0,
        "captures": 0,
    }


def test_automatic_or_unchanged_ready_to_contact_never_credits():
    assert not management.evaluate_manual_decision(
        status="Por contactar", previous_status="NUEVO", is_automatic=True
    )["eligible"]
    unchanged = management.evaluate_manual_decision(
        status="Por contactar", previous_status="Por contactar"
    )
    assert unchanged == {
        "eligible": False,
        "reason": "real_transition_required",
        "status": "por contactar",
    }


def test_ready_to_contact_credits_only_once_per_assignment_cycle():
    db = _Db()
    prop = _property()
    prop["gestion"]["assignment_cycle_id"] = "cycle-1"
    first = management.record_manual_management_decision(
        db, property_doc=prop, actor_user=_user(), status="Por contactar", previous_status="NUEVO",
        now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
    )
    repeated = management.record_manual_management_decision(
        db, property_doc=prop, actor_user=_user(), status="Por contactar", previous_status="Descartado",
        now=datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
    )
    assert first["credited"] is True
    assert repeated == {
        "status": "not_credited",
        "credited": False,
        "reason": "assignment_cycle_decision_already_recorded",
    }
    assert len(db[management.LEDGER_COLLECTION].rows) == 2
    assert sum(bool(row.get("credited")) for row in db[management.LEDGER_COLLECTION].rows) == 1


def test_new_assignment_cycle_allows_a_new_ready_to_contact_decision():
    db = _Db()
    first_prop = _property()
    first_prop["gestion"]["assignment_cycle_id"] = "cycle-1"
    second_prop = _property()
    second_prop["gestion"]["assignment_cycle_id"] = "cycle-2"
    first = management.record_manual_management_decision(
        db, property_doc=first_prop, actor_user=_user(), status="Por contactar", previous_status="NUEVO",
        now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
    )
    second = management.record_manual_management_decision(
        db, property_doc=second_prop, actor_user=_user(), status="Por contactar", previous_status="NUEVO",
        now=datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
    )
    assert first["credited"] is True
    assert second["credited"] is True
    assert {row["assignment_cycle_id"] for row in db[management.LEDGER_COLLECTION].rows} == {"cycle-1", "cycle-2"}


def test_second_action_same_day_does_not_duplicate_ready_to_contact_credit():
    db = _Db()
    prop = _property()
    prop["gestion"]["assignment_cycle_id"] = "cycle-1"
    now = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    first = management.record_manual_management_decision(
        db, property_doc=prop, actor_user=_user(), status="Por contactar", previous_status="NUEVO", now=now,
    )
    second = management.record_manual_management_decision(
        db, property_doc=prop, actor_user=_user(), status="Contacto exitoso",
        previous_status="Por contactar", now=now,
    )
    assert first["credited"] is True
    assert second["credited"] is False
    assert len(db[management.LEDGER_COLLECTION].rows) == 2
    assert sum(bool(row.get("credited")) for row in db[management.LEDGER_COLLECTION].rows) == 1
    assert management.summarize_management_metrics(db[management.LEDGER_COLLECTION].rows) == {
        "managed_properties": 1,
        "contact_attempts": 1,
        "effective_contacts": 1,
        "captures": 0,
    }


def test_manual_decisions_keep_one_credit_per_property_user_and_day():
    db = _Db()
    now = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    first = management.record_manual_management_decision(
        db, property_doc=_property(), actor_user=_user(), status="Contacto exitoso",
        previous_status="Por contactar", now=now,
    )
    second = management.record_manual_management_decision(
        db, property_doc=_property(), actor_user=_user(), status="Descartado",
        previous_status="Contacto exitoso", notes="Propietario no continuarÃ¡", now=now,
    )
    assert first["credited"] is True
    assert second["credited"] is False
    assert len(db[management.LEDGER_COLLECTION].rows) == 2
    assert sum(bool(row.get("credited")) for row in db[management.LEDGER_COLLECTION].rows) == 1


def test_reversal_appends_event_without_editing_original():
    db = _Db()
    original = {
        "event_id": "ev1",
        "event_type": "management_confirmed",
        "credited": True,
        "property_id": "p1",
        "actor_user_id": "u1",
        "local_date": "2026-07-20",
        "result": "contacted",
    }
    db[management.LEDGER_COLLECTION].rows.append(dict(original))
    reversal = management.reverse_management_event(
        db, event_id="ev1", actor_user={"_id": "admin1", "nombre": "Admin"}, reason="Confirmación errónea"
    )
    assert reversal["original_event_id"] == "ev1"
    assert reversal["previous_value"]["credited"] is True
    assert reversal["resulting_effect"]["credited"] is False
    assert db[management.LEDGER_COLLECTION].rows[0] == original
    assert len(db[management.LEDGER_COLLECTION].rows) == 2


def test_assignment_cycle_uses_existing_id_or_deterministic_legacy_fallback():
    current = _property()
    current["gestion"]["assignment_cycle_id"] = "cycle-current"
    assert management.assignment_cycle_id(current) == "cycle-current"
    legacy_one = management.assignment_cycle_id(_property())
    legacy_two = management.assignment_cycle_id(_property())
    assert legacy_one == legacy_two
    assert legacy_one.startswith("legacy-")
    assert management.new_assignment_cycle(property_id="p1", user_id="u1")["assignment_cycle_id"] != management.new_assignment_cycle(property_id="p1", user_id="u1")["assignment_cycle_id"]
