"""Verified handoff copy for the isolated WhatsApp V12 price-drop pilot.

No CRM writes, agent notifications, or visit bookings. The only notices this
module can describe are TEST notices to the allowlisted test phone.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable


def _plain(value: str) -> str:
    value = "".join(
        c for c in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(c)
    )
    return re.sub(r"\s+", " ", value).strip()


_CONTACT = re.compile(
    r"\b(?:quien (?:me |se )?(?:contact|llam|escrib|coordina)|"
    r"me (?:van a |pueden |puede )?(?:contact|conatct|llam)|"
    r"que me (?:contact|conatct|llam)|"
    r"(?:contact|conatct)en?\w*|llamen|hablar con (?:un |una )?(?:ejecutiv|asesor))",
    re.I,
)
_VISIT = re.compile(
    r"\b(?:visitar|visita|agendar|coordinar|ver el (?:depto|departamento)|"
    r"ir a verlo|ir a verla|quiero (?:verlo|verla)|conocer el departamento)\b", re.I
)
_INTEREST = re.compile(
    r"\b(?:me interesa|estoy interesado|estoy interesada|quiero ir a verlo|"
    r"quiero ir a verla|quiero agendar|quiero hacer una oferta)\b", re.I
)
_URGENT = re.compile(r"\b(?:pronto|urgente|apurad|cuanto antes|otras visit|esta semana)\w*", re.I)
_CONFIRM = re.compile(r"\b(?:ya quedo|quedo agend|esta confirm|confirmaste|reservad)\w*", re.I)


def _recent_visit_preference(history: list[dict], parse_parts: Callable) -> dict:
    """Previous customer dates are facts; previous bot promises are not."""
    preference = {}
    for turn in history[-24:]:
        customer = _plain(turn.get("customer_text") or "")
        if re.search(r"\b(?:no quiero visitar|cancele la visita|ya no quiero ir)\b", customer):
            preference = {}
            continue
        fields = parse_parts(customer)
        if fields:
            preference.update(fields)
    return preference


def _show_preference(preference: dict) -> str:
    day = str(preference.get("day") or "").replace("miercoles", "miércoles").replace("sabado", "sábado")
    time = str(preference.get("time") or "")
    period = str(preference.get("period") or "")
    if day and time:
        return f"{day} {time}"
    if day and period:
        return f"{day} {period}"
    return day or time or period


def followup_reply(
    text: str,
    history: list[dict],
    *,
    parse_parts: Callable,
    notice_roles: tuple[str, ...] = (),
) -> str | None:
    """Return a truthful, concise response for test-only handoff conversations.

    An affirmative answer can trigger *test* notices later in the same inbound
    handler, but this response must not claim they have already been sent.
    """
    message = _plain(text)
    contact = bool(_CONTACT.search(message))
    visit = bool(_VISIT.search(message))
    interest = bool(_INTEREST.search(message))
    urgent = bool(_URGENT.search(message))
    asks_confirmation = bool(_CONFIRM.search(message))
    current = parse_parts(message)
    previous = _recent_visit_preference(history, parse_parts)
    preference = {**previous, **current}
    reported = _show_preference(preference)
    had_visit_request = any(_VISIT.search(_plain(t.get("customer_text") or "")) for t in history[-24:])
    is_visit_context = had_visit_request or bool(previous) or visit
    if not (contact or visit or interest or asks_confirmation or (current and is_visit_context)):
        return None

    # Do not make the existence of a test alert sound like an actual assignment.
    if contact or asks_confirmation:
        if set(notice_roles) >= {"ADMIN", "EXECUTIVE"}:
            status = (
                "Ya se emitieron los dos avisos de prueba, pero únicamente a este "
                "número: no se ha solicitado contacto a un ejecutivo real."
            )
        elif notice_roles:
            status = (
                "Hay un aviso de prueba registrado, pero no una solicitud "
                "enviada a un ejecutivo real."
            )
        else:
            status = (
                "Todavía no hay una solicitud enviada a un ejecutivo real."
            )
        lead = "Entiendo que te urge avanzar. " if urgent else ""
        detail = f" Tengo presente que te acomoda {reported}." if reported else ""
        return (
            lead + "Un ejecutivo de PROCASA coordina las visitas en una gestión real. "
            + status + detail + " La visita tampoco está confirmada."
        )

    if current and is_visit_context:
        detail = _show_preference(preference)
        if preference.get("day") and (preference.get("time") or preference.get("period")):
            return (
                f"Perfecto, tengo anotado en esta conversación que te acomoda {detail}. "
                "Es tu horario preferido, no una visita confirmada; "
                "en esta prueba no se agenda ni se deriva al equipo real."
            )
        if preference.get("day"):
            return (
                f"Perfecto, {detail} te acomodaría. ¿Prefieres mañana o tarde? "
                "Aún no hay una visita confirmada."
            )
        if preference.get("time") or preference.get("period"):
            return (
                f"Entiendo que te acomoda {detail}. ¿Qué día te serviría? "
                "Aún no hay una visita confirmada."
            )

    if visit:
        if reported:
            return (
                f"Entiendo que te gustaría visitarlo y que te acomoda {reported}. "
                "Es una preferencia, no una reserva; en este piloto no se agenda "
                "con un ejecutivo real."
            )
        return (
            "¡Claro! ¿Qué día te acomodaría ir a verlo? "
            "No puedo confirmar disponibilidad ni agendar una visita desde esta prueba."
        )

    if interest:
        if reported:
            return (
                f"¡Qué bueno! 😊 Tengo presente que te acomoda {reported}. "
                "Si quieres avanzar con una visita real, debe confirmarla el equipo comercial."
            )
        return (
            "¡Qué bueno! 😊 ¿Te gustaría conocerlo en persona? "
            "Puedo conversar contigo sobre el día que te acomodaría, "
            "pero esta prueba no agenda visitas reales."
        )
    return None
