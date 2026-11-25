"""Offline tests: no provider, LLM or MongoDB network calls."""
from copy import deepcopy
import pytest
from chatbot.executive_silence_llm import (
    build_context, draft_followup, validate_draft, MAX_TURNS,
)

class Cursor(list):
    def limit(self, n):
        return self[:n]

class Properties:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
    def find(self, query, projection):
        self.calls.append((query, projection))
        return Cursor(self.rows)

def fixture():
    job = {"cycle": "c", "owner": "o", "session": "s"}
    anchor = {"actor_type": "human_agent", "channel": "executive_whatsapp",
              "assignment_cycle_id": "c", "assigned_executive_id": "o",
              "wasender_session_id": "s", "provider_message_id": "p",
              "timestamp": "2026-11-25T15:00:00+00:00",
              "content": "¿En qué le puedo ayudar? https://www.procasa.cl/16492"}
    lead = {"prospecto": {"codigo": "16492", "nombre": "PRIVATE"},
            "messages": [anchor], "commercial_intelligence": {"summary": {}}}
    prop = Properties([{"tipo_operacion": {"tipo": "Departamento", "venta": True, "arriendo": True},
                        "ubicacion": {"comuna": "Rancagua"}}])
    return {"universo_cartera_prop360": prop}, lead, job, anchor

def test_draft_receives_property_and_only_authorized_context_once():
    db, lead, job, anchor = fixture()
    alien = deepcopy(anchor)
    alien.update(assigned_executive_id="different", content="DO NOT SHARE")
    lead["messages"].insert(0, alien)
    calls = []
    def propose(ctx):
        calls.append(ctx)
        assert ctx["property"]["operaciones_publicadas"] == ["VENTA", "ARRIENDO"]
        assert ctx["property"]["comuna"] == "Rancagua"
        assert len(ctx["turns"]) == 1
        assert "PRIVATE" not in str(ctx) and "DO NOT SHARE" not in str(ctx)
        assert "https://" not in str(ctx)
        return {"decision": "DRAFT", "message": "Respecto al departamento en Rancagua, ¿busca comprar o arrendar?"}
    msg, usage = draft_followup(db, lead, job, anchor, scenario="INITIAL_NO_REPLY", propose=propose)
    assert len(calls) == 1 and len(db["universo_cartera_prop360"].calls) == 1
    assert msg == "Respecto al departamento en Rancagua, ¿busca comprar o arrendar?"
    assert "seguimiento automático" not in msg
    assert "comprar o arrendar" in msg
    assert usage["version"]

def test_unrelated_property_not_loaded():
    db, lead, job, anchor = fixture()
    anchor["content"] = "¿Le interesa otra alternativa?"
    context = build_context(db, lead, job, anchor, "DIALOGUE_INTERRUPTED", 1)
    assert context["property"] == {}
    assert not db["universo_cartera_prop360"].calls

def test_context_is_bounded():
    db, lead, job, anchor = fixture()
    lead["messages"] = [dict(anchor, provider_message_id=str(i)) for i in range(30)]
    ctx = build_context(db, lead, job, anchor, "DIALOGUE_INTERRUPTED", 1)
    assert len(ctx["turns"]) <= MAX_TURNS

def test_model_hold_is_not_replaced_with_generic_message():
    db, lead, job, anchor = fixture()
    msg, _ = draft_followup(db, lead, job, anchor, scenario="INITIAL_NO_REPLY",
                           propose=lambda ctx: {"decision": "HOLD", "message": ""})
    assert msg is None

@pytest.mark.parametrize("text", [
    "La propiedad está disponible. ¿Quiere visitarla?",
    "¿Puede enviarnos su RUT para continuar?",
    "¿Le interesa por 3700 UF?",
    "¿Está interesado? ¿Quiere visitar?",
    "¿Le interesa? https://example.com",
    "Hola, seguimiento automático de PROCASA. ¿Busca comprar?",
    "Respecto a el departamento, ¿busca comprar?",
])
def test_unsafe_draft_is_rejected(text):
    with pytest.raises(ValueError):
        validate_draft({"decision": "DRAFT", "message": text})
