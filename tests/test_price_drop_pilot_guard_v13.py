"""V13 replay of real WhatsApp regressions and adversarial commerce tests.

No external provider calls and no CRM/agent mutation.
"""
from __future__ import annotations

import pytest

from chatbot.price_drop_pilot_guard_v13 import (
    VERSION, _time_review, reply_v13, safe_outbound_v13,
)
from chatbot.price_drop_pilot_isolation import _visit_parts

CTX = {
    "campaign_version": VERSION,
    "campaign_id": "test:v13",
    "property_code": "6436",
    "old_price_uf": 2790.0,
    "new_price_uf": 2690.0,
    "price_drop_date": None,
    "property_facts": {"dormitorios": 2, "banos": 2, "superficie_total": 65},
}
HISTORY_VISIT = [
    {"customer_text": "me interesa, es negociable?",
     "bot_text": "No tengo confirmado si aceptan negociar. ¿Te gustaría visitarlo?"},
    {"customer_text": "oka, cuando se puede ir a vecfbrv",
     "bot_text": "¿Qué día y horario te acomodarían?"},
]

def answer(text, history=None, roles=()):
    return reply_v13(text, CTX, history or [], parse_parts=_visit_parts,
                     notice_roles=roles)

@pytest.mark.parametrize("question,required", [
    ("que propeidad e sno recuerdo", ["San Miguel", "6436", "2 dormitorios"]),
    ("qué propiedad es, no recuerdo", ["6436", "2 baños", "UF 2.690"]),
    ("que depto es?", ["San Miguel", "6436"]),
    ("no me acuerdo qué propiedad era", ["6436"]),
    ("me envias mensaje y dice que no quieres darme informacion?", ["fuimos nosotros", "6436"]),
    ("tan directo?", ["Perdona", "San Miguel"]),
    ("como", ["gracias", "San Miguel"]),
    ("como estás?", ["gracias"]),
    ("cuando bajo?", ["fecha exacta"]),
    ("y cuando bajo de precio?", ["fecha exacta"]),
    ("te pregundo cuanto bajo de precio", ["100 UF", "2.790", "2.690"]),
    ("cuánto bajó?", ["100 UF", "3,58%"]),
    ("cuanto bajo y si es negociable?", ["100 UF", "negociación"]),
    ("cuando bajo y cuanto bajo?", ["fecha exacta", "100 UF"]),
    ("me interesa, es negociable?", ["interese", "negociación", "2.690"]),
    ("¿es negociable?", ["negociación", "2.690"]),
    ("¿quiero visitar el depto?", ["Qué día"]),
    ("oka, cuando se puede ir a vecfbrv", ["Qué día"]),
    ("quiero verlo mañana a las 10", ["mañana", "10:00"]),
    ("mañana a las 4 de ña mañana", ["04:00", "horario poco habitual"]),
    ("mañana a las 4 de la tarde", ["mañana", "16:00"]),
    ("mañana a las 4", ["04:00", "16:00"]),
    ("mañana a las 10 o a las 11", ["10 o 11", "mañana"]),
    ("lunes a las 10", ["lunes", "10:00"]),
    ("quien me contactara?", ["no a un ejecutivo real", "dos avisos"]),
    ("ya está confirmada la visita?", ["no a un ejecutivo real", "no hay visita confirmada"]),
])
def test_26_replay_observed_and_adversarial(question, required):
    reply = answer(question, HISTORY_VISIT,
                   roles=("ADMIN", "EXECUTIVE"))
    assert reply is not None, question
    for fragment in required:
        assert fragment.casefold() in reply.casefold(), (question, reply, fragment)
    assert "te contacta desde su número directo" not in reply.casefold()

@pytest.mark.parametrize("message", [
    "hola", "bueno", "gracias", "no me interesa",
    "stop", "no me escriban", "no quiero visitar",
    "no quiero que me contacten", "dame de baja",
])
def test_neutral_decline_or_opt_out_never_triggers_sales_handoff(message):
    assert answer(message) is None

def test_price_question_does_not_turn_into_visit_or_hide_price():
    reply = answer("me interesa, es negociable?")
    assert "aceptaría una negociación" in reply
    assert "¿qué día" not in reply.casefold()
    assert "visita confirmada" not in reply

def test_known_fact_before_model_or_generic_fallback():
    reply = answer("que propeidad es no recuerdo")
    assert "Disculpa, no quiero darte información" not in reply
    assert "https://www.procasa.cl/6436" in reply

def test_safe_outbound_blocks_recreated_v12_false_agent_promise():
    unsafe = (
        "Claro, con gusto coordinamos una visita 😊 "
        "Con eso el ejecutivo asignado te contacta desde su número directo."
    )
    safe = safe_outbound_v13(unsafe, "oka, cuando se puede ir a vecfbrv",
                             CTX, HISTORY_VISIT, parse_parts=_visit_parts)
    assert "¿Qué día" in safe
    assert "ejecutivo asignado" not in safe
    assert "te contacta" not in safe

@pytest.mark.parametrize("model_output", [
    "El ejecutivo asignado te contactará desde su número directo.",
    "Ya agendamos la visita, muchas gracias.",
    "Le avisamos al ejecutivo; pronto se pondrán en contacto.",
    "Vamos a coordinar una visita con el agente.",
    "Tu solicitud quedó registrada y el ejecutivo te llamará.",
])
def test_fabricated_llm_handoff_is_never_sent(model_output):
    safe = safe_outbound_v13(model_output, "¿cuándo se puede visitar?",
                             CTX, [], parse_parts=_visit_parts)
    assert safe != model_output
    assert "todavía" in safe or "confirmar" in safe or "¿Qué día" in safe

def test_unverified_property_data_are_not_invented():
    ctx = {**CTX, "property_facts": {}}
    reply = reply_v13("que propiedad es no recuerdo", ctx, [],
                      parse_parts=_visit_parts)
    assert "6436" in reply
    assert "2 dormitorios" not in reply
    assert "65 m²" not in reply

def test_all_hour_options_have_no_claimed_booking():
    for question in (
        "mañana a las 4 de ña mañana", "mañana a las 4 de la tarde",
        "mañana a las 4", "lunes a las 10", "mañana a las 10 o a las 11",
        "sábado a las 21", "martes a las 23",
    ):
        reply = answer(question, HISTORY_VISIT)
        assert reply is not None, question
        assert "agendamos" not in reply.casefold(), (question, reply)
        assert "quedó agendada" not in reply.casefold(), (question, reply)

def test_preference_is_remembered_and_not_a_booking():
    history = [
        {"customer_text": "me interesa", "bot_text": "¿Te gustaría visitarlo?"},
        {"customer_text": "lunes a las 10",
         "bot_text": "Entiendo que te acomoda el lunes a las 10:00."},
    ]
    reply = answer("quién me llamará?", history,
                   roles=("ADMIN", "EXECUTIVE"))
    assert "avisos internos de prueba" in reply
    assert "visita confirmada" in reply
    assert "¿Qué día" not in reply

def test_single_word_yes_is_not_automatically_a_booking():
    reply = answer("ok", HISTORY_VISIT)
    assert reply is None
