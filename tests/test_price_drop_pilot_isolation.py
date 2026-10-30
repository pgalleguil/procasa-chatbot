"""End-to-end isolated handler tests: no CRM/SLA collections are available."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pymongo.errors import DuplicateKeyError

from chatbot.price_drop_pilot_isolation import (
    build_test_reply, is_test_phone, load_test_context, process_test_inbound,
)

PHONE = "+56977770000"


class MemoryCursor:
    def __init__(self, rows):
        self.rows = list(rows)
    def sort(self, name, direction):
        self.rows.sort(key=lambda row: row.get(name, datetime.min.replace(tzinfo=timezone.utc)), reverse=direction == -1)
        return self
    def limit(self, count):
        self.rows = self.rows[:count]
        return self
    def __iter__(self):
        return iter(self.rows)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.calls = []
    def find_one(self, query, projection=None, sort=None):
        self.calls.append(("find_one", query))
        if sort and self.docs:
            return sorted(self.docs, key=lambda x: x.get("sent_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[0]
        return self.docs[0] if self.docs else None
    def find(self, query, projection=None):
        self.calls.append(("find", query))
        return MemoryCursor([x for x in self.docs if
                             x.get("campaign_id") == query.get("campaign_id")
                             and x.get("status") == query.get("status")
                             and x.get("_id") != query.get("_id", {}).get("$ne")])
    def insert_one(self, doc):
        self.calls.append(("insert_one", doc["_id"]))
        if any(x["_id"] == doc["_id"] for x in self.docs):
            raise DuplicateKeyError("duplicate test provider id")
        self.docs.append(dict(doc))
    def update_one(self, selector, update):
        self.calls.append(("update_one", selector))
        for doc in self.docs:
            if doc.get("_id") == selector.get("_id"):
                doc.update(update.get("$set", {}))
                return


class BlockedCollection:
    def __getattr__(self, name):
        raise AssertionError("Live CRM collection was touched: " + name)


class FakeDB(dict):
    def __missing__(self, key):
        if key in {"leads", "crm_assignment_cycles", "crm_management_results",
                   "event_log", "chatbot_message_queue", "crm_sla_alerts"}:
            raise AssertionError("Unexpected CRM/SLA collection: " + key)
        raise AssertionError("Unexpected collection: " + key)


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "1")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", PHONE)


def db_case(*, duplicate_live_identity=False, active=True, price=2690):
    now = datetime.now(timezone.utc)
    campaign = {
        "_id": "test:6436",
        "phone_normalized": PHONE, "test_mode": True,
        "provider_message_id": "provider-original",
        "property_code": "6436",
        "operation": "Venta",
        "old_price_uf": 2790.0, "new_price_uf": 2690.0,
        "status": "SENT", "sent_at": now - timedelta(minutes=5),
        "expires_at": now + timedelta(days=1),
    }
    property_doc = {
        "codigo": "6436",
        "disponible_prop360": active,
        "estado": {"oficina": "PROCASA SUCRE"},
        "tipo_operacion": {"venta": True, "precio_venta": {"precio_uf": price}},
    }
    db = FakeDB({
        "crm_price_drop_pilot_v1": FakeCollection([campaign]),
        "universo_cartera_prop360": FakeCollection([property_doc]),
        "crm_price_drop_pilot_test_turns_v1": FakeCollection(),
    })
    # Simulate two previously existing records. They are never read.
    db["leads"] = BlockedCollection()
    return db


def sender_spy():
    calls = []
    def send(phone, answer):
        calls.append((phone, answer))
        return {"success": True, "provider_message_id": "reply-ack-" + str(len(calls))}
    return send, calls


def test_isolated_yes_no_executive_notifications_or_crm_changes(isolated):
    db = db_case()
    sender, calls = sender_spy()
    result = process_test_inbound(db, PHONE, "incoming-yes-1", "Sí", sender=sender)
    assert result == {"status": "test_reply_sent"}
    assert len(calls) == 1
    assert "6436" in calls[0][1]
    assert db["crm_price_drop_pilot_test_turns_v1"].docs[0]["status"] == "SENT"
    assert set(db.keys()) == {
        "leads", "crm_price_drop_pilot_v1", "universo_cartera_prop360",
        "crm_price_drop_pilot_test_turns_v1"
    }


@pytest.mark.parametrize("message,fragment", [
    ("Sí, me interesa", "6436"),
    ("Quiero visitarla", "¿Qué día"),
    ("¿Cuánto cuesta ahora?", "2.690"),
    ("¿Aceptan ofertas?", "oferta"),
    ("No me interesa", "No insistiremos"),
    ("¿Tienen otra en Ñuñoa?", "otras"),
    ("6500", "6436"),
])
def test_intents_without_commercial_actions(isolated, message, fragment):
    db = db_case()
    sender, calls = sender_spy()
    result = process_test_inbound(db, PHONE, "in-" + message, message, sender=sender)
    assert result["status"] == "test_reply_sent"
    assert fragment.casefold() in calls[0][1].casefold()


def test_freeform_reply_runs_isolated_model_not_crm(isolated):
    db = db_case()
    sender, calls = sender_spy()
    seen = []
    def llm(message, ctx, history):
        seen.append((message, ctx, history))
        return "La ficha de esta prueba no indica si incluye bodega."
    result = process_test_inbound(
        db, PHONE, "question-1", "¿Tiene bodega?", sender=sender, llm=llm
    )
    assert result["status"] == "test_reply_sent"
    assert seen[0][1]["property_code"] == "6436"
    assert calls[0][1].startswith("La ficha")


def test_duplicate_provider_event_never_resends(isolated):
    db = db_case()
    sender, calls = sender_spy()
    process_test_inbound(db, PHONE, "same-id", "Sí", sender=sender)
    assert process_test_inbound(db, PHONE, "same-id", "Sí", sender=sender) == {
        "status": "duplicate_suppressed",
    }
    assert len(calls) == 1


def test_stop_never_sends_or_mutates_crm(isolated):
    db = db_case()
    sender, calls = sender_spy()
    outcome = process_test_inbound(db, PHONE, "stop-1", "No me escriban más", sender=sender)
    assert outcome == {"status": "test_stopped"}
    assert not calls
    assert db["crm_price_drop_pilot_v1"].docs[0]["status"] == "STOPPED"
    assert process_test_inbound(db, PHONE, "stop-2", "Sí", sender=sender) == {
        "status": "test_not_active",
    }


def test_inactive_campaign_never_falls_through_or_sends(isolated):
    db = db_case()
    db["crm_price_drop_pilot_v1"].docs[0]["status"] = "SENDING"
    sender, calls = sender_spy()
    assert process_test_inbound(db, PHONE, "no-campaign", "sí", sender=sender) == {
        "status": "test_not_active",
    }
    assert not calls


def test_unverified_price_never_sends(isolated):
    db = db_case(price=2800)
    sender, calls = sender_spy()
    assert process_test_inbound(db, PHONE, "bad-price", "Sí", sender=sender)["status"] == "test_not_active"
    assert not calls


def test_outside_phone_never_reads_or_sends(isolated):
    db = db_case()
    sender, calls = sender_spy()
    assert process_test_inbound(db, "+56977770001", "someone", "Sí", sender=sender)["status"] == "not_test_phone"
    assert not calls


def test_provider_failure_has_no_retry(isolated):
    db = db_case()
    calls = []
    def bad_sender(*args):
        calls.append(1)
        raise TimeoutError()
    assert process_test_inbound(db, PHONE, "failure-1", "sí", sender=bad_sender)["status"] == "delivery_unknown"
    assert process_test_inbound(db, PHONE, "failure-1", "sí", sender=bad_sender)["status"] == "duplicate_suppressed"
    assert calls == [1]


def test_model_cannot_claim_executive_was_notified(isolated):
    ctx = {"property_code": "6436", "old_price_uf": 2790, "new_price_uf": 2690}
    answer = build_test_reply("¿Qué gastos comunes tiene?", ctx,
                              llm=lambda *_: "He notificado al ejecutivo y te llamará")
    assert "he notificado" not in answer.lower()
    assert "no notificaremos" in answer.lower()


def test_webhook_interception_occurs_before_production_operations():
    source = Path(__file__).resolve().parents[1].joinpath("webhook.py").read_text()
    marker = source.index("    # Isolated price-drop test: intercept this exact phone BEFORE log_event")
    assert marker < source.index("    # Para el log de eventos, usamos el número limpio sin el '+'", marker)
    assert marker < source.index("    # Persist to the durable queue.", marker)
    assert 'if is_test_phone(phone):' in source[marker:marker + 1600]


def test_script_no_longer_looks_up_or_edits_existing_leads():
    source = Path(__file__).resolve().parents[1].joinpath("scripts/price_drop_pilot_test.py").read_text()
    assert 'db["leads"]' not in source
    assert '"test_mode": True' in source
    assert "if not args.send:" in source
