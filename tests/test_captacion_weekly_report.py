import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

import captacion_management as management
import chatbot.captacion_weekly_report as weekly


def _panel():
    daily = [
        {"date": f"2026-07-{day}", "count": count}
        for day, count in zip(range(13, 18), (0, 0, 1, 6, 1))
    ]
    return {
        "mode": "team",
        "timezone": "America/Santiago",
        "week_count": 8,
        "contact_attempts": 3,
        "effective_contacts": 0,
        "captures": 0,
        "history_event_count": 17,
        "final_outcomes": {
            "por_contactar": 5,
            "en_gestion": 0,
            "no_respondio": 3,
            "ocupado": 0,
            "numero_invalido": 0,
            "contactado": 0,
            "solicita_llamada_posterior": 0,
            "mensaje_enviado": 0,
            "corredor": 0,
            "descartado": 0,
            "captado": 0,
            "otros": 0,
        },
        "executives": [
            {"name": "Susana", "week_count": 5, "contact_attempts": 1, "effective_contacts": 0, "captures": 0, "daily": daily},
            {"name": "Mariela", "week_count": 2, "contact_attempts": 2, "effective_contacts": 0, "captures": 0, "daily": daily},
            {"name": "Paula", "week_count": 1, "contact_attempts": 0, "effective_contacts": 0, "captures": 0, "daily": daily},
            {"name": "Erika", "week_count": 0, "contact_attempts": 0, "effective_contacts": 0, "captures": 0, "daily": daily},
        ],
    }


def _narrative():
    return {
        "intro": "Compartimos el avance registrado durante la semana.",
        "insight": "La gestión muestra oportunidades para fortalecer el seguimiento.",
        "weekly_focus": "Priorizar confirmaciones y registrar el resultado comercial.",
        "closing": "Gracias por mantener una gestión clara y trazable.",
    }


def test_snapshot_is_built_from_exact_panel_backend(monkeypatch):
    calls = []
    panel = _panel()
    monkeypatch.setattr(weekly, "get_captacion_goal_dashboard", lambda db, now=None: calls.append(now) or panel)
    snapshot = weekly.build_weekly_snapshot(object(), "2026-07-13", "2026-07-17", is_test=True)
    assert len(calls) == 1
    assert snapshot["team"]["properties_managed_unique"] == panel["week_count"] == 8
    assert snapshot["crm_parity"]["validated"] is True
    assert sum(snapshot["final_outcomes"].values()) == 8


def test_six_events_on_one_property_are_one_management_unit_and_one_final_outcome():
    start = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    events = []
    for index, result in enumerate(("ready_to_contact", "in_progress", "message_sent", "contacted", "message_sent", "no_answer")):
        events.append({
            "event_id": f"e{index}",
            "property_id": "p1",
            "actor_user_id": "u1",
            "local_date": "2026-07-13",
            "occurred_at": start + timedelta(minutes=index),
            "result": result,
            "credited": index == 0,
            "commercially_valid": True,
        })
    assert management.summarize_management_metrics([row for row in events if row["credited"]])["managed_properties"] == 1
    outcomes = management.summarize_final_outcomes(events)
    assert outcomes["no_respondio"] == 1
    assert sum(outcomes.values()) == 1
    assert len(events) == 6


def test_two_properties_are_two_management_units():
    events = [
        {"event_id": "e1", "property_id": "p1", "actor_user_id": "u1", "local_date": "2026-07-13", "occurred_at": datetime(2026, 7, 13, 12, tzinfo=timezone.utc), "result": "contacted", "credited": True},
        {"event_id": "e2", "property_id": "p2", "actor_user_id": "u1", "local_date": "2026-07-13", "occurred_at": datetime(2026, 7, 13, 13, tzinfo=timezone.utc), "result": "captured", "event_type": "capture_confirmed", "credited": True},
    ]
    metrics = management.summarize_management_metrics(events)
    assert metrics["managed_properties"] == 2
    assert metrics["effective_contacts"] == 0
    assert metrics["captures"] == 1
    assert sum(management.summarize_final_outcomes(events).values()) == 2


def test_history_events_are_admin_only_and_never_sent_to_deepseek_or_whatsapp(monkeypatch):
    monkeypatch.setattr(weekly, "get_captacion_goal_dashboard", lambda db, now=None: _panel())
    snapshot = weekly.build_weekly_snapshot(object(), "2026-07-13", "2026-07-17", is_test=True)
    payload = weekly.build_deepseek_payload(snapshot)
    message = weekly.assemble_whatsapp_message(snapshot, _narrative())
    assert "administrative" not in payload
    assert "history_event" not in str(payload)
    assert "Eventos registrados en el historial" not in message


