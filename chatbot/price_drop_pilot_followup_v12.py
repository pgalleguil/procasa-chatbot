"""Verified handoff copy for the isolated WhatsApp V12 price-drop pilot.

No CRM writes, agent notifications, or visit bookings. The only notices this
module can describe are TEST notices to the allowlisted test phone.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable

VERSION = "price_drop_pilot_v12_followup_test"


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
    r"\b(?:visitar|visistar|visidta|visilta|visista|visita|agendar|coordinar|ver el (?:depto|departamento)|"
    r"ir a verlo|ir a verla|quiero (?:verlo|verla)|conocer el departamento)\b", re.I
)
_INTEREST = re.compile(
    r"\b(?:me interesa|estoy interesado|estoy interesada|quiero ir a verlo|"
    r"quiero ir a verla|quiero agendar|quiero hacer una oferta)\b", re.I
)
_URGENT = re.compile(r"\b(?:pronto|urgente|apurad|cuanto antes|otras visit|otras vist|esta semana)\w*", re.I)
_CONFIRM = re.compile(r"\b(?:ya quedo|quedo agend|esta confirm|se confirm|confirmaste|confirmaron|reservad)\w*", re.I)


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
    if re.search(r"\\b(?:no me interesa|no quiero (?:visitar|visita|que me contacten)|"
                 r"ya no quiero ir|no me escriban|stop|darme de baja)\\b", message):
        return None
    contact = bool(_CONTACT.search(message))
    visit = bool(_VISIT.search(message))
    interest = bool(_INTEREST.search(message))
    # Short acceptance follows the latest bot question about visiting.
    if not interest and re.fullmatch(r"(?:si|sip|claro|dale|ok|okay)", message):
        previous_bot = _plain((history[-1].get("bot_text") or "") if history else "")
        interest = bool(re.search(r"visita|conocerlo|ir a verlo|interes", previous_bot))
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
                "Es tu horario preferido, no una visita confirmada."
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
                "Es una preferencia, no una reserva confirmada."
            )
        return (
            "¡Claro! ¿Qué día te acomodaría ir a verlo? "
            "El equipo debe confirmar disponibilidad antes de agendar."
        )

    if interest:
        if reported:
            return (
                f"¡Qué bueno! 😊 Tengo presente que te acomoda {reported}. "
                "Si quieres avanzar con una visita real, debe confirmarla el equipo comercial."
            )
        return (
            "¡Qué bueno! 😊 ¿Te gustaría conocerlo en persona? "
            "Podemos ver qué día te acomodaría, aunque aún no hay visita confirmada."
        )
    return None
