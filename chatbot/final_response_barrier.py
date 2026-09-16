"""The sole customer-response admission barrier before provider delivery.

It is deliberately pure: callers pass only a candidate response plus verified
context.  A response which cannot be made safe is suppressed, never sent.
"""
from __future__ import annotations

import re

from chatbot.phase3_conversation import (
    build_conversation_state, build_response_plan, deterministic_response, repair_response,
    select_next_best_action, validate_response,
)
from chatbot.conversation_policy import (
    build_visit_preference_confirmation,
    replace_repeated_visit_question,
)


def _safe_renderer(state, facts, message):
    if facts.get("rag_result_count") is not None:
        try:
            result_count = int(facts.get("rag_result_count"))
        except (TypeError, ValueError):
            result_count = 0
        if result_count > 0:
            return "Encontré una alternativa que coincide con los criterios que compartiste. Puedo mostrarte sus detalles para que la revises."
        return "No encontré opciones exactas con esos criterios. Podemos ampliar la búsqueda si quieres."
    action = select_next_best_action(state, message, facts=facts, property_resolved=bool(state.get("property_code")))
    return deterministic_response(state, action, facts, message) or ""


def _dedupe_exact_sentences(text: str) -> str:
    """Remove exact repeated sentences introduced by legacy safety transforms.

    This is intentionally conservative: only a byte-for-byte repeated
    sentence is removed, so the barrier does not rewrite the writer's prose
    or collapse intentionally different clauses.
    """
    seen = set()
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", str(text or "").strip()):
        normalized = sentence.strip().casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        kept.append(sentence.strip())
    return " ".join(kept)


def _required_components(plan, facts):
    available = {"price": ("precio_uf", "precio_clp"), "common_expenses": ("gastos_comunes",),
                 "bedrooms": ("dormitorios",), "parking": ("estacionamientos",),
                 "surface": ("superficie_util", "superficie_total")}
    return [item for item in plan.get("primary_intents", []) if item not in available or any(facts.get(key) not in (None, "") for key in available[item])]


def _covers(component, response, facts):
    text = str(response or "").casefold()
    if component == "availability": return "disponib" in text or "no tengo" in text
    if component == "price": return any(str(facts.get(key)).casefold() in text for key in ("precio_uf", "precio_clp") if facts.get(key) is not None)
    if component == "common_expenses": return str(facts.get("gastos_comunes", "")) in text
    if component == "bedrooms": return str(facts.get("dormitorios", "")) in text
    if component == "parking": return str(facts.get("estacionamientos", "")) in text
    if component == "surface": return any(str(facts.get(key, "")) in text for key in ("superficie_util", "superficie_total"))
    if component in {"storage", "negotiation"}:
        return any(phrase in text for phrase in (
            "no tengo", "no puedo confirmar", "debe confirmarse",
            "sin la información", "sin una revisión",
        )) or component in text
    # Unsupported facts are still covered when the response explicitly says
    # so.  "No puedo confirmar fotos..." is a valid answer, not a dropped
    # component.
    return any(phrase in text for phrase in (
        "no tengo", "no puedo confirmar", "sin la información",
        "sin la informacion", "no cuento con",
    )) or component in text


def _render_required_components(state, plan, facts, message):
    parts=[]
    if facts.get("rag_result_count") is not None:
        try:
            if int(facts.get("rag_result_count")) > 0:
                parts.append(_safe_renderer(state, facts, message))
        except (TypeError, ValueError):
            pass
    for component in _required_components(plan, facts):
        if component == "availability": parts.append("No tengo disponibilidad confirmada en la información disponible.")
        elif component == "price":
            value=facts.get("precio_uf") or facts.get("precio_clp"); parts.append(f"El precio informado es {value}.")
        elif component == "common_expenses": parts.append(f"Los gastos comunes informados son {facts['gastos_comunes']}.")
        elif component == "bedrooms": parts.append(f"La propiedad informa {facts['dormitorios']} dormitorio(s).")
        elif component == "parking": parts.append(f"La propiedad informa {facts['estacionamientos']} estacionamiento(s).")
        elif component == "surface": parts.append(f"La superficie informada es {facts.get('superficie_util') or facts.get('superficie_total')} m².")
        elif component == "photos": parts.append("No puedo confirmar fotos específicas con la información disponible.")
        elif component == "storage": parts.append("No puedo confirmar si incluye bodega con la información disponible.")
        elif component == "negotiation": parts.append("No puedo confirmar condiciones de negociación sin una revisión correspondiente.")
        elif component == "commission": parts.append("No tengo la comisión confirmada en la información disponible.")
    if "VISIT" in plan.get("secondary_intents", []):
        preference = state.get("visit_preference")
        if isinstance(preference, dict):
            preference = preference.get("text")
        if preference:
            # The customer already supplied a day/time. A required-component
            # repair must preserve it instead of asking the same scheduling
            # question again.
            parts.append(build_visit_preference_confirmation(str(preference)))
        else:
            parts.append("La disponibilidad debe confirmarse antes de agendar. ¿Qué día u horario te acomoda?")
    return " ".join(parts) or _safe_renderer(state, facts, message)


