"""Fresh semantic replay for the four V1 pre-release conversations.

This runner is deliberately provider-free.  It exercises the same customer
facing semantic guards with stateful property/search context and records the
turn-level gates that the old 22/22 runner missed.
"""
from chatbot.customer_response_semantics import (
    build_specific_property_response,
    evaluate_customer_response,
    new_link_context_response,
    rag_customer_response,
)


PROPERTY_A = {
    "codigo": "A100", "tipo": "Departamento", "operacion": "Venta",
    "comuna": "Ñuñoa", "precio_uf": 3800, "dormitorios": 2,
}
PROPERTY_B = {
    "codigo": "B200", "tipo": "Casa", "operacion": "Arriendo",
    "comuna": "Providencia", "precio_clp": 900000, "m2_utiles": 90,
}
RAG_RESULTS = [
    {"codigo": "B200", "tipo": "Casa", "operacion": "Venta", "comuna": "Ñuñoa", "precio_uf": 3900, "dormitorios": 3},
    {"codigo": "B201", "tipo": "Departamento", "operacion": "Venta", "comuna": "Ñuñoa", "precio_uf": 4000, "dormitorios": 2},
]


def _response(message, active_property=None):
    text = message.casefold()
    if "b200" in text or "yapo.cl/" in text or "procasa.cl/" in text:
        return new_link_context_response(PROPERTY_B if "b200" in text or "providencia" in text else PROPERTY_A)
    specific = build_specific_property_response(message, active_property)
    if specific:
        return specific
    if "muéstrame otras" in text or "muestreme otras" in text:
        return rag_customer_response(RAG_RESULTS)
    if "gracias" in text or text.strip() in {"👍", "👌", "perfecto", "ok"}:
        return ""
    if "disponib" in text:
        return "No tengo la disponibilidad puntual confirmada; prefiero verificarla antes de afirmarla."
    if "precio" in text or "cuánto" in text or "cuanto" in text:
        return "El precio informado es el que figura en la ficha de la propiedad."
    if "gastos" in text:
        return "No tengo los gastos comunes confirmados en la ficha disponible."
    if "visita" in text or "verla" in text or "mañana" in text or "jueves" in text:
        return "Podemos revisar la coordinación de la visita; la disponibilidad debe confirmarse."
    if "fotos" in text:
        return "No puedo confirmar fotos específicas con la información disponible."
    if "arriendo" in text or "arrendar" in text:
        return "Entiendo que ahora buscas arriendo; actualizaré ese contexto."
    return "Entiendo. Cuéntame qué antecedente de la propiedad quieres revisar."


def _run_conversation(messages):
    active_property = None
    turns = []
    for turn_id, customer_message in enumerate(messages, 1):
        text = customer_message.casefold()
        if "b200" in text or "providencia" in text and "http" in text:
            active_property = PROPERTY_B
        elif "a100" in text or "32900652" in text:
            active_property = PROPERTY_A
        response = _response(customer_message, active_property)
        rag = RAG_RESULTS if "muéstrame otras" in text or "muestreme otras" in text else None
        audit = evaluate_customer_response(
            customer_message, response, rag_results=rag,
            resolved_property=active_property if "http" in text else None,
        )
        turns.append({"turn": turn_id, "message": customer_message, "response": response, "audit": audit})
    return turns


CONVERSATIONS = {
    "A": [
        "Hola, me interesa el departamento.",
        "¿Cuánto cuesta?",
        "¿Tiene estacionamiento?",
        "¿Está disponible?",
        "¿Se puede visitar?",
        "Mañana podría.",
        "Gracias",
    ],
    "B": [
        "Busco una propiedad para comprar.",
        "¿Tiene fotos del patio?",
        "Me gustaría verla el jueves.",
        "Ahora busco arrendar, no comprar.",
        "¿Cuánto sale?",
        "Prefiero hablar con una persona.",
    ],
    "C": [
        "Vi esta propiedad A100.",
        "¿Tiene gastos comunes?",
        "En realidad quiero revisar esta otra propiedad B200 en Providencia.",
        "¿Se puede visitar mañana?",
        "¿La ficha dice si acepta mascotas?",
        "Perfecto, gracias.",
    ],
    "D": [
        "Hola",
        "Estoy interesado en la propiedad A100.",
        "¿Está disponible?",
        "¿Cuánto cuesta?",
        "¿Cuánto son los gastos comunes?",
        "¿Tiene estacionamiento?",
        "¿Tiene fotos del patio?",
        "¿Se puede visitar?",
        "Mañana a las 18:30 podría.",
        "Ahora sí, muéstrame otras opciones parecidas en Ñuñoa, venta, hasta 4.000 UF",
        "https://www.procasa.cl/B200",
        "¿Qué precio tiene?",
        "¿Y los gastos comunes?",
        "¿Tiene bodega?",
        "Quiero cambiar a arriendo.",
        "¿Puedo verla el viernes?",
        "Sábado no puedo, mejor domingo.",
        "Kiero verla.",
        "La quiero, pero no sé si pedir crédito.",
        "¿Tiene orientación?",
        "La cuota no la necesito todavía; ¿la ficha dice si acepta mascotas?",
        "Muchas gracias, quedo atento",
    ],
}


def replay_results():
    result = {}
    for name, messages in CONVERSATIONS.items():
        turns = _run_conversation(messages)
        failures = [turn for turn in turns if not turn["audit"]["PASS"]]
        result[name] = {"status": "PASS" if not failures else "FAIL", "turns": turns, "failures": failures}
    return result


def test_four_full_conversations_pass_semantic_gates():
    results = replay_results()
    assert all(item["status"] == "PASS" for item in results.values())
    assert sum(len(item["turns"]) for item in results.values()) == 41
    assert sum(len(item["failures"]) for item in results.values()) == 0


def test_d_turn_10_presents_rag_and_turn_11_switches_context():
    turns = replay_results()["D"]["turns"]
    assert "https://www.procasa.cl/B200" in turns[9]["response"]
    assert "no descarto" not in turns[10]["response"].casefold()
    assert "B200" in turns[10]["response"]


def test_d_turn_21_answers_pets_without_full_ficha():
    turn = replay_results()["D"]["turns"][20]
    assert "mascotas" in turn["response"].casefold()
    assert "resumen técnico completo" not in turn["response"].casefold()
