"""V12 regression suite: isolated commercial follow-up and no real-CRM effects."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pymongo.errors import DuplicateKeyError

from chatbot.price_drop_pilot_followup_v12 import followup_reply, VERSION
from chatbot.price_drop_pilot_isolation import _visit_parts
from chatbot.price_drop_pilot_dual_notice_sim import send_dual_test_notices

H_VISIT = [
    {"customer_text": "cuando se puede ir a visistar?", "bot_text": "¿Qué día te acomoda?"},
    {"customer_text": "ok, me interesa", "bot_text": "¿Qué día te acomodaría?"},
]
H_TIME = H_VISIT + [
    {"customer_text": "lunes a las 10", "bot_text": "Tengo presente tu preferencia."},
]
H_YES = [{"customer_text": "quiero verla", "bot_text": "¿Te interesa visitar el departamento?"}]


@pytest.mark.parametrize("customer,history,roles,expected", [
    ("ok, me interesa", [], (), "Qué bueno"),
    ("me interesa", [], (), "conocerlo"),
    ("estoy interesado", [], (), "conocerlo"),
    ("quiero agendar", [], (), "Qué día"),
    ("cuando se puede ir a visistar?", [], (), "Qué día"),
    ("¿cuándo se puede visitar?", [], (), "Qué día"),
    ("me puedes llamar?", [], (), "no hay una solicitud"),
    ("que me conatcte pronto que tengo otras vistas", H_VISIT, (), "urge"),
    ("¿Quién me contactará?", H_VISIT, ("ADMIN", "EXECUTIVE"), "dos avisos de prueba"),
    ("oka, gracias que me contactará?", H_VISIT, ("ADMIN", "EXECUTIVE"), "dos avisos de prueba"),
    ("quién me llamará?", H_VISIT, (), "no hay una solicitud"),
    ("me pueden contactar", H_VISIT, ("ADMIN",), "un aviso de prueba"),
    ("lunes a las 10", H_VISIT, ("ADMIN", "EXECUTIVE"), "lunes a las 10"),
    ("martes en la tarde", H_VISIT, (), "martes"),
    ("mañana a las 10 o a las 11", H_VISIT, (), "10 o 11"),
    ("sábado a las 12", H_VISIT, (), "sábado"),
    ("viernes", H_VISIT, (), "mañana o tarde"),
    ("a las 10", H_VISIT, (), "Qué día"),
    ("ok me interesa, lunes a las 10", H_VISIT, (), "lunes a las 10"),
    ("ya quedó agendada la visita?", H_TIME, ("ADMIN", "EXECUTIVE"), "no se ha solicitado"),
    ("tengo otras visitas, que me contacte pronto", H_TIME, ("ADMIN", "EXECUTIVE"), "urge"),
    ("sí", H_YES, (), "Qué bueno"),
    ("claro", H_YES, (), "Qué bueno"),
    ("quiero ir a verlo", H_VISIT, (), "Qué día"),
])
def test_24_commercial_scenarios(customer, history, roles, expected):
    reply = followup_reply(customer, history, parse_parts=_visit_parts, notice_roles=roles)
    assert reply is not None, (customer, history)
    assert expected.casefold() in reply.casefold(), (customer, reply)
    # No fabricated booking or real human notifications.
    assert "visita confirmada para" not in reply.casefold()
    assert "avisé al ejecutivo" not in reply.casefold()
    assert "te llamará mañana" not in reply.casefold()


@pytest.mark.parametrize("customer", [
    "hola", "cuánto bajó?", "es negociable?", "precio?",
    "no me interesa", "no quiero visitar", "stop", "no me escriban",
])
def test_8_neutral_or_negative_scenarios_are_not_handoff(customer):
    assert followup_reply(customer, [], parse_parts=_visit_parts) is None


class _NoticeCollection:
    def __init__(self):
        self.rows = {}

    def insert_one(self, record):
        if record["_id"] in self.rows:
            raise DuplicateKeyError("duplicate test notice")
        self.rows[record["_id"]] = dict(record)

    def update_one(self, query, patch):
        self.rows[query["_id"]].update(patch.get("$set", {}))


class _PrivateDB(dict):
    def __getitem__(self, collection):
        if collection != "crm_price_drop_pilot_test_turns_v1":
            raise AssertionError("Attempted production/CRM collection: " + collection)
        return super().__getitem__(collection)


def test_v12_only_two_durable_allowlisted_notices_no_duplicate(monkeypatch):
    phone = "+56983219804"
    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "1")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", phone)
    monkeypatch.setenv("PRICE_DROP_PILOT_EXEC_NOTICE_SIMULATION", "1")
    turns = _NoticeCollection()
    db = _PrivateDB({"crm_price_drop_pilot_test_turns_v1": turns})
    ctx = {
        "campaign_version": VERSION, "campaign_id": "test:v12-single",
        "property_code": "6436", "old_price_uf": 2790.0, "new_price_uf": 2690.0,
    }
    sends = []

    def sender(recipient, message):
        assert recipient == phone
        sends.append(message)
        return {"success": True, "provider_message_id": str(1000 + len(sends)),
                "delivery_status": "pending"}

    first = send_dual_test_notices(
        db, ctx=ctx, phone=phone, customer_text="ok, me interesa",
        history=[], inbound_message_id="first-inbound", sender=sender,
    )
    second = send_dual_test_notices(
        db, ctx=ctx, phone=phone, customer_text="ok, me interesa",
        history=[], inbound_message_id="repeat-inbound", sender=sender,
    )
    assert first["roles"] == {"ADMIN": "SENT", "EXECUTIVE": "SENT"}
    assert second["roles"] == {
        "ADMIN": "ALREADY_ATTEMPTED", "EXECUTIVE": "ALREADY_ATTEMPTED"
    }
    assert len(sends) == 2
    assert all(phone in message for message in sends)
    assert {r["test_notification_role"] for r in turns.rows.values()} == {"ADMIN", "EXECUTIVE"}
    assert all(r["status"] == "NOTICE_SENT" for r in turns.rows.values())
    assert "PRUEBA" in sends[0] and "PRUEBA" in sends[1]


def test_v12_never_sends_to_other_phone(monkeypatch):
    monkeypatch.setenv("PRICE_DROP_PILOT_ENABLED", "1")
    monkeypatch.setenv("PRICE_DROP_PILOT_TEST_PHONE", "+56983219804")
    monkeypatch.setenv("PRICE_DROP_PILOT_EXEC_NOTICE_SIMULATION", "1")
    db = _PrivateDB({"crm_price_drop_pilot_test_turns_v1": _NoticeCollection()})
    result = send_dual_test_notices(
        db,
        ctx={"campaign_version": VERSION, "campaign_id": "test:v12",
             "property_code": "6436", "old_price_uf": 2790.0, "new_price_uf": 2690.0},
        phone="+56999990000", customer_text="me interesa",
        history=[], inbound_message_id="inb", sender=lambda *args: (_ for _ in ()).throw(
            AssertionError("Never send to non-test number")
        ),
    )
    assert result["status"] == "OUTSIDE_TEST_ALLOWLIST"


def test_day_after_simple_interest_prompt_remembers_context():
    history = [{"customer_text": "ok, me interesa",
                "bot_text": "¡Qué bueno! ¿Te gustaría conocerlo en persona? "
                            "Dime qué día te acomodaría."}]
    reply = followup_reply("lunes a las 10", history, parse_parts=_visit_parts)
    assert "lunes a las 10" in reply
    assert "no una visita confirmada" in reply


def test_known_visit_preference_not_requested_twice():
    history = H_TIME
    reply = followup_reply("¿quién me contactará?", history, parse_parts=_visit_parts,
                           notice_roles=("ADMIN", "EXECUTIVE"))
    assert "lunes a las 10" in reply
    assert "¿Qué día" not in reply
    assert "avisos de prueba" in reply
