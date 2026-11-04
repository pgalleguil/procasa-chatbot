"""Twenty multi-turn price-drop conversations replayed as continuous history.

The previous unit suite passed 381 tests yet missed basic first-turn dialogue.
Each scenario updates user and bot history turn by turn and checks exact intent.
No real provider, CRM, model API, agent notification or appointment.
"""
from __future__ import annotations

import pytest

from chatbot.price_drop_pilot_guard_v13 import reply_v13
from chatbot.price_drop_pilot_isolation import _visit_parts

CONTEXT = {
    "property_code": "6436", "old_price_uf": 2790.0, "new_price_uf": 2690.0,
    "price_drop_date": None, "property_facts": {
        "dormitorios": 2, "banos": 2, "superficie_total": 65,
    },
}

# Each tuple is customer text and a list of required fragments (None means
# the neutral/decline message is intentionally passed to the normal handler).
DIALOGUES = [
    [("hola", ["San Miguel", "2.690"]),
     ("me interesa", ["conocer el departamento"]),
     ("sí", ["Qué día"])],
    [("hola", ["San Miguel"]), ("no", ["2.790", "2.690", "6436"])],
    [("hola", ["San Miguel"]), ("sí", ["nuevo precio"])],
    [("hola", ["San Miguel"]),
     ("qué propiedad es, no recuerdo", ["6436", "2 dormitorios"])],
    [("me interesa", ["San Miguel", "2.690"]),
     ("¿y es negociable?", ["negociación"])],
    [("me interesa", ["San Miguel"]),
     ("no", ["no coordinamos una visita"])],
    [("me interesa", ["San Miguel"]),
     ("si", ["Qué día"]),
     ("mañana a las 4 de la tarde", ["16:00", "mañana"])],
    [("me interesa", ["San Miguel"]),
     ("si", ["Qué día"]),
     ("mañana a las 4 de la mañana", ["04:00", "poco habitual"])],
    [("me interesa", ["San Miguel"]),
     ("sí", ["Qué día"]),
     ("mañana a las 4", ["04:00", "16:00"])],
    [("me interesa", ["San Miguel"]),
     ("sí", ["Qué día"]),
     ("lunes a las 10", ["lunes", "10:00"])],
    [("hola", ["San Miguel"]),
     ("no entiendo de qué se trata", ["6436", "2.690"])],
    [("hola", ["San Miguel"]),
     ("me interesa, es negociable?", ["negociación", "2.690"])],
    [("hola", ["San Miguel"]),
     ("te pregundo cuanto bajo de precio", ["100 UF", "3,58%"])],
    [("hola", ["San Miguel"]),
     ("y cuando bajo de precio?", ["fecha exacta"])],
    [("hola", ["San Miguel"]),
     ("cuanto bajo y cuando bajo?", ["100 UF", "fecha exacta"])],
    [("buenas tardes", ["San Miguel"]),
     ("¿quiero visitar el depto?", ["Qué día"])],
    [("hola", ["San Miguel"]),
     ("quiero verlo mañana a las 10", ["mañana", "10:00"])],
    [("hola", ["San Miguel"]),
     ("me interesa", ["conocer el departamento"]),
     ("quién me llamará?", ["no se ha solicitado contacto"])],
    [("hola", ["San Miguel"]),
     ("me interesa", ["conocer el departamento"]),
     ("ya está confirmada la visita?", ["No hay visita confirmada"])],
    [("hola", ["San Miguel"]),
     ("hola", ["Hola de nuevo", "departamento"]),
     ("me interesa", ["conocer el departamento"])],
]

@pytest.mark.parametrize("script", DIALOGUES, ids=[
    "greeting-interest-yes", "greeting-no-not-optout",
    "greeting-yes-acknowledgment", "forgot-property",
    "interest-then-negotiation", "declines-visit-not-all-contact",
    "yes-then-16h", "yes-then-04h", "yes-then-ambiguous-4",
    "yes-then-monday-ten", "outreach-confusion",
    "interest-plus-negotiation", "amount-in-uf",
    "exact-date-unknown", "mixed-amount-date", "visit-typo",
    "date-and-hour-preference", "human-contact-status",
    "booking-status", "repeated-greeting"
])
def test_20_continuous_dialogues(script):
    history = []
    for customer, fragments in script:
        answer = reply_v13(customer, CONTEXT, history,
                           parse_parts=_visit_parts, notice_roles=())
        assert answer, (script, customer, history)
        for frag in fragments:
            assert frag.casefold() in answer.casefold(), (customer, answer, frag)
        forbidden = [
            "tu visita quedó confirmada", "te contacta desde su número",
            "ya avisé al ejecutivo", "visita agendada para",
        ]
        assert not any(bad in answer.casefold() for bad in forbidden), answer
        history.append({"customer_text": customer, "bot_text": answer})