def test_deepseek_payload_has_no_pii_and_narrative_cannot_add_numbers(monkeypatch):
    monkeypatch.setattr(weekly, "get_captacion_goal_dashboard", lambda db, now=None: _panel())
    snapshot = weekly.build_weekly_snapshot(object(), "2026-07-13", "2026-07-17", is_test=True)
    snapshot["phone"] = "+56911111111"
    snapshot["owner"] = "Persona privada"
    payload = weekly.build_deepseek_payload(snapshot)
    serialized = str(payload).casefold()
    assert "phone" not in serialized
    assert "owner" not in serialized
    invalid = _narrative()
    invalid["insight"] = "Se registraron 99 gestiones."
    with pytest.raises(ValueError, match="cifras"):
        weekly.validate_narrative(invalid)


def test_parity_failure_aborts():
    panel = _panel()
    snapshot = {
        "team": {
            "properties_managed_unique": 7,
            "properties_with_contact_attempt_unique": 3,
            "effective_contacts_unique": 0,
            "captured_properties_unique": 0,
        },
        "executives": [],
        "final_outcomes": {"otros": 7},
    }
    with pytest.raises(ValueError, match="Paridad CRM fallida"):
        weekly.validate_crm_parity(snapshot, panel)


class _Result:
    modified_count = 1


class _Collection:
    def __init__(self):
        self.rows = []

    def create_index(self, *args, **kwargs):
        return kwargs.get("name")

    def find_one(self, query, projection=None):
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                result = deepcopy(row)
                if projection and projection.get("_id") == 0:
                    result.pop("_id", None)
                return result
        return None

    def insert_one(self, row):
        self.rows.append(deepcopy(row))
        return _Result()

    def update_one(self, query, update):
        row = next(item for item in self.rows if all(item.get(key) == value for key, value in query.items()))
        row.update(deepcopy(update.get("$set", {})))
        return _Result()


class _Db:
    def __init__(self):
        self.data = {}

    def __getitem__(self, name):
        return self.data.setdefault(name, _Collection())


def test_test_delivery_only_targets_exact_admin_and_never_consumes_official_idempotency(monkeypatch):
    db = _Db()
    report = {
        "report_id": "r-test",
        "snapshot_id": "s-test",
        "is_test": True,
        "status": "ready_for_test",
        "crm_parity_validated": True,
        "message_original": "mensaje seguro",
        "prompt_version": "v2",
        "model": "deepseek-test",
    }
    db[weekly.REPORT_COLLECTION].rows.append(report)
    sent_to = []

    async def fake_send(recipient, text):
        sent_to.append(recipient)
        return {"success": True, "delivery_status": "accepted", "provider_message_id": "provider-1"}

    async def fake_receipt(message_id, timeout_seconds=30):
        return {"delivery_status": "delivered", "provider_message_id": message_id}

    monkeypatch.setattr(weekly, "get_db", lambda: db)
    monkeypatch.setattr(weekly, "send_whatsapp_message_detailed", fake_send)
    monkeypatch.setattr(weekly, "wait_for_whatsapp_delivery", fake_receipt)
    delivery = asyncio.run(weekly.send_test_report("r-test", "+56 9 8321 9804"))
    assert sent_to == ["+56983219804"]
    assert delivery["official_delivery"] is False
    assert delivery["test_recipient"] is True
    assert delivery["idempotency_key"].startswith("test:")
    assert all(not row["idempotency_key"].startswith("official:") for row in db[weekly.DELIVERY_COLLECTION].rows)
    stored = db[weekly.REPORT_COLLECTION].rows[0]
    assert stored["official_sent_at"] is None
    assert stored["status"] == "test_sent"


def test_wrong_test_recipient_aborts_before_provider_call(monkeypatch):
    called = []

    async def fake_send(*args):
        called.append(args)

    monkeypatch.setattr(weekly, "send_whatsapp_message_detailed", fake_send)
    with pytest.raises(PermissionError):
        asyncio.run(weekly.send_test_report("r-test", "+56911111111"))
    assert called == []


def test_scheduler_source_has_no_automatic_group_send():
    assert weekly.Config.CAPTACION_WEEKLY_PREVIEW_REQUIRED is True
    assert weekly.Config.CAPTACION_WEEKLY_AUTOMATIC_SEND is False
    source = open("chatbot/captacion_weekly_report.py", encoding="utf-8").read()
    scheduler = source.split("async def check_and_prepare_weekly_report", 1)[1]
    assert "approve_and_send_report(" not in scheduler
    assert '"pending_approval"' in scheduler