def admit_customer_response(*, candidate: str, customer_message: str, lead=None,
                            property_context=None, facts=None, source="unknown") -> dict:
    """Validate, repair, revalidate, then deterministically fail closed.

    ``approved`` is the only flag a provider-facing caller may use to admit a
    response.  Empty output is a deliberate no-reply, not an unsafe fallback.
    """
    lead = dict(lead or {})
    facts = dict(facts or {})
    property_context = dict(property_context or {})
    # The barrier runs after core has persisted the generated turn.  Rebuild
    # policy state from that durable history instead of only the current
    # inbound; otherwise a repair can lose a prior visit preference, actor
    # role, operation switch, or human ownership signal.
    history = list(lead.get("messages") or [])
    if not history or history[-1].get("role") != "user" or history[-1].get("content") != customer_message:
        history.append({"role": "user", "content": customer_message})
    state = build_conversation_state(lead, history, property_context=property_context)
    plan = build_response_plan(state, customer_message, facts)
    raw = _dedupe_exact_sentences(str(candidate or "").strip())
    preference = state.get("visit_preference")
    if isinstance(preference, dict):
        preference = preference.get("text")
    if preference:
        # The core normally removes this repetition before persistence, but
        # the barrier is the final authority and must also protect a response
        # produced by a legacy/deterministic branch.
        without_repeated_question = replace_repeated_visit_question(
            raw, visit_preference=str(preference), next_question=None,
        )
        if without_repeated_question != raw:
            raw = without_repeated_question
            if "preferencia" not in raw.casefold():
                raw = f"{raw} {build_visit_preference_confirmation(str(preference))}".strip()
    # A human request is terminal for this turn.  Do not allow a model
    # response (or a legacy candidate with extra questions) to append sales
    # qualification or another CTA after the handoff acknowledgement.
    if "HUMAN_HANDOFF" in (plan.get("secondary_intents") or []):
        raw = deterministic_response(state, {"next_best_action": "HANDOFF_HUMAN"}, facts, customer_message) or raw
    first = validate_response(raw, state=state, facts=facts, property_code=state.get("property_code"))
    repaired = raw if first.get("valid") else repair_response(
        raw, first, state=state, facts=facts, customer_message=customer_message
    )
    second = validate_response(repaired, state=state, facts=facts, property_code=state.get("property_code"))
    used_safe_renderer = False
    if not second.get("valid"):
        used_safe_renderer = True
        repaired = _safe_renderer(state, facts, customer_message)
        second = validate_response(repaired, state=state, facts=facts, property_code=state.get("property_code"))
    required = _required_components(plan, facts)
    missing = [item for item in required if not _covers(item, repaired, facts)]
    initially_missing = list(missing)
    if missing:
        used_safe_renderer = True
        repaired = _render_required_components(state, plan, facts, customer_message)
        second = validate_response(repaired, state=state, facts=facts, property_code=state.get("property_code"))
        missing = [item for item in required if not _covers(item, repaired, facts)]
    approved = bool(second.get("valid")) and not bool(state.get("human_active"))
    return {
        "approved": approved, "response": repaired if approved else "", "source": source,
        "state": state, "initial_validation": first, "final_validation": second,
        "repaired": raw != repaired, "used_safe_renderer": used_safe_renderer,
        "response_plan": plan, "required_components": required, "required_components_initially_dropped": initially_missing,
        "required_components_dropped": missing,
    }
