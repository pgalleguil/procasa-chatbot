"""HOT WhatsApp auto greeting — session, consent, cutover, SLA and concurrency."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
import pytest

from chatbot import crm_hot_linked_auto as hot
from chatbot.crm_metrics import calculate_cycle_sla
from chatbot.crm_sla_reassignment_worker import canonical_expiration_recheck
from chatbot.crm_sla_reassignment_transaction import (
    TransactionContractError, build_transaction_plan, build_source_cycle_filter,
)

ID = ObjectId("690e13577fb5678251136b01")
OWNER = str(ObjectId("6a681413140190dde11f26d1"))
CYCLE = "hot-prospective-cycle"
NOW = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)
ASSIGNED = NOW - timedelta(minutes=5)


def inbound():
    return {
        "role": "user", "actor_type": "customer",
        "provider_message_id": "inbound-office-001",
        "channel": "office_whatsapp", "source": "wasender_inbound",
        "timestamp": (NOW - timedelta(minutes=10)).isoformat(),
        "content": "Me interesa coordinar visita",
    }


class Leads:
    def __init__(self):
        self.doc = {
            "_id": ID, "phone": "+56996762064", "conversation_id": "conv-1",
            "prospecto": {"nombre": "Cliente"},
            "lead_temperature_effective": "HOT",
            "lifecycle": {
                "assigned_to_user_id": OWNER,
                "current_assignment_cycle_id": CYCLE,
            },
            "messages": [inbound()],
        }

    def find_one(self, query, projection=None):
        return deepcopy(self.doc) if query.get("_id") == ID else None

    def update_one(self, query, mutation):
        if query.get("_id") != ID:
            return SimpleNamespace(modified_count=0)
        lc = self.doc["lifecycle"]
        if (lc.get("current_assignment_cycle_id") != query.get("lifecycle.current_assignment_cycle_id")
                or lc.get("assigned_to_user_id") != query.get("lifecycle.assigned_to_user_id")):
            return SimpleNamespace(modified_count=0)
        elem_match = (query.get("messages") or {}).get("$elemMatch")
        if elem_match:
            exact = [m for m in self.doc["messages"]
                     if all(m.get(k) == v for k, v in elem_match.items())]
            if not exact:
                return SimpleNamespace(modified_count=0)
            for k, v in mutation["$set"].items():
                if k.startswith("messages.$."):
                    exact[0][k[len("messages.$."):]] = v
            return SimpleNamespace(modified_count=1)
        prohibited = query.get("$nor") or []
        if prohibited:
            dup = prohibited[0].get("messages", {}).get("$elemMatch", {})
            if any(all(m.get(k) == v for k, v in dup.items()) for m in self.doc["messages"]):
                return SimpleNamespace(modified_count=0)
        if "$push" in mutation:
            self.doc["messages"].append(deepcopy(mutation["$push"]["messages"]))
        if "$set" in mutation:
            for k,v in mutation["$set"].items():
                root = self.doc
                parts = k.split(".")
                for part in parts[:-1]:
                    root = root.setdefault(part, {})
                root[parts[-1]] = v
        return SimpleNamespace(modified_count=1)


class Cycles:
    def __init__(self):
        self.doc = {
            "assignment_cycle_id": CYCLE, "lead_id": ID,
            "assigned_to_user_id": OWNER,
            "cycle_status": "active", "unassigned_at": None,
            "temperature_at_assignment": "HOT",
            "assigned_at": ASSIGNED,
            "sla_started_at": ASSIGNED, "hot_started_at": ASSIGNED,
            "sla_policy_version": "sla_visual_v1_20260723",
        }

    def update_one(self, query, mutation):
        if (query.get("assignment_cycle_id") != CYCLE
                or self.doc.get("cycle_status") != query.get("cycle_status")
                or self.doc.get("assigned_to_user_id") != query.get("assigned_to_user_id")
                or self.doc.get("unassigned_at") != query.get("unassigned_at")
                or self.doc.get("temperature_at_assignment") != query.get("temperature_at_assignment")
                or self.doc.get("hot_auto_sla_exemption", {}).get("status") == "active"):
            return SimpleNamespace(modified_count=0)
        self.doc.update(deepcopy(mutation["$set"]))
        return SimpleNamespace(modified_count=1)


class Outbox:
    def __init__(self):
        self.docs = {}

    def insert_one(self, doc):
        if doc["_id"] in self.docs:
            raise DuplicateKeyError("duplicate")
        self.docs[doc["_id"]] = deepcopy(doc)

    def update_one(self, query, update):
        self.docs[query["_id"]].update(update["$set"])


class OfficeJobs:
    def __init__(self):
        self.record = None

    def find_one(self, query, projection):
        row = self.record
        if not isinstance(row, dict):
            return None
        for key in ("kind", "message_type", "message_domain", "is_from_me", "lead_id", "phone"):
            if row.get(key) != query.get(key):
                return None
        when = row.get("received_at")
        interval = query.get("received_at") or {}
        if not isinstance(when, datetime) or not (interval.get("$gte") <= when <= interval.get("$lte")):
            return None
        if not row.get("inbound_provider_message_id"):
            return None
        return dict(row)


class Users:
    def find_one(self, query, projection):
        if str(query.get("_id")) != OWNER:
            return None
        return {
            "_id": ObjectId(OWNER), "nombre": "Hernán Castro",
            "wasender": {"session_id": 133517},
        }


@pytest.fixture
def context(monkeypatch):
    leads, cycles = Leads(), Cycles()
    db = {
        "leads": leads,
        "crm_assignment_cycles": cycles,
        "usuarios": Users(),
        "chatbot_inbound_jobs": OfficeJobs(),
        hot.OUTBOX: Outbox(),
    }
    monkeypatch.setenv("PROCASA_EXEC_HOT_AUTO_ENABLED", "1")
    monkeypatch.setenv("PROCASA_EXEC_HOT_AUTO_SESSION_IDS", "133517")
    monkeypatch.setenv("PROCASA_EXEC_HOT_AUTO_CUTOVER_AT", (NOW - timedelta(minutes=30)).isoformat())
    monkeypatch.setattr(hot, "active_assignment_cycle", lambda _db, lid: cycles.doc)
    monkeypatch.setattr(hot, "_authorized_lead",
                        lambda _db, phone, user_id: (leads.doc, cycles.doc))
    monkeypatch.setattr(hot, "_session", lambda _db, sid: {
        "user_id": OWNER, "session_id": sid, "capture_enabled": True,
        "token": "executive-token-only",
    })
    monkeypatch.setattr(hot, "lead_must_not_be_contacted", lambda _: False)
    return db, leads, cycles


def response(*, ok=True, pid="provider-hot-001"):
    return SimpleNamespace(
        status_code=200 if ok else 403,
        content=b"{}",
        json=lambda: {"success": ok, "data": {"msgId": pid}},
    )


def send(db, post):
    return hot.send_hot_auto(db, lead_id=ID, post=post, now=NOW)


def test_default_is_disabled_no_scan_no_call(context, monkeypatch):
    db, _, _ = context
    monkeypatch.delenv("PROCASA_EXEC_HOT_AUTO_ENABLED")
    assert not hot.enabled()
    assert hot.process_hot_auto_batch(db, now=NOW)["examined"] == 0
    with pytest.raises(hot.OutboundBlocked, match="hot_auto_disabled"):
        send(db, lambda *a, **k: pytest.fail("unexpected POST"))
    assert not db[hot.OUTBOX].docs


@pytest.mark.parametrize("session_allowlist", ["", "*", "133517,", "133517,133517", "bad", "133526"])
def test_session_not_whitelisted_never_sends(context, monkeypatch, session_allowlist):
    db, _, _ = context
    monkeypatch.setenv("PROCASA_EXEC_HOT_AUTO_SESSION_IDS", session_allowlist)
    with pytest.raises(hot.OutboundBlocked):
        send(db, lambda *a, **k: pytest.fail("unexpected POST"))
    assert not db[hot.OUTBOX].docs


def test_only_new_hot_cycles_after_explicit_cutover(context):
    db, leads, cycles = context
    cycles.doc["assigned_at"] = NOW - timedelta(days=1)
    with pytest.raises(hot.OutboundBlocked, match="before_cutover"):
        send(db, lambda *a, **k: pytest.fail("old HOT never backfilled"))
    cycles.doc["assigned_at"] = ASSIGNED
    cycles.doc["temperature_at_assignment"] = "NORMAL"
    with pytest.raises(hot.OutboundBlocked, match="not_active_hot_cycle"):
        send(db, lambda *a, **k: pytest.fail("not HOT"))
    cycles.doc["temperature_at_assignment"] = "HOT"
    cycles.doc["sla_start_status"] = "pending_notification"
    with pytest.raises(hot.OutboundBlocked, match="not_active_hot_cycle"):
        send(db, lambda *a, **k: pytest.fail("owner notification pending"))
    assert not db[hot.OUTBOX].docs


def test_no_permission_or_optout_means_no_auto_contact_and_no_exemption(context, monkeypatch):
    db, leads, cycles = context
    leads.doc["messages"].clear()
    with pytest.raises(hot.OutboundBlocked, match="whatsapp_permission_missing"):
        send(db, lambda *a, **k: pytest.fail("no consent"))
    leads.doc["messages"] = [inbound()]
    monkeypatch.setattr(hot, "lead_must_not_be_contacted", lambda _: True)
    with pytest.raises(hot.OutboundBlocked, match="customer_blocked"):
        send(db, lambda *a, **k: pytest.fail("optout"))
    assert hot.sla_exemption(cycles.doc, leads.doc) is None
    assert not db[hot.OUTBOX].docs


def test_accepted_send_creates_automation_not_human_and_active_sla_exemption(context):
    db, leads, cycles = context
    called = []
    def transport(url, **kwargs):
        called.append((url, kwargs))
        return response()
    result = send(db, transport)
    assert result["status"] == "accepted_exempt"
    assert len(called) == 1
    assert called[0][0].endswith("/send-message")
    assert called[0][1]["headers"]["Authorization"] == "Bearer executive-token-only"
    assert called[0][1]["json"]["to"] == "+56996762064"
    assert "PROCASA" in called[0][1]["json"]["text"]
    assert len(leads.doc["messages"]) == 2
    msg = leads.doc["messages"][-1]
    assert msg["actor_type"] == "automation"
    assert msg["source"] == "crm_hot_auto_api"
    assert msg["assigned_executive_id"] == OWNER
    assert msg["wasender_session_id"] == "133517"
    assert msg["provider_message_id"] == "provider-hot-001"
    assert msg["timestamp"].endswith("+00:00")
    assert cycles.doc["hot_auto_sla_exemption"]["status"] == "active"
    assert cycles.doc["hot_auto_sla_exemption"]["executive_id"] == OWNER
    assert cycles.doc["hot_auto_sla_exemption"]["policy_version"] == hot.POLICY
    assert "first_valid_management_at" not in cycles.doc
    assert calculate_cycle_sla(cycle=cycles.doc, lead=leads.doc, now=NOW)["status"] == "exempt"
    expired = canonical_expiration_recheck(cycles.doc, leads.doc, now=NOW + timedelta(hours=4))
    assert not expired.expired
    assert expired.breach_at is None
    assert expired.canonical_state == "EXEMPT_AUTO_HOT"
    with pytest.raises(hot.OutboundBlocked, match="already_exempt"):
        send(db, lambda *a, **k: pytest.fail("never send another greeting"))


def test_provider_failure_keeps_original_hot_sla(context):
    db, leads, cycles = context
    r = send(db, lambda *a, **k: response(ok=False))
    assert r["status"] == "rejected"
    assert hot.sla_exemption(cycles.doc, leads.doc) is None
    assert len(leads.doc["messages"]) == 1
    assert calculate_cycle_sla(cycle=cycles.doc, lead=leads.doc, now=NOW)["status"] != "exempt"
    r2 = send(db, lambda *a, **k: pytest.fail("must not retry an accepted or rejected attempt"))
    assert r2["status"] == "duplicate"


def test_timeout_never_causes_retry_or_false_exemption(context):
    db, leads, cycles = context
    def timeout(*a, **k):
        raise TimeoutError("provider uncertain")
    assert send(db, timeout)["status"] == "delivery_unknown"
    assert send(db, lambda *a, **k: pytest.fail("never retry on uncertainty"))["status"] == "duplicate"
    assert hot.sla_exemption(cycles.doc, leads.doc) is None


def test_provider_accept_without_id_does_not_exempt(context):
    db, leads, cycles = context
    assert send(db, lambda *a, **k: response(pid=None))["status"] == "accepted_without_provider_id"
    assert len(leads.doc["messages"]) == 1
    assert hot.sla_exemption(cycles.doc, leads.doc) is None


def test_sla_exemption_requires_correct_cycle_owner_status_and_provider_evidence(context):
    db, leads, cycles = context
    send(db, lambda *a, **k: response())
    assert hot.sla_exemption(cycles.doc, leads.doc)
    bad = deepcopy(cycles.doc)
    bad["hot_auto_sla_exemption"]["executive_id"] = "wrong-owner"
    assert not hot.sla_exemption(bad, leads.doc)
    bad = deepcopy(cycles.doc)
    bad["hot_auto_sla_exemption"]["provider_message_id"] = None
    assert not hot.sla_exemption(bad, leads.doc)
    bad = deepcopy(cycles.doc)
    bad["temperature_at_assignment"] = "NORMAL"
    assert not hot.sla_exemption(bad, leads.doc)
    bad = deepcopy(cycles.doc)
    bad["hot_auto_sla_exemption"]["accepted_at"] = bad["assigned_at"] - timedelta(seconds=1)
    assert not hot.sla_exemption(bad, leads.doc)
    bad = deepcopy(cycles.doc)
    bad["hot_auto_sla_exemption"].pop("outbox_id", None)
    assert not hot.sla_exemption(bad, leads.doc)

    bad = deepcopy(cycles.doc)
    bad["cycle_status"] = "closed"
    assert not hot.sla_exemption(bad, leads.doc)
    assert "first_valid_management_at" not in cycles.doc


def test_reassignment_transaction_cannot_close_exempt_cycle(context):
    db, leads, cycles = context
    send(db, lambda *a, **k: response())
    with pytest.raises(TransactionContractError, match="hot_exec_auto_exempt"):
        build_transaction_plan(
            decision={}, source_cycle=cycles.doc, lead=leads.doc,
            target_user={}, reassigned_at=NOW,
            effective_sla_started_at=None,
            expected_sla_policy_version="sla_visual_v1_20260723",
        )
    filtered = build_source_cycle_filter(
        {"lead_id": str(ID), "current_assignment_cycle_id": CYCLE,
         "previous_owner_user_id": OWNER, "assignment_number": 0},
        cycles.doc,
        expected_sla_policy_version="sla_visual_v1_20260723",
    )
    assert {"hot_auto_sla_exemption.status": {"$ne": "active"}} in filtered["$and"]


def test_explicit_permission_can_be_verified_without_recent_chat(context):
    db, leads, _ = context
    leads.doc["messages"] = []
    leads.doc["crm_hot_auto"] = {
        "whatsapp_contact_authorization": {
            "status": "ACTIVE", "channel": "WHATSAPP",
            "scope": "SALE_INQUIRY",
            "phone_normalized": "+56996762064",
            "evidence_reference": "signed-form-2026",
            "reviewed_by": "admin",
            "reviewed_at": NOW - timedelta(days=1),
            "revoked_at": None,
        },
    }
    assert hot.eligible_hot_auto(db, lead=leads.doc,
                                 cycle=db["crm_assignment_cycles"].doc, now=NOW)


def test_invalid_and_old_wa_inbound_does_not_authorize(context):
    db, leads, _ = context
    msg = inbound()
    msg["timestamp"] = (NOW - timedelta(days=2)).isoformat()
    leads.doc["messages"] = [msg]
    with pytest.raises(hot.OutboundBlocked, match="whatsapp_permission_missing"):
        send(db, lambda *a, **k: pytest.fail("old inbound"))
    msg["timestamp"] = (NOW - timedelta(minutes=10)).isoformat()
    msg["source"] = "unknown"
    msg["channel"] = "portal"
    leads.doc["messages"] = [msg]
    with pytest.raises(hot.OutboundBlocked, match="whatsapp_permission_missing"):
        send(db, lambda *a, **k: pytest.fail("portal is not optin"))


def test_hot_auto_does_not_duplicate_existing_executive_message(context):
    db, leads, cycles = context
    leads.doc["messages"].append({
        "assignment_cycle_id": CYCLE,
        "channel": "executive_whatsapp",
        "actor_type": "human_agent",
        "provider_message_id": "already-contacted",
    })
    with pytest.raises(hot.OutboundBlocked, match="executive_already_contacted"):
        send(db, lambda *a, **k: pytest.fail("would double greet"))
    assert hot.sla_exemption(cycles.doc, leads.doc) is None


def test_temperature_mirror_mismatch_fails_closed(context):
    db, leads, _ = context
    leads.doc["lead_temperature_effective"] = "COLD"
    with pytest.raises(hot.OutboundBlocked):
        send(db, lambda *a, **k: pytest.fail("not HOT"))
    assert not db[hot.OUTBOX].docs


def test_verified_recent_office_inbound_job_allows_real_hot_contact(context):
    db, leads, cycles = context
    leads.doc["messages"] = []
    db["chatbot_inbound_jobs"].record = {
        "kind": "inbound_job",
        "message_type": "inbound_message",
        "message_domain": "chatbot",
        "is_from_me": False,
        "lead_id": str(ID), "phone": leads.doc["phone"],
        "received_at": NOW - timedelta(minutes=8),
        "inbound_provider_message_id": "real-provider-inbound-001",
    }
    result = send(db, lambda *_a, **_kw: response())
    assert result["status"] == "accepted_exempt"
    assert cycles.doc["hot_auto_sla_exemption"]["provider_message_id"] == "provider-hot-001"


@pytest.mark.parametrize("field,bad", [
    ("lead_id", "other-lead"),
    ("phone", "+56999998888"),
    ("message_domain", "campaign"),
    ("message_type", "outbound_message"),
    ("kind", "batch"),
    ("is_from_me", True),
    ("inbound_provider_message_id", ""),
    ("received_at", NOW - timedelta(days=3)),
])
def test_untrusted_office_job_cannot_authorize_hot_send(context, field, bad):
    db, leads, _ = context
    leads.doc["messages"] = []
    row = {
        "kind": "inbound_job", "message_type": "inbound_message",
        "message_domain": "chatbot", "is_from_me": False,
        "lead_id": str(ID), "phone": leads.doc["phone"],
        "received_at": NOW - timedelta(minutes=8),
        "inbound_provider_message_id": "inbound-provider-001",
    }
    row[field] = bad
    db["chatbot_inbound_jobs"].record = row
    with pytest.raises(hot.OutboundBlocked, match="whatsapp_permission_missing"):
        send(db, lambda *_a, **_kw: pytest.fail("untrusted inbound must not be sent"))


def test_hot_greeting_uses_property_and_visit_intent_without_reasking_for_details(context):
    db, leads, cycles = context
    leads.doc["prospecto"]["nombre"] = "Juan Pérez"
    leads.doc["prospecto"]["codigo_propiedad"] = "16479"
    leads.doc["prospecto"]["comuna"] = "Maipú"
    leads.doc["messages"] = [{
        **inbound(), "conversation_id": "conv-1",
        "content": "Quiero agendar una visita el sábado a las 11:00. Mi teléfono es +56912345678",
    }]
    sent = []
    result = send(db, lambda _url, **kwargs: (sent.append(kwargs["json"]["text"]) or response()))
    assert result["status"] == "accepted_exempt"
    assert len(sent) == 1
    greeting = sent[0]
    assert greeting.startswith("Hola Juan, soy Hernán, tu asesor inmobiliario asignado en PROCASA.")
    assert "una búsqueda" not in greeting
    assert "16479" not in greeting
    assert "búsqueda" not in greeting
    assert "Maipú" in greeting
    assert "propusiste un horario para visitar la propiedad" in greeting
    assert "sábado" not in greeting  # do not echo proposed personal details
    assert "+56912345678" not in greeting
    assert "¿Qué día" not in greeting  # do not ask again for a supplied day
    assert leads.doc["messages"][-1]["content"] == greeting
    assert hot.sla_exemption(cycles.doc, leads.doc) is not None
    assert calculate_cycle_sla(cycle=cycles.doc, lead=leads.doc, now=NOW)["status"] == "exempt"


def test_hot_greeting_price_context_excludes_raw_user_content(context):
    _db, leads, _cycles = context
    leads.doc["prospecto"]["codigo_propiedad"] = 6436
    leads.doc["prospecto"]["comuna"] = "Peñaflor"
    leads.doc["messages"] = [{
        **inbound(), "conversation_id": "conv-1",
        "content": "¿Cuál es el precio y cuánto vale? Escribirme a privado@example.org",
    }]
    greeting = hot._message_for(leads.doc, "Paula Morales", assigned_at=ASSIGNED, now=NOW)
    assert "soy Paula" in greeting
    assert "6436" not in greeting
    assert "Peñaflor" in greeting
    assert "consultaste por el precio" in greeting
    assert "privado@example.org" not in greeting


def test_hot_greeting_context_skips_portal_untrusted_wrong_conversation_and_old(context):
    _db, leads, _cycles = context
    leads.doc["prospecto"]["codigo_propiedad"] = "16479"
    leads.doc["messages"] = [
        {**inbound(), "source": "prop360", "channel": "portal",
         "content": "Quiero visitar una casa", "conversation_id": "conv-1"},
        {**inbound(), "content": "Me interesan los gastos comunes",
         "source": "", "channel": "", "conversation_id": "other"},
        {**inbound(), "content": "¿Está disponible?",
         "timestamp": (NOW - timedelta(days=2)).isoformat(), "conversation_id": "conv-1"},
        {"role": "assistant", "actor_type": "bot",
         "content": "El cliente pidió visita y dejó su teléfono",
         "timestamp": (NOW - timedelta(minutes=10)).isoformat(), "conversation_id": "conv-1"},
        {"role": "user", "actor_type": "human_agent", "channel": "executive_whatsapp",
         "content": "quiero visitar", "timestamp": (NOW - timedelta(minutes=10)).isoformat()},
    ]
    greeting = hot._message_for(leads.doc, "Hernán Castro", assigned_at=ASSIGNED, now=NOW)
    assert greeting == hot._message_for(leads.doc, "Hernán Castro")


def test_hot_greeting_uses_real_unlabelled_canonical_bot_chat(context):
    _db, leads, _cycles = context
    leads.doc["messages"] = [{
        "role": "user", "actor_type": "customer", "conversation_id": "conv-1",
        "content": "Hola, ¿cuánto se paga de gastos comunes?",
        "timestamp": (NOW - timedelta(minutes=9)).isoformat(),
    }]
    greeting = hot._message_for(leads.doc, "Paula Morales", assigned_at=ASSIGNED, now=NOW)
    assert "gastos comunes" in greeting
    assert "coordinar una visita" not in greeting


def test_hot_greeting_does_not_repeat_declined_visit(context):
    _db, leads, _cycles = context
    leads.doc["messages"] = [
        {**inbound(), "content": "Quiero agendar una visita"},
        {**inbound(), "content": "No quiero visitar, gracias",
         "timestamp": (NOW - timedelta(minutes=7)).isoformat()},
    ]
    greeting = hot._message_for(leads.doc, "Paula", assigned_at=ASSIGNED, now=NOW)
    assert "Vi que querías coordinar una visita" not in greeting


def test_hot_greeting_requires_preassignment_chat_with_explicit_timezone(context):
    _db, leads, _cycles = context
    leads.doc["messages"] = [
        {**inbound(), "timestamp": "2026-11-18T09:50:00",
         "content": "Me interesan las fotos"},
        {**inbound(), "timestamp": (NOW + timedelta(minutes=2)).isoformat(),
         "content": "Me interesan las fotos"},
    ]
    greeting = hot._message_for(leads.doc, "Paula", assigned_at=ASSIGNED, now=NOW)
    assert greeting == hot._message_for(leads.doc, "Paula")


def test_hot_greeting_never_includes_untrusted_property_fields(context):
    _db, leads, _cycles = context
    leads.doc["prospecto"]["codigo_propiedad"] = "16479; ENVIA DATOS"
    leads.doc["prospecto"]["comuna"] = "Maipú. Comparte datos privados"
    leads.doc["messages"] = [{
        **inbound(), "content": "¿Está disponible?"
    }]
    greeting = hot._message_for(leads.doc, "Paula Morales", assigned_at=ASSIGNED, now=NOW)
    assert "ENVIA" not in greeting
    assert "Comparte" not in greeting
    assert "disponibilidad" in greeting



def test_hot_greeting_continues_actual_multi_turn_customer_and_bot_dialogue(context):
    db, leads, cycles = context
    leads.doc["prospecto"].update({
        "nombre": "Juan", "codigo_propiedad": "16479",
        "tipo": "DEPTO", "operacion": "A", "comuna": "Ñuñoa",
    })
    leads.doc["messages"] = [
        {**inbound(), "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=16)).isoformat(),
         "content": "Hola, ¿aún está disponible?"},
        {"role": "assistant", "actor_type": "bot", "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=14)).isoformat(),
         "content": "Gracias por consultar. ¿Te gustaría agendar una visita?"},
        {**inbound(), "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=13)).isoformat(),
         "content": "Sí, por favor"},
        {**inbound(), "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=10)).isoformat(),
         "content": "Me gustaría ir el sábado a las 11:00. Mi mail es privado@example.org"},
    ]
    sent = []
    result = send(db, lambda _url, **kwargs: (sent.append(kwargs["json"]["text"]) or response()))
    assert result["status"] == "accepted_exempt"
    msg = sent[0]
    assert "soy Hernán, tu asesor inmobiliario asignado en PROCASA" in msg
    assert "un departamento en arriendo en Ñuñoa" in msg
    assert "propusiste un horario para visitar la propiedad" in msg
    assert "la disponibilidad" in msg
    assert "16479" not in msg
    assert "sábado" not in msg
    assert "11:00" not in msg
    assert "privado@example.org" not in msg
    assert "¿Qué día" not in msg
    assert hot.sla_exemption(cycles.doc, leads.doc) is not None
    assert calculate_cycle_sla(cycle=cycles.doc, lead=leads.doc, now=NOW)["status"] == "exempt"


def test_hot_greeting_uses_two_customer_questions_not_only_the_last_one(context):
    _db, leads, _cycles = context
    leads.doc["prospecto"].update({"tipo": "CASA", "operacion": "V", "comuna": "Maipú"})
    leads.doc["messages"] = [
        {**inbound(), "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=15)).isoformat(),
         "content": "Hola, ¿qué precio tiene?"},
        {**inbound(), "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=9)).isoformat(),
         "content": "Y ¿cuánto se paga de gastos comunes?"},
    ]
    greeting = hot._message_for(leads.doc, "Paula Morales", assigned_at=ASSIGNED, now=NOW)
    assert "soy Paula, tu asesora inmobiliaria asignada en PROCASA" in greeting
    assert "una casa en venta en Maipú" in greeting
    assert "los gastos comunes" in greeting
    assert "el precio" in greeting
    assert "código" not in greeting


def test_hot_greeting_respects_visit_decline_but_keeps_other_topic(context):
    _db, leads, _cycles = context
    leads.doc["messages"] = [
        {**inbound(), "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=20)).isoformat(),
         "content": "Quiero agendar una visita y saber el precio"},
        {**inbound(), "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=10)).isoformat(),
         "content": "No quiero visitar la propiedad; solo necesito saber el precio"},
    ]
    greeting = hot._message_for(leads.doc, "Paula", assigned_at=ASSIGNED, now=NOW)
    assert "coordinar una visita" not in greeting
    assert "propusiste un horario" not in greeting
    assert "el precio" in greeting


def test_hot_greeting_does_not_assume_bot_offer_without_customer_acceptance(context):
    _db, leads, _cycles = context
    leads.doc["messages"] = [
        {**inbound(), "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=15)).isoformat(),
         "content": "Hola, necesito información"},
        {"role": "assistant", "actor_type": "bot", "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=10)).isoformat(),
         "content": "¿Quieres agendar una visita?"},
    ]
    greeting = hot._message_for(leads.doc, "Erika", assigned_at=ASSIGNED, now=NOW)
    assert "soy Erika, tu asesora inmobiliaria asignada" in greeting
    assert "coordinar una visita" not in greeting
    assert "propusiste un horario" not in greeting


def test_hot_greeting_short_client_confirmation_reuses_real_bot_question(context):
    _db, leads, _cycles = context
    leads.doc["messages"] = [
        {"role": "assistant", "actor_type": "bot", "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=12)).isoformat(),
         "content": "¿Te gustaría agendar una visita?"},
        {**inbound(), "conversation_id": "conv-1",
         "timestamp": (NOW - timedelta(minutes=11)).isoformat(),
         "content": "Sí, por favor"},
    ]
    greeting = hot._message_for(leads.doc, "Susana", assigned_at=ASSIGNED, now=NOW)
    assert "soy Susana, tu asesora inmobiliaria asignada" in greeting
    assert "coordinar una visita" in greeting


def test_hot_greeting_no_property_codes_or_unverified_narrative(context):
    _db, leads, _cycles = context
    leads.doc["prospecto"].update({
        "codigo_propiedad": "16479", "tipo": "DEPTO",
        "operacion": "V", "comuna": "Las Condes",
    })
    leads.doc["property_resolution_error"] = "property_unresolved"
    leads.doc["messages"] = [
        {**inbound(), "conversation_id": "conv-1",
         "content": "Quiero conocer el precio de la propiedad 16479 y si está disponible"},
    ]
    greeting = hot._message_for(leads.doc, "Paula", assigned_at=ASSIGNED, now=NOW)
    assert "16479" not in greeting
    assert "Las Condes" not in greeting  # unresolved property, avoid wrong description
    assert "el precio" in greeting
    assert "la disponibilidad" in greeting


def test_hot_greeting_fallback_still_identifies_assigned_adviser(context):
    _db, leads, _cycles = context
    leads.doc["messages"] = []
    greeting = hot._message_for(leads.doc, "Mariela Arriagada", assigned_at=ASSIGNED, now=NOW)
    assert "soy Mariela, tu asesora inmobiliaria asignada en PROCASA" in greeting
    assert "Quedé a cargo de tu consulta" in greeting
    assert "código" not in greeting
    assert "visita" not in greeting


def test_hot_greeting_unknown_executive_uses_neutral_role(context):
    _db, leads, _cycles = context
    greeting = hot._message_for(leads.doc, "Alex Rivera")
    assert "tu contacto asignado del equipo de asesoría inmobiliaria" in greeting
