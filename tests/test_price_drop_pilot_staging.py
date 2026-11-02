"""Read-only staged price-drop pilot. No actual customers or providers contacted."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from chatbot.price_drop_pilot_staging import preview_candidates, _message
from chatbot.price_drop_pilot_isolation import build_test_reply


CHANGE = "2026-10-14T11:52:43.405868+00:00"


class Cursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, key, order):
        self.docs.sort(key=lambda x: str(x.get(key) or ""), reverse=order == -1)
        return self

    def limit(self, amount):
        self.docs = self.docs[:amount]
        return self

    def __iter__(self):
        return iter(self.docs)


class ReadOnlyCollection:
    def __init__(self, docs):
        self.docs = docs
        self.reads = 0

    def find(self, query, projection=None):
        self.reads += 1
        if "prospecto.codigo" in query:
            return Cursor([x for x in self.docs if (x.get("prospecto") or {}).get("codigo") == query["prospecto.codigo"]])
        if "$or" in query:
            variants = {v for clause in query["$or"] for obj in clause.values() for v in obj.get("$in", [])}
            return Cursor([x for x in self.docs if
                           any(str(x.get(key) or "") in variants
                               for key in ("phone", "contact_phone", "contact_phone_normalized"))])
        if query.get("codigo") == "6436":
            return Cursor([x for x in self.docs if x.get("codigo") == "6436"])
        return Cursor([])

    def find_one(self, query, projection=None):
        self.reads += 1
        if "phone_normalized" in query:
            for doc in self.docs:
                if doc.get("phone_normalized") == query["phone_normalized"] and doc.get("property_code") == query.get("property_code"):
                    return doc
            return None
        return next((x for x in self.docs if x.get("codigo") == query.get("codigo")), None)

    def __getattr__(self, name):
        if name.startswith(("insert", "update", "delete", "replace", "bulk", "find_one_and")):
            raise AssertionError("A staging preview attempted a database write: " + name)
        raise AttributeError(name)


def _lead(phone, *, lead_id, hot=True, when="2026-08-28T12:00:00-04:00",
          name=None, messages=None):
    return {
        "_id": lead_id, "phone": phone, "prospecto": {
            "codigo": "6436", "operacion": "Venta",
            "nombre": name,
        }, "lead_temperature_effective": "HOT" if hot else "COLD",
        "created_at": when, "ejecutivo_asignado": "Mariela Arriagada",
        "messages": messages or [],
    }


def _db(leads=None, *, new_price=2690, history=True):
    return {
        "universo_cartera_prop360_historial": ReadOnlyCollection([
            {"codigo": "6436", "campo": "precio_publicado", "tipo_evento": "cambio_precio",
             "moneda_anterior": "UF", "moneda_nueva": "UF",
             "monto_anterior": 2790, "monto_nuevo": 2690, "fecha": CHANGE}
        ] if history else []),
        "universo_cartera_prop360": ReadOnlyCollection([
            {"codigo": "6436", "disponible_prop360": True,
             "estado": {"oficina": "PROCASA SUCRE"},
             "tipo_operacion": {"venta": True, "precio_venta": {"precio_uf": new_price}},
             "ubicacion": {"comuna": "San Miguel"}}
        ]),
        "leads": ReadOnlyCollection(leads or []),
        "crm_price_drop_pilot_v1": ReadOnlyCollection([]),
    }


def test_hot_prechange_leads_preview_only_no_send_or_crm_writes():
    db = _db([
        _lead("+56988881111", lead_id="lead-1", messages=[
            {"role": "user", "content": "Hola, me llamo Carla.", "timestamp": "2026-08-20T00:00:00Z"}
        ]),
        _lead("+56988882222", lead_id="lead-2", name="Nombre del CRM no verificado"),
        _lead("+56988883333", lead_id="lead-3", hot=False),
    ])
    result = preview_candidates(db)
    assert result["status"] == "PREVIEW_ONLY"
    assert result["ready_to_send"] is False
    assert result["verified_price_change"]["date"].startswith("2026-10-14")
    assert len(result["review"]) == 2
    assert result["excluded"]["not_hot"] == 1
    assert "¡Hola, Carla!" in result["review"][0]["message_preview"]
    assert "Nombre del CRM no verificado" not in result["review"][1]["message_preview"]
    assert "habías" not in result["review"][0]["message_preview"]
    assert "UF 2.790" in result["review"][0]["message_preview"]
    assert "UF 2.690" in result["review"][0]["message_preview"]
    assert result["review"][0]["phone_masked"].startswith("+56 ••••")
    assert "+56988881111" not in repr(result)
    assert all(x["marketing_consent"] == "UNVERIFIED" for x in result["review"])
    assert all(x["decision"] == "HOLD_FOR_MANUAL_REVIEW" for x in result["review"])


@pytest.mark.parametrize("history,price", [(False, 2690), (True, 2700), (True, 2790)])
def test_price_is_rechecked_from_history_and_current_prop360(history, price):
    result = preview_candidates(_db([
        _lead("+56988881111", lead_id="lead-1")
    ], history=history, new_price=price))
    assert result["status"] == "BLOCKED_UNVERIFIED_PRICE_DROP"
    assert not result["review"]
    assert result["ready_to_send"] is False


def test_optout_duplicate_after_drop_and_unverified_property_are_excluded():
    stop = _lead("+56988881111", lead_id="lead-stop", messages=[
        {"role": "user", "content": "No me escriban más", "timestamp": "2026-09-01T12:00:00Z"}
    ])
    duplicate1 = _lead("+56988882222", lead_id="dup-a")
    duplicate2 = _lead("56988882222", lead_id="dup-b")
    after = _lead("+56988883333", lead_id="late", when="2026-10-20T00:00:00Z")
    contacted = _lead("+56988884444", lead_id="contacted", messages=[
        {"role": "assistant", "content": "Hola", "timestamp": "2026-10-20T00:00:00Z"}
    ])
    invalid_op = _lead("+56988885555", lead_id="invalid")
    invalid_op["prospecto"]["operacion"] = "Arriendo"
    result = preview_candidates(_db([stop, duplicate1, duplicate2, after, contacted, invalid_op]))
    assert not result["review"]
    for reason in ("prior_opt_out", "duplicate_identity", "missing_pre_drop_provenance",
                   "conversation_since_drop", "unverified_property_or_operation"):
        assert result["excluded"].get(reason, 0) >= 1
    assert result["ready_to_send"] is False


def test_previous_send_attempt_blocks_review_even_for_hot_lead():
    db = _db([_lead("+56988881111", lead_id="lead-1")])
    db["crm_price_drop_pilot_v1"].docs = [
        {"phone_normalized": "+56988881111", "property_code": "6436", "status": "SENT"}
    ]
    result = preview_candidates(db)
    assert result["review"] == []
    assert result["excluded"]["previous_campaign_attempt"] == 1


def test_limit_cannot_be_expanded_and_output_is_never_send_ready():
    db = _db([_lead("+56988881111", lead_id="lead-1")])
    with pytest.raises(ValueError):
        preview_candidates(db, max_review=4)
    assert preview_candidates(db, max_review=1)["ready_to_send"] is False


def test_simulated_whatsapp_conversation_does_not_touch_production_crm():
    ctx = {
        "property_code": "6436", "old_price_uf": 2790,
        "new_price_uf": 2690,
        "property_facts": {"banos": 2, "gastos_comunes_clp": 85000},
    }
    history = []
    dialogue = [
        ("hola", "¡Hola!"),
        ("¿Cuánto son los gastos comunes?", "85.000"),
        ("Quiero visitar el departamento", "¿Qué día"),
        ("prroximo viernes", "el próximo viernes"),
        ("en la mañana", "el próximo viernes por la mañana"),
        ("ya te respondi", "el próximo viernes por la mañana"),
    ]
    for message, expected in dialogue:
        answer = build_test_reply(
            message, ctx, history,
            llm=lambda *_: (_ for _ in ()).throw(AssertionError("No LLM needed for smoke transcript")),
        )
        assert expected in answer
        history.append({"customer_text": message, "bot_text": answer})
    assert "no hay una visita agendada" in history[-1]["bot_text"]


def test_staging_cli_and_core_do_not_offer_real_send():
    root = Path(__file__).resolve().parents[1]
    code = (root / "scripts" / "price_drop_pilot_preflight.py").read_text()
    core = (root / "chatbot" / "price_drop_pilot_staging.py").read_text()
    assert "send_whatsapp_message" not in code + core
    assert "insert_one(" not in core
    assert "update_one(" not in core
    assert '"--send"' not in code
    assert "ready_to_send" in core
    assert "False" in core



@pytest.mark.parametrize("text,intent,handoff", [
    ("sí, me interesa", "INTEREST", False),
    ("¿Cuántos baños tiene?", "ASK_INFO", False),
    ("Quiero visitar el departamento", "VISIT_REQUEST", True),
    ("¿Aceptan una oferta?", "OFFER_REQUEST", True),
    ("Que me llame una ejecutiva", "HUMAN_REQUEST", True),
    ("No quiero visitar", "NO_VISIT", False),
    ("No me interesa", "DECLINE", True),
    ("No me escriban más", "OPT_OUT", True),
    ("¿Y eso?", "UNRESOLVED", False),
])
def test_shadow_followup_does_not_notify_or_mutate_crm(text, intent, handoff):
    from chatbot.price_drop_pilot_shadow import shadow_reply_decision
    db = _db([_lead("+56988881111", lead_id="lead-1")])
    candidate = preview_candidates(db)["review"][0]
    result = shadow_reply_decision(candidate, text)
    assert result["status"] == "SHADOW_ONLY"
    assert result["customer_intent"] == intent
    assert result["requires_live_handoff_implementation"] is handoff
    assert result["existing_owner"] == "Mariela Arriagada"
    assert result["crm_written"] is False
    assert result["whatsapp_sent"] is False
    assert result["executive_notified"] is False
    assert result["visit_booked"] is False


def test_shadow_rejects_forged_send_ready_candidate():
    from chatbot.price_drop_pilot_shadow import shadow_reply_decision
    fake = {"decision": "SEND_NOW", "current_owner": "Mariela Arriagada",
            "property_code": "6436", "marketing_consent": "VERIFIED"}
    response = shadow_reply_decision(fake, "Quiero visitar")
    assert response == {
        "status": "BLOCKED_INVALID_PREVIEW",
        "crm_written": False, "whatsapp_sent": False, "executive_notified": False
    }


def test_shadow_module_cannot_import_production_sender():
    root = Path(__file__).resolve().parents[1]
    source = (root / "chatbot" / "price_drop_pilot_shadow.py").read_text()
    assert "send_whatsapp_message" not in source
    assert "from .crm_service" not in source
    assert "db[" not in source
    assert "insert_one(" not in source
