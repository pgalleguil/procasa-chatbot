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
        "detailed_outcomes": {"por_contactar": 5, "no_respondio": 3},
        "detail_labels": {"por_contactar": "Por contactar", "no_respondio": "No respondió"},
        "requires_outcome_review": False,
        "outcome_groups": {
            "pending_next_action": {"label": "Pendientes de nueva gestión", "total": 8, "details": {"por_contactar": 5, "no_respondio": 3}},
            "management_in_progress": {"label": "Gestión en curso", "total": 0, "details": {}},
            "closed_without_capture": {"label": "Cerradas sin captación", "total": 0, "details": {}},
            "captured": {"label": "Captadas", "total": 0, "details": {}},
            "other_review": {"label": "Otros / Por revisar", "total": 0, "details": {}},
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
    assert sum(group["total"] for group in snapshot["outcome_groups"].values()) == 8


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
    assert outcomes["contactado"] == 1
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
        "outcome_groups": {"other_review": {"total": 7, "details": {"unknown": 7}}},
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


def test_manual_official_send_is_disabled(monkeypatch):
    db = _Db()
    db[weekly.REPORT_COLLECTION].rows.append({
        "report_id": "r-official", "is_test": False, "status": "pending_approval",
        "crm_parity_validated": True, "snapshot": {"requires_outcome_review": True},
    })
    called = []

    async def fake_send(*args):
        called.append(args)

    monkeypatch.setattr(weekly, "get_db", lambda: db)
    monkeypatch.setattr(weekly, "send_whatsapp_message_detailed", fake_send)
    with pytest.raises(ValueError, match="envío manual oficial está deshabilitado"):
        asyncio.run(weekly.approve_and_send_report("r-official", {"username": "admin"}))
    assert called == []


def test_scheduler_is_permanently_automatic_at_0830():
    assert weekly.Config.CAPTACION_WEEKLY_PREVIEW_REQUIRED is False
    assert weekly.Config.CAPTACION_WEEKLY_AUTOMATIC_SEND is False
    assert weekly.Config.CAPTACION_WEEKLY_SCHEDULE_HOUR == 8
    assert weekly.Config.CAPTACION_WEEKLY_SCHEDULE_MINUTE == 30


@pytest.mark.parametrize(("result", "expected_group", "expected_detail"), [
    ("ready_to_contact", "pending_next_action", "por_contactar"),
    ("no_answer", "pending_next_action", "no_respondio"),
    ("contacted", "management_in_progress", "contactado"),
    ("broker_identified", "closed_without_capture", "corredor"),
    ("discarded", "closed_without_capture", "descartado"),
    ("unavailable", "closed_without_capture", "propiedad_no_disponible"),
    ("captured", "captured", "captado"),
])
def test_real_results_use_shared_grouping(result, expected_group, expected_detail):
    events = [{
        "event_id": "e1", "property_id": "p1", "actor_user_id": "u1",
        "local_date": "2026-07-13", "occurred_at": datetime(2026, 7, 13, 12, tzinfo=timezone.utc),
        "result": result, "credited": True, "commercially_valid": True,
    }]
    summary = management.summarize_grouped_outcomes(events)
    assert summary["outcome_groups"][expected_group]["total"] == 1
    assert summary["detailed_outcomes"][expected_detail] == 1
    assert sum(group["total"] for group in summary["outcome_groups"].values()) == 1


def test_unknown_valid_observation_is_preserved_for_review():
    events = [{
        "event_id": "e1", "property_id": "p1", "actor_user_id": "u1", "local_date": "2026-07-13",
        "occurred_at": datetime(2026, 7, 13, 12, tzinfo=timezone.utc), "result": "new_crm_result",
        "credited": True, "commercially_valid": True,
    }]
    summary = management.summarize_grouped_outcomes(events)
    assert summary["requires_outcome_review"] is True
    assert summary["outcome_groups"]["other_review"]["details"]["new_crm_result"] == 1


def test_whatsapp_omits_zero_groups_and_uses_compact_executive_lines(monkeypatch):
    monkeypatch.setattr(weekly, "get_captacion_goal_dashboard", lambda db, now=None: _panel())
    snapshot = weekly.build_weekly_snapshot(object(), "2026-07-13", "2026-07-17", is_test=True)
    message = weekly.assemble_whatsapp_message(snapshot, _narrative())
    assert "📋 *Estado al cierre*" in message
    assert "• *5* por contactar" in message
    assert "• *3* sin respuesta" in message
    assert "Pendientes de nueva gestión: *8*" not in message
    assert "Gestión en curso: *0*" not in message
    assert "contactos efectivos ·" not in message
    assert "Compartimos el resumen" not in message
    assert "sin gestiones registradas en el período" in message
    assert _narrative()["closing"] in message


def test_official_format_has_no_test_reference(monkeypatch):
    panel = _panel()
    monkeypatch.setattr(weekly, "get_captacion_goal_dashboard", lambda db, now=None: panel)
    snapshot = weekly.build_weekly_snapshot(object(), "2026-07-13", "2026-07-17", is_test=False)
    message = weekly.assemble_whatsapp_message(snapshot, _narrative())
    assert "CAPTACIONES | INICIO DE SEMANA" in message
    assert "PRUEBA" not in message
    assert "Prueba enviada" not in message


def test_transition_note_is_omitted_when_history_is_complete(monkeypatch):
    monkeypatch.setattr(weekly, "get_captacion_goal_dashboard", lambda db, now=None: _panel())
    snapshot = weekly.build_weekly_snapshot(object(), "2026-07-13", "2026-07-17", is_test=False)
    snapshot["data_quality"]["historical_measurement_complete"] = True
    message = weekly.assemble_whatsapp_message(snapshot, _narrative())
    assert "inicio de la nueva medición" not in message


def test_scheduler_uses_previous_monday_to_friday_and_sends_at_0830(monkeypatch):
    db = _Db()
    calls = []
    report = {
        "report_id": "r-auto", "period_start": "2026-07-13", "period_end": "2026-07-17",
        "is_test": False, "status": "ready_to_send", "snapshot": {"requires_outcome_review": False},
    }

    async def fake_create(start, end, **kwargs):
        calls.append((start.isoformat(), end.isoformat(), kwargs))
        db[weekly.REPORT_COLLECTION].rows.append(deepcopy(report))
        return deepcopy(report)

    async def fake_send(report_id, now=None):
        calls.append(("send", report_id, now.strftime("%H:%M")))
        return {"delivery_status": "delivered"}

    monkeypatch.setattr(weekly, "get_db", lambda: db)
    monkeypatch.setattr(weekly, "create_weekly_report", fake_create)
    monkeypatch.setattr(weekly, "send_official_report", fake_send)
    monday = weekly.CHILE.localize(datetime(2026, 7, 20, 8, 30))
    asyncio.run(weekly.check_and_prepare_weekly_report(now=monday))
    assert calls[0][:2] == ("2026-07-13", "2026-07-17")
    assert calls[1] == ("send", "r-auto", "08:30")


def test_scheduler_does_not_run_before_window(monkeypatch):
    monkeypatch.setattr(weekly, "get_db", lambda: _Db())
    monday = weekly.CHILE.localize(datetime(2026, 7, 20, 8, 29))
    assert asyncio.run(weekly.check_and_prepare_weekly_report(now=monday)) is None


def test_deepseek_failure_uses_safe_fallback(monkeypatch):
    monkeypatch.setattr(weekly, "generate_narrative", lambda snapshot: (_ for _ in ()).throw(ValueError("down")))
    narrative, model, source = weekly.generate_narrative_with_fallback({})
    assert source == "fallback"
    assert model == "deterministic_fallback"
    assert narrative["closing"] == "¡Buen inicio de semana! 💪"


def test_official_validation_blocks_other_review(monkeypatch):
    panel = _panel()
    panel["week_count"] = 8
    panel["outcome_groups"]["pending_next_action"]["total"] = 7
    panel["outcome_groups"]["pending_next_action"]["details"]["por_contactar"] = 4
    panel["outcome_groups"]["other_review"] = {"label": "Otros / Por revisar", "total": 1, "details": {"new_value": 1}}
    panel["requires_outcome_review"] = True
    monkeypatch.setattr(weekly, "get_captacion_goal_dashboard", lambda db, now=None: panel)
    snapshot = weekly.build_weekly_snapshot(object(), "2026-07-13", "2026-07-17", is_test=False)
    with pytest.raises(ValueError, match="no_other_review"):
        weekly.validate_official_report(snapshot, weekly.assemble_whatsapp_message(snapshot, _narrative()))


def test_official_send_remains_blocked_until_weekly_approval(monkeypatch):
    db = _Db()
    monkeypatch.setattr(weekly, "get_captacion_goal_dashboard", lambda db, now=None: _panel())
    snapshot = weekly.build_weekly_snapshot(object(), "2026-07-13", "2026-07-17", is_test=False)
    report = {
        "report_id": "r-group", "report_type": "captacion_weekly_official",
        "message_domain": weekly.MESSAGE_DOMAIN, "message_type": "weekly_report", "recipient_role": "captacion_team",
        "period_start": "2026-07-13", "period_end": "2026-07-17", "is_test": False,
        "status": "ready_to_send", "snapshot": snapshot, "snapshot_id": snapshot["snapshot_id"],
        "snapshot_hash": "hash", "crm_parity_validated": True,
        "message_original": weekly.assemble_whatsapp_message(snapshot, _narrative()),
        "group_recipient": "56990152481-1598919271@g.us", "narrative": _narrative(),
    }
    db[weekly.REPORT_COLLECTION].rows.append(report)
    recipients = []

    async def fake_send(recipient, text):
        recipients.append(recipient)
        return {"success": True, "delivery_status": "accepted", "provider_message_id": "provider-official"}

    async def fake_receipt(message_id, timeout_seconds=30):
        return {"delivery_status": "delivered", "provider_message_id": message_id}

    monkeypatch.setattr(weekly, "get_db", lambda: db)
    monkeypatch.setattr(weekly, "send_whatsapp_message_detailed", fake_send)
    monkeypatch.setattr(weekly, "wait_for_whatsapp_delivery", fake_receipt)
    now = weekly.CHILE.localize(datetime(2026, 7, 20, 8, 30))
    with pytest.raises(PermissionError, match="grupo bloqueado"):
        asyncio.run(weekly.send_official_report("r-group", now=now))
    assert recipients == []
